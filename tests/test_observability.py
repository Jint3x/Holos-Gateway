import json
import logging
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest
from fastapi.testclient import TestClient
from opentelemetry.proto.collector.logs.v1.logs_service_pb2 import (
    ExportLogsServiceRequest,
)
from opentelemetry.proto.collector.metrics.v1.metrics_service_pb2 import (
    ExportMetricsServiceRequest,
)
from opentelemetry.proto.collector.trace.v1.trace_service_pb2 import (
    ExportTraceServiceRequest,
)
from opentelemetry.sdk._logs.export import (
    InMemoryLogRecordExporter,
    SimpleLogRecordProcessor,
)
from opentelemetry.sdk.metrics.export import InMemoryMetricReader
from opentelemetry.sdk.trace.export import SpanExportResult
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from opentelemetry.trace import StatusCode

from app.main import create_app
from app.observability import Telemetry, TelemetrySettings


@pytest.fixture(autouse=True)
def clean_settings(monkeypatch):
    for name in (
        "OTEL_SERVICE_NAME",
        "OTEL_EXPORTER_OTLP_ENDPOINT",
        "OTEL_EXPORTER_OTLP_HEADERS",
        "OTEL_EXPORTER_OTLP_PROTOCOL",
        "OTEL_SDK_DISABLED",
        "LOG_LEVEL",
        "otlp_endpoint",
        "otlp_key",
        "OTEL_TRACES_SAMPLER",
        "OTEL_TRACES_SAMPLER_ARG",
    ):
        monkeypatch.delenv(name, raising=False)


def test_container_environment_configuration(monkeypatch):
    monkeypatch.setenv("otlp_endpoint", "https://collector.example/otlp")
    monkeypatch.setenv("otlp_key", "dummy-base64-payload")
    settings = TelemetrySettings.from_env()
    assert settings.service_name == "holos-api-gateway"
    assert settings.endpoint == "https://collector.example/otlp"
    assert settings.headers == {"Authorization": "Basic dummy-base64-payload"}
    assert "dummy-base64-payload" not in repr(settings)
    assert "collector.example" not in repr(settings)


def test_standard_environment_takes_precedence(monkeypatch):
    monkeypatch.setenv("otlp_endpoint", "https://fallback.example")
    monkeypatch.setenv("otlp_key", "unused")
    monkeypatch.setenv("OTEL_EXPORTER_OTLP_ENDPOINT", "https://collector.example/otlp")
    monkeypatch.setenv("OTEL_EXPORTER_OTLP_HEADERS", "Authorization=Basic%20dummy%3D")
    monkeypatch.setenv("OTEL_SERVICE_NAME", "custom-name")
    settings = TelemetrySettings.from_env()
    assert settings.endpoint == "https://collector.example/otlp"
    assert settings.headers == {"Authorization": "Basic dummy="}
    assert settings.service_name == "custom-name"


@pytest.mark.parametrize(
    "endpoint",
    [
        "http://remote.example",
        "https://user:password@collector.example",
        "https://collector.example?token=secret",
        "https://collector.example/#secret",
    ],
)
def test_rejects_unsafe_endpoint_without_exposing_it(monkeypatch, endpoint):
    monkeypatch.setenv("OTEL_EXPORTER_OTLP_ENDPOINT", endpoint)
    with pytest.raises(ValueError, match="OTLP endpoint must be HTTPS") as error:
        TelemetrySettings.from_env()
    assert endpoint not in str(error.value)


@pytest.mark.parametrize(
    "header",
    [
        "secret-without-equals",
        "Authorization=Basic%0Asecret",
        "bad header=secret",
        "Authorization=%20secret",
        "Authorization=secret%00",
    ],
)
def test_rejects_malformed_headers_without_exposing_them(monkeypatch, header):
    monkeypatch.setenv("OTEL_EXPORTER_OTLP_HEADERS", header)
    with pytest.raises(ValueError, match="Invalid") as error:
        TelemetrySettings.from_env()
    assert header not in str(error.value)


def test_disabled_export(monkeypatch):
    monkeypatch.setenv("OTEL_SDK_DISABLED", "true")
    monkeypatch.setenv("otlp_endpoint", "https://collector.example")
    monkeypatch.setenv("otlp_key", "secret")
    assert TelemetrySettings.from_env().endpoint == ""
    assert TelemetrySettings.from_env().headers == {}


def test_traces_metrics_logs_and_redaction(capsys):
    spans = InMemorySpanExporter()
    logs = InMemoryLogRecordExporter()
    metrics = InMemoryMetricReader()
    telemetry = Telemetry(
        TelemetrySettings(),
        span_exporter=spans,
        metric_readers=[metrics],
        log_processor=SimpleLogRecordProcessor(logs),
    )
    app = create_app(telemetry=telemetry)

    @app.get("/failure")
    def fail():
        raise RuntimeError("private-exception-value")

    before = list(logging.getLogger("holos").handlers)
    trace_id = "1234567890abcdef1234567890abcdef"
    with TestClient(app, raise_server_exceptions=False) as client:
        assert (
            client.get(
                "/",
                params={"name": "private-query-value"},
                headers={
                    "traceparent": f"00-{trace_id}-1234567890abcdef-01",
                    "Authorization": "Bearer private-auth-value",
                },
            ).status_code
            == 200
        )
        assert client.get("/", params={"name": " "}).status_code == 400
        assert client.get("/", params={"name": ""}).status_code == 422
        assert client.get("/private-path-value").status_code == 404
        assert client.get("/failure").status_code == 500
        assert telemetry.traces.force_flush()
        snapshot = metrics.get_metrics_data()
    assert logging.getLogger("holos").handlers == before

    finished = spans.get_finished_spans()
    assert len(finished) == 5
    assert finished[0].context.trace_id == int(trace_id, 16)
    assert finished[0].parent.span_id == int("1234567890abcdef", 16)
    assert finished[-1].status.status_code == StatusCode.ERROR
    assert any(event.name == "exception" for event in finished[-1].events)
    assert all(
        span.resource.attributes["service.name"] == "holos-api-gateway"
        for span in finished
    )
    span_text = "\n".join(span.to_json() for span in finished)
    assert "unmatched" in span_text

    metric_text = repr(snapshot)
    points = [
        point
        for resource in snapshot.resource_metrics
        for scope in resource.scope_metrics
        for metric in scope.metrics
        if metric.name == "holos.http.server.errors"
        for point in metric.data.data_points
    ]
    assert sum(point.value for point in points) == 4
    assert {point.attributes["http.response.status_code"] for point in points} == {
        400,
        422,
        404,
        500,
    }
    assert any(
        metric.name in {"http.server.duration", "http.server.request.duration"}
        for resource in snapshot.resource_metrics
        for scope in resource.scope_metrics
        for metric in scope.metrics
    )

    records = logs.get_finished_logs()
    requests = [
        record.log_record
        for record in records
        if record.log_record.body == "http.request.completed"
    ]
    assert len(requests) == 5
    assert requests[0].trace_id == int(trace_id, 16)
    assert requests[0].span_id == finished[0].context.span_id
    assert requests[-1].attributes["error_type"] == "RuntimeError"
    output = capsys.readouterr().err
    console = [json.loads(line) for line in output.splitlines() if line.startswith("{")]
    assert any(record.get("trace_id") == trace_id for record in console)
    for secret in (
        "private-query-value",
        "private-auth-value",
        "private-path-value",
        "private-exception-value",
    ):
        assert secret not in span_text + metric_text + output + repr(records)


def test_exporter_failure_does_not_fail_requests():
    class FailingExporter(InMemorySpanExporter):
        def export(self, spans):
            return SpanExportResult.FAILURE

    telemetry = Telemetry(TelemetrySettings(), span_exporter=FailingExporter())
    with TestClient(create_app(telemetry=telemetry)) as client:
        assert client.get("/").json() == {"message": "Hello World"}
        assert telemetry.traces.force_flush()
        assert client.get("/").status_code == 200


@pytest.fixture
def collector():
    requests = []

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            requests.append(
                (
                    self.path,
                    {key.lower(): value for key, value in self.headers.items()},
                    self.rfile.read(int(self.headers["Content-Length"])),
                )
            )
            self.send_response(200)
            self.send_header("Content-Length", "0")
            self.end_headers()

        def log_message(self, *args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}/otlp", requests
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def test_all_signals_export_as_authenticated_otlp_protobuf(collector):
    endpoint, requests = collector
    telemetry = Telemetry(
        TelemetrySettings(endpoint=endpoint, headers={"Authorization": "Basic dummy"})
    )
    with TestClient(create_app(telemetry=telemetry)) as client:
        assert client.get("/").status_code == 200
    signals = {
        "/otlp/v1/traces": (ExportTraceServiceRequest, "resource_spans"),
        "/otlp/v1/metrics": (ExportMetricsServiceRequest, "resource_metrics"),
        "/otlp/v1/logs": (ExportLogsServiceRequest, "resource_logs"),
    }
    assert set(path for path, _, _ in requests) == set(signals)
    for path, headers, body in requests:
        assert headers["authorization"] == "Basic dummy"
        assert headers["content-type"] == "application/x-protobuf"
        message_type, resource_field = signals[path]
        decoded = message_type.FromString(body)
        resources = getattr(decoded, resource_field)
        assert resources
        assert any(
            attribute.key == "service.name"
            and attribute.value.string_value == "holos-api-gateway"
            for attribute in resources[0].resource.attributes
        )
