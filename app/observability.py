"""Application-owned OpenTelemetry providers and safe structured logging."""

import json
import logging
import os
import re
from dataclasses import dataclass, field
from datetime import UTC, datetime
from time import perf_counter
from urllib.parse import unquote, urlsplit
from uuid import uuid4

from opentelemetry import trace
from opentelemetry.exporter.otlp.proto.http._log_exporter import OTLPLogExporter
from opentelemetry.exporter.otlp.proto.http.metric_exporter import OTLPMetricExporter
from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor
from opentelemetry.instrumentation.logging.handler import LoggingHandler
from opentelemetry.sdk._logs import LoggerProvider
from opentelemetry.sdk._logs.export import BatchLogRecordProcessor
from opentelemetry.sdk.metrics import MeterProvider
from opentelemetry.sdk.metrics.export import PeriodicExportingMetricReader
from opentelemetry.sdk.metrics.view import View
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import Event, ReadableSpan, TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor, SpanExporter
from opentelemetry.trace import SpanKind, Status

METRIC_ATTRIBUTES = {
    "http.method",
    "http.request.method",
    "http.route",
    "http.target",
    "http.status_code",
    "http.response.status_code",
    "error.type",
}
LOG_ATTRIBUTES = (
    "http_method",
    "http_route",
    "status_code",
    "duration_ms",
    "error_type",
)
logger = logging.getLogger("holos.requests")


@dataclass(frozen=True)
class TelemetrySettings:
    service_name: str = "holos-api-gateway"
    endpoint: str = field(default="", repr=False)
    headers: dict[str, str] = field(default_factory=dict, repr=False)
    log_level: str = "INFO"

    @classmethod
    def from_env(cls):
        endpoint = os.getenv("OTEL_EXPORTER_OTLP_ENDPOINT") or os.getenv(
            "otlp_endpoint",  # noqa: SIM112 -- container variable supplied by deployment
            "",
        )
        headers = {}
        raw_headers = os.getenv("OTEL_EXPORTER_OTLP_HEADERS", "")
        for item in raw_headers.split(",") if raw_headers else []:
            key, separator, value = item.partition("=")
            if (
                not separator
                or not re.fullmatch(r"[!#$%&'*+.^_`|~0-9A-Za-z-]+", key.strip())
                or any(c in item for c in "\r\n")
            ):
                raise ValueError("Invalid OTEL_EXPORTER_OTLP_HEADERS configuration")
            headers[key.strip()] = unquote(value.strip())
        if not raw_headers and (key := os.getenv("otlp_key")):  # noqa: SIM112 -- deployment contract
            # Grafana supplies the already encoded Basic-auth payload.
            headers["Authorization"] = f"Basic {key}"
        if any(
            not value.isascii()
            or any(ord(char) < 32 or ord(char) == 127 for char in value)
            or value.startswith(" ")
            for value in headers.values()
        ):
            raise ValueError("Invalid OTLP header configuration")
        if os.getenv("OTEL_SDK_DISABLED", "false").lower() == "true":
            endpoint, headers = "", {}
        if endpoint:
            parsed = urlsplit(endpoint)
            local = parsed.hostname in {"localhost", "127.0.0.1", "::1"}
            if (
                not parsed.hostname
                or parsed.username is not None
                or parsed.password is not None
                or parsed.query
                or parsed.fragment
                or (
                    parsed.scheme != "https" and not (parsed.scheme == "http" and local)
                )
            ):
                raise ValueError(
                    "OTLP endpoint must be HTTPS (HTTP is allowed on loopback only)"
                )
            if (
                os.getenv("OTEL_EXPORTER_OTLP_PROTOCOL", "http/protobuf")
                != "http/protobuf"
            ):
                raise ValueError("Only OTLP http/protobuf is supported")
        level = os.getenv("LOG_LEVEL", "INFO").upper()
        if level not in {"DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"}:
            raise ValueError("Invalid LOG_LEVEL")
        return cls(
            os.getenv("OTEL_SERVICE_NAME", "holos-api-gateway"),
            endpoint,
            headers,
            level,
        )


class JsonFormatter(logging.Formatter):
    def __init__(self, service_name):
        super().__init__()
        self.service_name = service_name

    def format(self, record):
        context = trace.get_current_span().get_span_context()
        data = {
            "timestamp": datetime.fromtimestamp(record.created, UTC).isoformat(),
            "level": record.levelname,
            "service": self.service_name,
            "logger": record.name,
            "message": record.getMessage(),
        }
        if context.is_valid:
            data.update(
                trace_id=f"{context.trace_id:032x}", span_id=f"{context.span_id:016x}"
            )
        data.update(
            {
                key: getattr(record, key)
                for key in LOG_ATTRIBUTES
                if hasattr(record, key)
            }
        )
        # Never implicitly serialize exc_info, arbitrary extras, or request objects.
        return json.dumps(data, ensure_ascii=True)


class SafeSpanExporter(SpanExporter):
    """Keep HTTP metadata useful without exporting URLs, headers, or exception text."""

    def __init__(self, delegate):
        self.delegate = delegate

    def export(self, spans):
        cleaned = []
        for span in spans:
            attributes = {
                key: value
                for key, value in (span.attributes or {}).items()
                if key in METRIC_ATTRIBUTES
                or not key.startswith(
                    (
                        "http.",
                        "url.",
                        "client.",
                        "net.",
                        "network.",
                        "user_agent.",
                        "server.",
                        "exception.message",
                        "exception.stacktrace",
                    )
                )
            }
            # Only a route template is safe; a request target can contain user input.
            attributes.pop("http.target", None)
            name = span.name
            if span.kind == SpanKind.SERVER:
                method = attributes.get(
                    "http.request.method", attributes.get("http.method", "HTTP")
                )
                name = f"{method} {attributes.get('http.route', 'unmatched')}"
            events = [
                Event(
                    event.name,
                    {
                        "exception.type": (event.attributes or {}).get(
                            "exception.type", "Exception"
                        )
                    },
                    event.timestamp,
                )
                if event.name == "exception"
                else event
                for event in span.events
            ]
            cleaned.append(
                ReadableSpan(
                    name=name,
                    context=span.context,
                    parent=span.parent,
                    resource=span.resource,
                    attributes=attributes,
                    events=events,
                    links=span.links,
                    kind=span.kind,
                    status=Status(span.status.status_code),
                    start_time=span.start_time,
                    end_time=span.end_time,
                    instrumentation_scope=span.instrumentation_scope,
                )
            )
        return self.delegate.export(cleaned)

    def shutdown(self):
        return self.delegate.shutdown()

    def force_flush(self, timeout_millis=30000):
        return self.delegate.force_flush(timeout_millis)


class Telemetry:
    def __init__(
        self, settings, *, span_exporter=None, metric_readers=None, log_processor=None
    ):
        self.settings = settings
        resource = Resource.create(
            {
                "service.name": settings.service_name,
                "service.version": "0.1.0",
                "service.instance.id": str(uuid4()),
            }
        )
        self.traces = TracerProvider(resource=resource, shutdown_on_exit=False)
        readers = list(metric_readers or [])
        self.logs = LoggerProvider(resource=resource, shutdown_on_exit=False)
        if settings.endpoint:
            common = {"headers": settings.headers, "timeout": 5}
            base = settings.endpoint.rstrip("/")
            span_exporter = OTLPSpanExporter(endpoint=f"{base}/v1/traces", **common)
            readers.append(
                PeriodicExportingMetricReader(
                    OTLPMetricExporter(endpoint=f"{base}/v1/metrics", **common),
                    export_interval_millis=60000,
                    export_timeout_millis=5000,
                )
            )
            log_processor = BatchLogRecordProcessor(
                OTLPLogExporter(endpoint=f"{base}/v1/logs", **common)
            )
        if span_exporter is not None:
            self.traces.add_span_processor(
                BatchSpanProcessor(SafeSpanExporter(span_exporter))
            )
        if log_processor is not None:
            self.logs.add_log_record_processor(log_processor)
        self.metrics = MeterProvider(
            resource=resource,
            metric_readers=readers,
            shutdown_on_exit=False,
            views=[View(instrument_name="*", attribute_keys=METRIC_ATTRIBUTES)],
        )
        self.errors = self.metrics.get_meter("holos.http").create_counter(
            "holos.http.server.errors",
            unit="{request}",
            description="HTTP responses with status >= 400",
        )
        self.handlers = []
        self.closed = False

    def instrument(self, app):
        app.add_middleware(RequestLoggingMiddleware, telemetry=self)
        FastAPIInstrumentor.instrument_app(
            app,
            tracer_provider=self.traces,
            meter_provider=self.metrics,
            http_capture_headers_server_request=[],
            http_capture_headers_server_response=[],
            http_capture_headers_sanitize_fields=[".*"],
            exclude_spans=["receive", "send"],
        )

    def start_logging(self):
        app_logger = logging.getLogger("holos")
        self.previous_logging = (app_logger.level, app_logger.propagate)
        app_logger.setLevel(self.settings.log_level)
        app_logger.propagate = False
        console = logging.StreamHandler()
        console.setFormatter(JsonFormatter(self.settings.service_name))
        self.handlers = [console, LoggingHandler(logger_provider=self.logs)]
        for handler in self.handlers:
            app_logger.addHandler(handler)
        app_logger.info("application.started")

    def shutdown(self):
        if self.closed:
            return
        self.closed = True
        app_logger = logging.getLogger("holos")
        app_logger.info("application.stopping")
        for handler in self.handlers:
            app_logger.removeHandler(handler)
            handler.close()
        if self.handlers:
            app_logger.setLevel(self.previous_logging[0])
            app_logger.propagate = self.previous_logging[1]
        # Shutdown drains the SDK's bounded batch queues and metric reader.
        self.traces.shutdown()
        self.metrics.shutdown(timeout_millis=5000)
        self.logs.shutdown()


class RequestLoggingMiddleware:
    def __init__(self, app, telemetry):
        self.app = app
        self.telemetry = telemetry

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return await self.app(scope, receive, send)
        start = perf_counter()
        status, error_type = 500, None

        async def capture_status(message):
            nonlocal status
            if message["type"] == "http.response.start":
                status = message["status"]
            await send(message)

        try:
            await self.app(scope, receive, capture_status)
        except Exception as error:
            error_type = type(error).__name__
            raise
        finally:
            route = getattr(scope.get("route"), "path", "unmatched")
            method = scope["method"]
            if method not in {
                "GET",
                "POST",
                "PUT",
                "PATCH",
                "DELETE",
                "HEAD",
                "OPTIONS",
                "TRACE",
                "CONNECT",
            }:
                method = "_OTHER"
            attributes = {
                "http.route": route,
                "http.request.method": method,
                "http.response.status_code": status,
            }
            if status >= 400:
                self.telemetry.errors.add(1, attributes)
            fields = {
                "http_method": method,
                "http_route": route,
                "status_code": status,
                "duration_ms": round((perf_counter() - start) * 1000, 3),
            }
            if error_type:
                fields["error_type"] = error_type
            level = (
                logging.ERROR
                if status >= 500
                else logging.WARNING
                if status >= 400
                else logging.INFO
            )
            logger.log(level, "http.request.completed", extra=fields)
