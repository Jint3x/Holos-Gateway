# Observability

Holos Gateway uses the OpenTelemetry Python SDK, FastAPI instrumentation, and
Python's standard logging module. All three signals are sent directly to Grafana's
OTLP ingestion endpoint using **HTTP/protobuf**. No collector is required. A
collector such as Grafana Alloy can be introduced later without changing the API.

## Container environment configuration

The application reads its process environment at startup. Supply credentials at
runtime using your container platform's secret injection. Do not bake credentials
into an image, source files, build arguments, or committed environment files.
The application does not read `.env` files automatically.

| Variable | Behavior |
| --- | --- |
| `OTEL_SERVICE_NAME` | Defaults to `holos-api-gateway`. |
| `OTEL_EXPORTER_OTLP_PROTOCOL` | Defaults to `http/protobuf`; other protocols are rejected when exporting. |
| `OTEL_EXPORTER_OTLP_ENDPOINT` | Shared base endpoint, for example `https://collector.example/otlp`. Overrides `otlp_endpoint`. |
| `OTEL_EXPORTER_OTLP_HEADERS` | URL-encoded OTLP headers, such as `Authorization=Basic%20<encoded-payload>`. Overrides `otlp_key`. Treat the entire value as secret. |
| `otlp_endpoint` | Container-provided endpoint used when the standard endpoint variable is absent. |
| `otlp_key` | Container-provided **already base64-encoded Basic-auth payload**, used when standard headers are absent. The app adds `Basic ` without encoding the payload again. This is not a raw Grafana API token. |
| `OTEL_SDK_DISABLED` | Set `true` to disable remote export. Console application logging remains available. |
| `LOG_LEVEL` | Application log threshold, default `INFO`. Supports `DEBUG`, `INFO`, `WARNING`, `ERROR`, `CRITICAL`. |
| `OTEL_RESOURCE_ATTRIBUTES` | Standard SDK resource attributes; use `deployment.environment.name=production` to identify the deployment. Never put secrets here. |
| `OTEL_TRACES_SAMPLER` | Standard SDK trace sampler, default `parentbased_always_on`. For production volume control, consider `parentbased_traceidratio`. |
| `OTEL_TRACES_SAMPLER_ARG` | Root trace sampling fraction for the ratio sampler, for example `0.1` for 10%. Incoming parent sampling decisions are respected. |

Use either the standard endpoint/header variables **or** the lowercase container
variables; the standard variables take precedence independently. The lowercase
names are case sensitive. There are no literal `otlp_endpoint` or `otlp_key`
placeholder credentials in the application configuration.

The endpoint is a **base URL**: the exporter appends `/v1/traces`, `/v1/metrics`,
and `/v1/logs`, preserving any base path such as `/otlp`. Do not supply a
signal-specific URL. HTTPS and normal certificate verification are required for
remote export. Plain HTTP is accepted only on loopback for local collector tests.
A single endpoint/protocol/header configuration is used for all three signals;
signal-specific OTLP environment overrides are not supported by this application.

Without an endpoint, the application runs locally with structured console logs
and does not attempt remote export. Malformed explicit configuration fails
startup with a generic error that does not include configuration values. A
backend outage does not fail application requests: exporting occurs in background
workers with bounded queues, five-second network timeouts, and SDK retry behavior.
Sustained outages can drop telemetry when the queues fill; no durable spool is
provided. Allow at least 30 seconds of termination grace for a final drain.

## Running

From the repository root, with dependencies installed via `uv sync --locked`:

```sh
uv run --locked uvicorn app.main:create_app --factory --host 0.0.0.0 --port 8000 --no-access-log
```

Use the factory entry point so each worker owns its providers. Do not also wrap
this command in `opentelemetry-instrument`: telemetry is initialized explicitly.
The lifespan handler installs logging and drains/shuts down providers on exit.
`--no-access-log` disables Uvicorn's duplicate raw access log, which can contain
query strings. Infrastructure/reverse-proxy access logs need their own equivalent
privacy configuration. Uvicorn's own diagnostic logs are separate from the
application's `holos.*` logs.

## Signals and privacy

- **Traces:** automatic incoming FastAPI server spans, W3C trace-context
  propagation, status codes, route templates, and exception types. Internal ASGI
  send/receive spans are excluded. The export boundary removes HTTP URLs,
  arbitrary HTTP headers, client addresses, exception messages/stack traces, and
  status descriptions. Unmatched requests use the span name `METHOD unmatched`.
- **Metrics:** FastAPI's request duration histogram (whose count also supplies
  request volume), active requests, request/response sizes where supported by the
  instrumentation, and `holos.http.server.errors` for responses >= 400. Status
  codes distinguish client errors from server failures. The instrumentation's
  default HTTP semantic conventions use `http.server.duration` in milliseconds;
  stable HTTP conventions, when opted into through standard SDK configuration,
  use `http.server.request.duration` in seconds. Do not mix their units in dashboards.
  Metric views retain only vetted HTTP method, route, status, and error labels;
  legacy `http.target` labels contain route templates, never request URLs.
- **Logs:** one structured JSON event for each completed request, including
  duration, route template, status, and trace/span IDs. Requests >= 400 log at
  WARNING, and >= 500 at ERROR. Unexpected exceptions include the type, not the
  exception text. Lifecycle logs and application logs under `holos.*` go to the
  console and to OTLP with native trace correlation. Console logs go to stderr.

Resources include the service name, version, and a unique instance ID per worker.
Sampling controls traces, not aggregate request metrics or the application log
threshold. Avoid ingesting both OTLP logs and container stderr into the same
Loki stream unless duplicate ingestion is explicitly desired.

## Adding application behavior

Use `logging.getLogger("holos.<component>")` for meaningful lifecycle and business
events. Do not put tokens, headers, cookies, request/response bodies, query values,
user names, or raw exception text into messages or extra fields. Redaction at the
HTTP export boundary does not make arbitrary business events safe automatically.

For custom spans, obtain a tracer from
`request.app.state.telemetry.traces.get_tracer(__name__)`; for business metrics,
use `request.app.state.telemetry.metrics.get_meter(__name__)`. Providers are
application-scoped, not installed as process-global SDK providers. Give operations
stable names and add only reviewed low-cardinality attributes. The current metric
view drops custom attributes; deliberately extend the allowlist with tests if a
business metric requires additional labels. Instrument outgoing HTTP/database
clients when those dependencies are introduced, rather than adding them now.

## Verification

```sh
uv run --locked ruff check .
uv run --locked ruff format --check .
uv run --locked python -m scripts.export_openapi --check
uv run --locked pytest
```

Tests use isolated providers, in-memory exporters, and a local HTTP receiver with
dummy authentication. They check all three real OTLP protobuf payloads, graceful
shutdown export, trace/log correlation, errors, label safety, and redaction. They
do not require a Grafana account. OpenAPI export explicitly uses offline providers
and never initializes remote exporters.

After deployment, send a request and look for service `holos-api-gateway` in the
Grafana traces, metrics, and logs explorers. Live Grafana ingestion must be checked
in that deployment with its supplied endpoint and secret; local tests do not
establish remote access or authentication.
