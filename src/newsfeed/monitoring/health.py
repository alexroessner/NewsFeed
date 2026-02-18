"""Health check and metrics endpoint for NewsFeed.

Provides:
- /health endpoint for container orchestrators (K8s, Cloud Run)
- /metrics endpoint for Prometheus scraping
- In-process metrics collection for pipeline stages

Uses stdlib http.server for zero-dependency operation.
"""
from __future__ import annotations

import json
import logging
import threading
import time
from collections import defaultdict
from http.server import HTTPServer, BaseHTTPRequestHandler
from typing import Any

log = logging.getLogger(__name__)


class Metrics:
    """Thread-safe in-process metrics collector.

    Tracks counters, gauges, and histograms for pipeline health.
    Compatible with Prometheus exposition format.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._counters: dict[str, float] = defaultdict(float)
        self._gauges: dict[str, float] = {}
        self._histograms: dict[str, list[float]] = defaultdict(list)
        self._start_time = time.monotonic()

    def inc(self, name: str, value: float = 1.0, labels: dict[str, str] | None = None) -> None:
        """Increment a counter."""
        key = self._key(name, labels)
        with self._lock:
            self._counters[key] += value

    def set_gauge(self, name: str, value: float, labels: dict[str, str] | None = None) -> None:
        """Set a gauge value."""
        key = self._key(name, labels)
        with self._lock:
            self._gauges[key] = value

    def observe(self, name: str, value: float, labels: dict[str, str] | None = None) -> None:
        """Record a histogram observation (e.g., latency)."""
        key = self._key(name, labels)
        with self._lock:
            hist = self._histograms[key]
            hist.append(value)
            # Cap at 1000 observations to prevent unbounded growth
            if len(hist) > 1000:
                self._histograms[key] = hist[-500:]

    def snapshot(self) -> dict[str, Any]:
        """Return all metrics as a dict."""
        with self._lock:
            return {
                "counters": dict(self._counters),
                "gauges": dict(self._gauges),
                "histograms": {
                    k: {
                        "count": len(v),
                        "sum": sum(v),
                        "avg": sum(v) / len(v) if v else 0,
                        "p50": sorted(v)[len(v) // 2] if v else 0,
                        "p99": sorted(v)[int(len(v) * 0.99)] if v else 0,
                    }
                    for k, v in self._histograms.items()
                },
                "uptime_seconds": round(time.monotonic() - self._start_time, 1),
            }

    def prometheus_format(self) -> str:
        """Export metrics in Prometheus text exposition format."""
        lines: list[str] = []
        with self._lock:
            for key, val in sorted(self._counters.items()):
                name, labels = self._parse_key(key)
                lines.append(f"# TYPE {name} counter")
                lines.append(f"{name}{labels} {val}")

            for key, val in sorted(self._gauges.items()):
                name, labels = self._parse_key(key)
                lines.append(f"# TYPE {name} gauge")
                lines.append(f"{name}{labels} {val}")

            for key, values in sorted(self._histograms.items()):
                name, labels = self._parse_key(key)
                if not values:
                    continue
                lines.append(f"# TYPE {name} summary")
                lines.append(f"{name}_count{labels} {len(values)}")
                lines.append(f"{name}_sum{labels} {sum(values):.3f}")

            lines.append(f"# TYPE newsfeed_uptime_seconds gauge")
            lines.append(f"newsfeed_uptime_seconds {time.monotonic() - self._start_time:.1f}")

        return "\n".join(lines) + "\n"

    @staticmethod
    def _key(name: str, labels: dict[str, str] | None = None) -> str:
        if not labels:
            return name
        label_str = ",".join(f'{k}="{v}"' for k, v in sorted(labels.items()))
        return f"{name}{{{label_str}}}"

    @staticmethod
    def _parse_key(key: str) -> tuple[str, str]:
        if "{" in key:
            name, rest = key.split("{", 1)
            return name, "{" + rest
        return key, ""


# Global metrics instance
metrics = Metrics()


class HealthCheckHandler(BaseHTTPRequestHandler):
    """HTTP handler for health + metrics endpoints."""

    # Reference to the engine for health checks
    engine = None

    def do_GET(self) -> None:
        if self.path == "/health" or self.path == "/healthz":
            self._respond_health()
        elif self.path == "/metrics":
            self._respond_metrics()
        elif self.path == "/ready" or self.path == "/readyz":
            self._respond_ready()
        else:
            self.send_error(404)

    def _respond_health(self) -> None:
        """Basic liveness probe — always returns 200 if the process is running."""
        body = json.dumps({"status": "healthy", "ts": time.time()})
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(body.encode())

    def _respond_ready(self) -> None:
        """Readiness probe — returns 200 only when the engine is initialized."""
        if self.engine is not None:
            body = json.dumps({"status": "ready", "ts": time.time()})
            self.send_response(200)
        else:
            body = json.dumps({"status": "not_ready"})
            self.send_response(503)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(body.encode())

    def _respond_metrics(self) -> None:
        """Prometheus metrics endpoint."""
        body = metrics.prometheus_format()
        self.send_response(200)
        self.send_header("Content-Type", "text/plain; version=0.0.4; charset=utf-8")
        self.end_headers()
        self.wfile.write(body.encode())

    def log_message(self, format: str, *args: Any) -> None:
        """Suppress default access logging (too noisy for health checks)."""
        pass


def start_health_server(port: int = 8080, engine: Any = None) -> threading.Thread:
    """Start the health check server in a background thread.

    Args:
        port: Port to listen on (default 8080, respects PORT env var)
        engine: Engine instance for readiness checks
    """
    import os
    port = int(os.environ.get("HEALTH_PORT", os.environ.get("PORT", port)))

    HealthCheckHandler.engine = engine
    server = HTTPServer(("0.0.0.0", port), HealthCheckHandler)

    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    log.info("Health check server started on port %d", port)
    return thread
