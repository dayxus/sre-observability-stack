#!/usr/bin/env python3
"""Synthetic target for the observability lab.

The stack needs a workload that is honest about its own behaviour: something that serves
requests, exposes Prometheus metrics, terminates TLS with a certificate we control, and
can accept the Alertmanager webhook so the alert path can be proven end to end. That is
this file, and it is deliberately small: the point of the repository is the telemetry
around it, not the service itself.

Endpoints
    GET  /healthz              liveness/readiness, probed by blackbox over HTTP
    GET  /api/orders           synthetic order list
    GET  /api/orders/<id>      one synthetic order, 404 when the id is not numeric
    GET  /api/slow             200 after a fixed delay inside the latency objective
    GET  /api/error            500 on purpose: fault injection for the error panels
    GET  /metrics              Prometheus exposition (requests, durations, inflight)
    POST /alerts               Alertmanager webhook sink
    GET  /alerts/received      what the sink has received (used by scripts/smoke.sh)

Two listeners are started: plain HTTP for Prometheus scraping and HTTPS for the blackbox
ssl probe. The certificate is generated at runtime by scripts/gen-certs.sh and is never
committed.

Only known paths are counted as SLI series. An unknown path answers 404 and is logged but
never becomes a metric label, because unbounded label values are how a scraped counter
turns into an outage of the monitoring system itself.

Configuration is entirely through the environment (see docker-compose.yml):
    TARGET_HTTP_PORT, TARGET_HTTPS_PORT, TARGET_TLS_CERT, TARGET_TLS_KEY, TARGET_LOG_FILE,
    SELF_TRAFFIC_ENABLED, SELF_TRAFFIC_INTERVAL_SECONDS
"""

from __future__ import annotations

import json
import os
import random
import signal
import ssl
import sys
import threading
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

VERSION = "1.0.0"

# Histogram buckets in seconds. 0.3 is the latency objective used by the SLO recording
# rules, so it has to be a bucket boundary for the ratio to be exact.
BUCKETS = (0.05, 0.1, 0.3, 0.5, 1.0, 2.5)

# The slow endpoint always takes this long: just inside the 0.3 s latency objective (the
# le="0.3" histogram bucket), so /api/slow is the top of the latency distribution without
# ever being the reason the objective is missed. Every other path answers in milliseconds.
SLOW_ENDPOINT_MAX_SECONDS = 0.28

MAX_SINK_ALERTS = 200

# Known paths are the only ones that become metric labels (see module docstring).
_APPROVED_PATHS = ("/healthz", "/api/orders", "/api/slow", "/api/error")

ORDERS = [
    {"id": 1001, "item": "widget", "quantity": 3, "status": "shipped"},
    {"id": 1002, "item": "gizmo", "quantity": 1, "status": "pending"},
    {"id": 1003, "item": "sprocket", "quantity": 12, "status": "delivered"},
    {"id": 1004, "item": "flange", "quantity": 7, "status": "pending"},
]


def utc_now_iso() -> str:
    """RFC3339 with microseconds, which is what the promtail timestamp stage expects."""
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def classify(path: str) -> str | None:
    """Map a request path to its metric label, or None when it must not be counted."""
    if path in _APPROVED_PATHS:
        return path
    if path.startswith("/api/orders/") and path.rsplit("/", 1)[-1].isdigit():
        return "/api/orders/{id}"
    return None


def escape_label(value: str) -> str:
    """Escape a Prometheus label value."""
    return value.replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n")


class LogWriter:
    """JSON-lines logger. One file, appended by both listeners and the self-traffic loop."""

    def __init__(self, path: str | None) -> None:
        self._path = Path(path) if path else None
        self._lock = threading.Lock()
        if self._path is not None:
            self._path.parent.mkdir(parents=True, exist_ok=True)

    def write(self, level: str, logger: str, msg: str, **fields: object) -> None:
        record = {"time": utc_now_iso(), "level": level, "logger": logger, "msg": msg}
        record.update(fields)
        line = json.dumps(record, sort_keys=False)
        with self._lock:
            print(line, flush=True)
            if not self._path:
                return
            try:
                with self._path.open("a", encoding="utf-8") as handle:
                    handle.write(line + "\n")
            except OSError as exc:  # the lab must keep serving even if the log volume is gone
                print(
                    f'{{"level":"error","logger":"log","msg":"cannot write log file","error":"{exc}"}}',
                    file=sys.stderr,
                    flush=True,
                )


class Metrics:
    """Counters and histogram behind /metrics, with the SLI-relevant paths only."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self.reset()

    def reset(self) -> None:
        with self._lock:
            self._requests: dict[tuple[str, int], int] = {}
            self._buckets: dict[str, list[int]] = {}
            self._sums: dict[str, float] = {}
            self._counts: dict[str, int] = {}
            self._inflight = 0
            self._started = time.time()

    def request_started(self) -> None:
        with self._lock:
            self._inflight += 1

    def request_finished(self, path: str | None, code: int, duration: float) -> None:
        with self._lock:
            self._inflight -= 1
            if path is None:
                return
            self._requests[(path, code)] = self._requests.get((path, code), 0) + 1
            counts = self._buckets.setdefault(path, [0] * len(BUCKETS))
            for index, boundary in enumerate(BUCKETS):
                if duration <= boundary:
                    counts[index] += 1
            self._sums[path] = self._sums.get(path, 0.0) + duration
            self._counts[path] = self._counts.get(path, 0) + 1

    @property
    def inflight(self) -> int:
        with self._lock:
            return self._inflight

    @property
    def uptime(self) -> float:
        with self._lock:
            return time.time() - self._started

    def render(self) -> str:
        with self._lock:
            requests = dict(self._requests)
            buckets = {path: list(counts) for path, counts in self._buckets.items()}
            sums = dict(self._sums)
            counts = dict(self._counts)
            inflight = self._inflight
            uptime = time.time() - self._started

        lines = [
            "# HELP synthetic_http_requests_total Requests served, by path and status code.",
            "# TYPE synthetic_http_requests_total counter",
        ]
        for (path, code), value in sorted(requests.items()):
            lines.append(
                f'synthetic_http_requests_total{{path="{escape_label(path)}",code="{code}"}} {value}'
            )

        lines += [
            "# HELP synthetic_http_request_duration_seconds Request duration in seconds.",
            "# TYPE synthetic_http_request_duration_seconds histogram",
        ]
        for path in sorted(buckets):
            for index, boundary in enumerate(BUCKETS):
                lines.append(
                    "synthetic_http_request_duration_seconds_bucket"
                    f'{{path="{escape_label(path)}",le="{boundary}"}} {buckets[path][index]}'
                )
            lines.append(
                "synthetic_http_request_duration_seconds_bucket"
                f'{{path="{escape_label(path)}",le="+Inf"}} {counts[path]}'
            )
            lines.append(
                f'synthetic_http_request_duration_seconds_sum{{path="{escape_label(path)}"}} {sums[path]:.6f}'
            )
            lines.append(
                f'synthetic_http_request_duration_seconds_count{{path="{escape_label(path)}"}} {counts[path]}'
            )

        lines += [
            "# HELP synthetic_http_inflight_requests Requests being served right now.",
            "# TYPE synthetic_http_inflight_requests gauge",
            f"synthetic_http_inflight_requests {inflight}",
            "# HELP synthetic_uptime_seconds Seconds since the process started.",
            "# TYPE synthetic_uptime_seconds gauge",
            f"synthetic_uptime_seconds {uptime:.3f}",
            "# HELP synthetic_build_info Build and runtime information.",
            "# TYPE synthetic_build_info gauge",
            "synthetic_build_info"
            f'{{version="{VERSION}",python="{sys.version_info.major}.{sys.version_info.minor}.{sys.version_info.micro}",'
            f'platform="{sys.platform}"}} 1',
        ]
        return "\n".join(lines) + "\n"


class AlertSink:
    """Webhook receiver standing in for a pager, so the alert path is testable."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._received: list[dict[str, object]] = []

    def add(self, payload: dict) -> int:
        alerts = payload.get("alerts") or []
        stored = []
        for alert in alerts:
            if not isinstance(alert, dict):
                continue
            labels = alert.get("labels") or {}
            annotations = alert.get("annotations") or {}
            stored.append(
                {
                    "received_at": utc_now_iso(),
                    "status": alert.get("status") or payload.get("status") or "unknown",
                    "alertname": labels.get("alertname"),
                    "severity": labels.get("severity"),
                    "service": labels.get("service"),
                    "summary": annotations.get("summary"),
                }
            )
        with self._lock:
            self._received.extend(stored)
            del self._received[:-MAX_SINK_ALERTS]
            return len(self._received)

    def snapshot(self) -> dict:
        with self._lock:
            return {"count": len(self._received), "alerts": list(self._received)}


STATE = {
    "metrics": Metrics(),
    "sink": AlertSink(),
    "log": LogWriter(os.environ.get("TARGET_LOG_FILE")),
}


class LabServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True


class LabHandler(BaseHTTPRequestHandler):
    server_version = f"synthetic-target/{VERSION}"
    protocol_version = "HTTP/1.1"

    # --- plumbing -------------------------------------------------------------------
    def log_message(self, fmt: str, *args: object) -> None:
        STATE["log"].write("debug", "http.server", fmt % args, client=self.client_address[0])

    def _respond(self, code: int, body: bytes, content_type: str) -> None:
        self.send_response(code)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def _respond_json(self, code: int, payload: object) -> None:
        body = json.dumps(payload, sort_keys=False).encode("utf-8")
        self._respond(code, body, "application/json")

    def do_GET(self) -> None:  # noqa: N802 - http.server API
        self._dispatch()

    def do_HEAD(self) -> None:  # noqa: N802 - http.server API
        self._dispatch()

    def do_POST(self) -> None:  # noqa: N802 - http.server API
        self._dispatch()

    # --- request handling -----------------------------------------------------------
    def _dispatch(self) -> None:
        path = self.path.split("?", 1)[0]
        metrics: Metrics = STATE["metrics"]
        log: LogWriter = STATE["log"]

        if path == "/metrics":
            body = metrics.render().encode("utf-8")
            self._respond(200, body, "text/plain; version=0.0.4; charset=utf-8")
            return

        if path == "/alerts" and self.command == "POST":
            self._handle_alert_webhook()
            return

        if path == "/alerts/received":
            self._respond_json(200, STATE["sink"].snapshot())
            return

        started = time.perf_counter()
        metric_path = classify(path)
        metrics.request_started()
        code = 500
        try:
            code = self._route(path, log)
        except Exception as exc:  # never let one bad request kill the listener
            log.write("error", "http", "unhandled error while serving request", path=path, error=str(exc))
            code = 500
            self._respond_json(500, {"error": "internal_error"})
        finally:
            duration = time.perf_counter() - started
            metrics.request_finished(metric_path, code, duration)
            log.write(
                "info",
                "http",
                "request served",
                path=path,
                method=self.command,
                status=code,
                duration_ms=round(duration * 1000, 3),
                client=self.client_address[0],
            )

    def _route(self, path: str, log: LogWriter) -> int:
        if path == "/healthz":
            self._respond_json(
                200,
                {
                    "status": "ok",
                    "version": VERSION,
                    "uptime_seconds": round(STATE["metrics"].uptime, 3),
                },
            )
            return 200

        if path == "/api/orders":
            self._respond_json(200, {"count": len(ORDERS), "orders": ORDERS})
            return 200

        if path.startswith("/api/orders/"):
            raw_id = path.rsplit("/", 1)[-1]
            order = next((item for item in ORDERS if str(item["id"]) == raw_id), None)
            if order is None:
                self._respond_json(404, {"error": "order_not_found", "id": raw_id})
                return 404
            self._respond_json(200, order)
            return 200

        if path == "/api/slow":
            time.sleep(SLOW_ENDPOINT_MAX_SECONDS)
            self._respond_json(200, {"status": "ok", "endpoint": "slow"})
            return 200

        if path == "/api/error":
            log.write("warn", "http", "fault injection triggered", path=path)
            self._respond_json(500, {"error": "injected_fault", "detail": "/api/error always fails"})
            return 500

        self._respond_json(404, {"error": "not_found", "path": path})
        return 404

    def _handle_alert_webhook(self) -> None:
        log: LogWriter = STATE["log"]
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            length = 0
        raw = self.rfile.read(length) if length > 0 else b""
        try:
            payload = json.loads(raw.decode("utf-8")) if raw else {}
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            log.write("error", "alertmanager-sink", "rejected malformed webhook payload", error=str(exc))
            self._respond_json(400, {"error": "invalid_json"})
            return

        alerts = payload.get("alerts") or []
        total = STATE["sink"].add(payload)
        log.write(
            "info",
            "alertmanager-sink",
            "alert notification received",
            status=payload.get("status"),
            alerts_in_batch=len(alerts),
            alerts_total=total,
        )
        self._respond_json(200, {"received": total})


def build_http_server(port: int) -> LabServer:
    return LabServer(("0.0.0.0", port), LabHandler)


def build_https_server(port: int, certfile: str, keyfile: str) -> LabServer:
    server = LabServer(("0.0.0.0", port), LabHandler)
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.load_cert_chain(certfile, keyfile)
    server.socket = context.wrap_socket(server.socket, server_side=True)
    return server


def start_self_traffic(base_url: str, interval: float) -> threading.Thread:
    """Drive a little traffic through the app so a fresh lab is not a flat line."""
    endpoints = ["/healthz", "/api/orders", "/api/orders/1001", "/api/orders/9999", "/api/slow", "/api/error"]
    weights = [4, 3, 2, 1, 2, 2]

    def loop() -> None:
        while True:
            path = random.choices(endpoints, weights=weights, k=1)[0]
            try:
                urllib.request.urlopen(base_url + path, timeout=5).read()
            except urllib.error.HTTPError:
                pass  # /api/error and the unknown order id answer 4xx/5xx on purpose
            except OSError as exc:
                STATE["log"].write(
                    "warn", "self-traffic", "self traffic request failed", path=path, error=str(exc)
                )
            time.sleep(interval)

    thread = threading.Thread(target=loop, name="self-traffic", daemon=True)
    thread.start()
    return thread


def main() -> int:
    log: LogWriter = STATE["log"]
    http_port = int(os.environ.get("TARGET_HTTP_PORT", "8080"))
    https_port = int(os.environ.get("TARGET_HTTPS_PORT", "8443"))
    certfile = os.environ.get("TARGET_TLS_CERT", "")
    keyfile = os.environ.get("TARGET_TLS_KEY", "")
    self_traffic_enabled = os.environ.get("SELF_TRAFFIC_ENABLED", "true").lower() in {"1", "true", "yes"}
    interval = float(os.environ.get("SELF_TRAFFIC_INTERVAL_SECONDS", "2"))

    servers = [build_http_server(http_port)]
    if certfile and keyfile and Path(certfile).exists() and Path(keyfile).exists():
        servers.append(build_https_server(https_port, certfile, keyfile))
    else:
        log.write(
            "error",
            "main",
            "tls certificate not found, HTTPS listener disabled",
            certfile=certfile,
            keyfile=keyfile,
            hint="run scripts/gen-certs.sh",
        )

    stop = threading.Event()

    def shutdown(signum: int, _frame: object) -> None:
        log.write("info", "main", "shutdown signal received", signal=signal.Signals(signum).name)
        stop.set()

    signal.signal(signal.SIGTERM, shutdown)
    signal.signal(signal.SIGINT, shutdown)

    threads = []
    for server in servers:
        thread = threading.Thread(target=server.serve_forever, name="http", daemon=True)
        thread.start()
        threads.append(thread)

    log.write(
        "info",
        "main",
        "synthetic target started",
        version=VERSION,
        http_port=servers[0].server_address[1],
        https_port=servers[1].server_address[1] if len(servers) > 1 else None,
        http_endpoints=["/healthz", "/api/orders", "/api/slow", "/api/error", "/metrics"],
    )

    if self_traffic_enabled:
        start_self_traffic(f"http://127.0.0.1:{servers[0].server_address[1]}", interval)
        log.write("info", "main", "self traffic enabled", interval_seconds=interval)

    stop.wait()
    for server in servers:
        server.shutdown()
        server.server_close()
    log.write("info", "main", "synthetic target stopped")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
