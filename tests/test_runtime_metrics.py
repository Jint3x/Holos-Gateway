import threading
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace

import anyio
import psutil
import pytest
from fastapi.testclient import TestClient
from opentelemetry.sdk.metrics.export import (
    InMemoryMetricReader,
    MetricExporter,
    MetricExportResult,
    PeriodicExportingMetricReader,
)

from app.main import create_app
from app.observability import Telemetry, TelemetrySettings


def snapshot(reader):
    return {
        metric.name: metric
        for resource in reader.get_metrics_data().resource_metrics
        for scope in resource.scope_metrics
        for metric in scope.metrics
    }


@pytest.fixture
def measured_worker():
    reader = InMemoryMetricReader()
    telemetry = Telemetry(TelemetrySettings(), metric_readers=[reader])
    try:
        yield telemetry, reader
    finally:
        telemetry.shutdown()


def test_process_and_scheduler_measurements_have_correct_units_and_dimensions(
    measured_worker,
    monkeypatch,
):
    telemetry, reader = measured_worker
    monkeypatch.setattr(
        telemetry.runtime,
        "process",
        SimpleNamespace(
            cpu_times=lambda: SimpleNamespace(user=4.5, system=1.25),
            memory_info=lambda: SimpleNamespace(rss=123456),
            num_fds=lambda: 12,
        ),
    )
    telemetry.runtime.sample_scheduler(
        10,
        10.25,
        SimpleNamespace(borrowed_tokens=3, total_tokens=40, tasks_waiting=2),
    )
    metrics = snapshot(reader)
    cpu = metrics["process.cpu.time"]
    assert cpu.unit == "s"
    assert {
        point.attributes["cpu.mode"]: point.value for point in cpu.data.data_points
    } == {"user": 4.5, "system": 1.25}
    memory = metrics["process.memory.usage"]
    assert memory.unit == "By"
    assert memory.data.data_points[0].value == 123456
    assert metrics["process.open_file_descriptor.count"].data.data_points[0].value == 12
    for name, expected in [("busy", 3), ("capacity", 40), ("waiting", 2)]:
        point = metrics[f"holos.runtime.thread_pool.{name}"].data.data_points[0]
        assert point.value == expected
        assert dict(point.attributes) == {}
    delay = metrics["holos.runtime.event_loop.delay"]
    assert delay.unit == "s"
    point = delay.data.data_points[0]
    assert point.count == 1
    assert point.sum == pytest.approx(0.25)
    assert point.explicit_bounds == (
        0.001,
        0.005,
        0.01,
        0.025,
        0.05,
        0.1,
        0.25,
        0.5,
        1,
        2.5,
        5,
    )
    assert metrics["holos.runtime.uptime"].data.data_points[0].value >= 0
    assert metrics["holos.telemetry.heartbeat"].data.data_points[0].value == 1


def test_unavailable_os_measurements_are_omitted_without_breaking_requests(
    measured_worker,
    monkeypatch,
    capsys,
):
    telemetry, reader = measured_worker

    def denied():
        raise psutil.AccessDenied(msg="private-os-error")

    monkeypatch.setattr(
        telemetry.runtime,
        "process",
        SimpleNamespace(cpu_times=denied, memory_info=denied),
    )
    with TestClient(create_app(telemetry=telemetry)) as client:
        for _ in range(2):
            metrics = snapshot(reader)
            assert "process.cpu.time" not in metrics
            assert "process.memory.usage" not in metrics
            assert "process.open_file_descriptor.count" not in metrics
            assert client.get("/").status_code == 200
    output = capsys.readouterr().err
    assert "private-os-error" not in output
    assert output.count("runtime.observation.unavailable: cpu") == 1
    assert output.count("runtime.observation.unavailable: memory") == 1


def test_concurrency_is_measured_and_returns_to_zero_after_failure(measured_worker):
    telemetry, reader = measured_worker
    app = create_app(telemetry=telemetry)
    entered, release = threading.Event(), threading.Event()

    @app.get("/blocked")
    def blocked():
        entered.set()
        if not release.wait(timeout=5):
            raise TimeoutError("test did not release blocked request")
        raise RuntimeError("synthetic failure")

    with TestClient(app, raise_server_exceptions=False) as client:
        with ThreadPoolExecutor(max_workers=1) as pool:
            request = pool.submit(client.get, "/blocked")
            try:
                assert entered.wait(timeout=5)
                metrics = snapshot(reader)
                active = metrics["http.server.active_requests"].data.data_points
                assert sum(point.value for point in active) == 1
                assert client.get("/").status_code == 200
            finally:
                release.set()
            assert request.result(timeout=5).status_code == 500
        metrics = snapshot(reader)
        assert (
            sum(
                point.value
                for point in metrics["http.server.active_requests"].data.data_points
            )
            == 0
        )


def test_probe_and_documentation_requests_do_not_inflate_user_http_metrics(
    measured_worker,
):
    telemetry, reader = measured_worker
    with TestClient(create_app(telemetry=telemetry)) as client:
        for path in (
            "/healthz",
            "/readyz",
            "/docs",
            "/redoc",
            "/openapi.json",
            "/healthz?probe=synthetic",
            "/docs?probe=synthetic",
        ):
            assert client.get(path).status_code == 200
        assert "http.server.request.duration" not in snapshot(reader)
        assert client.get("/").status_code == 200
        metrics = snapshot(reader)
        assert "http.server.duration" not in metrics
        duration = metrics["http.server.request.duration"]
        assert duration.unit == "s"
        assert sum(point.count for point in duration.data.data_points) == 1
        assert dict(duration.data.data_points[0].attributes) == {
            "http.request.method": "GET",
            "http.response.status_code": 200,
            "http.route": "/",
        }


def test_background_monitor_is_cancelled_when_lifespan_ends(
    measured_worker,
    monkeypatch,
):
    telemetry, _ = measured_worker
    started, stopped = threading.Event(), threading.Event()

    async def monitor(_):
        started.set()
        try:
            await anyio.sleep_forever()
        finally:
            stopped.set()

    monkeypatch.setattr(telemetry.runtime, "_monitor", monitor)
    with TestClient(create_app(telemetry=telemetry)) as client:
        assert started.wait(timeout=5)
        assert client.get("/readyz").status_code == 200
    assert stopped.is_set()


def test_health_exclusion_does_not_hide_nested_business_routes(measured_worker):
    telemetry, reader = measured_worker
    app = create_app(telemetry=telemetry)

    @app.get("/users/healthz")
    async def business_route():
        return {"message": "business response"}

    with TestClient(app) as client:
        assert client.get("/users/healthz").status_code == 200
        metric = snapshot(reader)["http.server.request.duration"]
        assert metric.data.data_points[0].attributes["http.route"] == "/users/healthz"


def test_failed_metric_export_does_not_break_api_or_readiness():
    class FailingExporter(MetricExporter):
        calls = 0

        def export(self, metrics_data, timeout_millis=10000, **kwargs):
            self.calls += 1
            return MetricExportResult.FAILURE

        def force_flush(self, timeout_millis=10000):
            return True

        def shutdown(self, timeout_millis=30000, **kwargs):
            pass

    exporter = FailingExporter()
    reader = PeriodicExportingMetricReader(exporter, export_interval_millis=60000)
    telemetry = Telemetry(TelemetrySettings(), metric_readers=[reader])
    with TestClient(create_app(telemetry=telemetry)) as client:
        assert client.get("/").status_code == 200
        telemetry.metrics.force_flush()
        assert exporter.calls >= 1
        assert client.get("/healthz").json() == {"status": "ok"}
        assert client.get("/readyz").json() == {"status": "ready"}
        assert client.get("/").status_code == 200


def test_resources_identify_environment_version_and_unique_workers(monkeypatch):
    monkeypatch.setenv(
        "OTEL_RESOURCE_ATTRIBUTES",
        "deployment.environment.name=staging,service.version=release-42",
    )
    resources = []
    for _ in range(2):
        reader = InMemoryMetricReader()
        telemetry = Telemetry(TelemetrySettings(), metric_readers=[reader])
        try:
            resources.append(reader.get_metrics_data().resource_metrics[0].resource)
        finally:
            telemetry.shutdown()
    assert all(
        resource.attributes["deployment.environment.name"] == "staging"
        and resource.attributes["service.version"] == "release-42"
        for resource in resources
    )
    assert (
        resources[0].attributes["service.instance.id"]
        != resources[1].attributes["service.instance.id"]
    )


@pytest.mark.parametrize("value", ["0", "-1", "999", "60001", "secret-invalid-value"])
def test_export_interval_rejects_invalid_values_without_echoing_them(
    monkeypatch,
    value,
):
    monkeypatch.setenv("OTEL_METRIC_EXPORT_INTERVAL", value)
    with pytest.raises(ValueError, match="OTEL_METRIC_EXPORT_INTERVAL") as error:
        TelemetrySettings.from_env()
    assert "secret-invalid-value" not in str(error.value)


def test_export_interval_default_and_override(monkeypatch):
    monkeypatch.delenv("OTEL_METRIC_EXPORT_INTERVAL", raising=False)
    assert TelemetrySettings.from_env().metric_export_interval_ms == 30000
    monkeypatch.setenv("OTEL_METRIC_EXPORT_INTERVAL", "15000")
    assert TelemetrySettings.from_env().metric_export_interval_ms == 15000
