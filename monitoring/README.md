# Production monitoring

The application exports OpenTelemetry metrics to the configured OTLP endpoint.
This directory provides a Grafana dashboard, Prometheus/Mimir recording rules,
tested alert rules, and the deployment steps needed to make them operational.
These files do not activate monitoring or notification channels by themselves.

## Deploy and connect

1. Keep the existing OTLP endpoint and authentication supplied through the runtime
   environment. Set resource attributes for each deployment, for example:

   ```sh
   OTEL_RESOURCE_ATTRIBUTES=deployment.environment.name=production,service.version=YOUR_RELEASE
   OTEL_METRIC_EXPORT_INTERVAL=30000
   OTEL_SEMCONV_STABILITY_OPT_IN=http
   ```

   The application defaults to stable HTTP conventions, a 30-second export
   interval, service `holos-api-gateway`, environment `development`, and version
   `0.1.0`. Set the environment and version explicitly for production. Resource
   attributes identify deployments; never put secrets or user information in them.
   The worker instance ID is generated independently for each worker.

2. Confirm Grafana receives the stable HTTP metrics and runtime metrics listed
   below. The rules assume the standard OTLP-to-Prometheus translation with unit
   and counter suffixes: `service.name` becomes `job`, `service.instance.id`
   becomes `instance`, and the other resource attributes are available on
   `target_info`. Keep `target_info` enabled, with `deployment_environment_name`
   and `service_version` present. The rules join these resource attributes onto
   metric series; they do not require resource attribute promotion on every metric.
   If a service namespace changes `job`, or your collector uses a different naming
   strategy, adapt the selectors and names before importing the rules.

3. Load `recording-rules.yml` and `alerts.yml` into your Prometheus or Grafana Cloud
   Mimir ruler. For Prometheus, add the files to `rule_files` in its configuration:

   ```yaml
   rule_files:
     - /etc/prometheus/holos/recording-rules.yml
     - /etc/prometheus/holos/alerts.yml
   ```

   These are Prometheus-compatible rule groups, not Grafana-managed alert
   provisioning files. Grafana Cloud must have its ruler configured to evaluate
   them. Use your existing rule management workflow and check rule evaluation
   errors. The default selectors target `holos-api-gateway` and alerts target
   environment `production`; adapt them for other services or environments.

4. Import `grafana-dashboard.json` using Grafana's dashboard import workflow and
   select the Prometheus-compatible metrics data source containing those rules.
   Filter by environment, release version, worker, and route. Allow at least two
   export samples for rates, and a few minutes for useful five-minute windows.
   Check the traffic, outcome, latency, and worker panels against known requests.
   Ratio panels display no result without traffic; missing metrics never imply
   100% success. Non-route panels intentionally ignore the route filter.

5. Configure an independent HTTP probe against the deployed HTTPS `/readyz`
   endpoint, expecting HTTP 200. Use job `holos-gateway-production`, a one-minute
   interval, and a five-second timeout. Grafana Synthetic Monitoring or a
   separately hosted blackbox exporter can provide `probe_success`.
   Select a probe that can reach the deployment (public or private as appropriate).
   A probe on the same host cannot independently detect loss of that host.
   Probe rules report failures and missing probe results separately.

6. Configure the ruler/Alertmanager notification route and recipients, test
   delivery, and confirm every alert resolves after recovery. OTLP ingestion
   credentials do not establish dashboard or rule administration access.
   Enable the production and probe alerts after their corresponding sources are
   configured; otherwise missing-data alerts will fire immediately on evaluation.

Application configuration and successful OTLP requests do not prove that data is
queryable in Grafana. Verify the deployment in Grafana before calling monitoring
operational. Local tests and the local Prometheus ingestion check do not verify
production credentials, external probes, or notification delivery.

## What the dashboard means

| Measurement | Interpretation |
| --- | --- |
| Requests/second | Completed user request volume from the HTTP duration histogram count; no extra request counter is added. |
| HTTP success | `(2xx + 3xx) / all completed user requests`; a five-minute rate ratio. |
| 4xx responses | Invalid requests, denied access, missing routes, and other client-class outcomes. Track separately; a future 429 overload response may deserve its own operational alert. |
| Server failures | `5xx / all completed user requests`; includes unhandled exceptions returned as 500. |
| p50/p95/p99 | Approximate request latency percentiles from histogram buckets. p95 means 95% of observations finish within that time. Buckets are summed before calculating percentiles across workers. |
| Active requests | In-progress HTTP requests, sampled at export time. Short peaks can occur between exports. |
| CPU cores | Rate of worker user + system CPU seconds. `1` means one fully occupied core; this is not percent of the machine or container's CPU allowance. |
| Physical memory | Resident worker memory (RSS), in bytes. Shared pages can be counted in multiple workers; container working-set measurements are preferable for container limit alerts. |
| Event loop delay | Delay in executing a one-second scheduler probe; high values can indicate blocking code or CPU pressure. |
| Thread pool usage/waiting | Borrowed default AnyIO thread-limiter tokens, total capacity, and queued tasks. This includes synchronous endpoints and work using that default limiter, not every OS thread or custom pool. |
| Worker uptime | Seconds since the worker initialized telemetry. Platform restart events are more reliable for restart alerting. |
| Telemetry freshness | Original heartbeat sample time. Recording-rule evaluations do not make an old heartbeat fresh. A heartbeat is collected even with no user traffic. |
| External availability | Probe success is 1; failure is 0. Missing results mean the probe is not reporting. |

Health probes, `/docs`, `/redoc`, and `/openapi.json` are excluded from automatic
HTTP metrics and spans, including requests with query parameters. They still
produce the existing safe request logs. The compatibility error counter
`holos.http.server.errors` still includes all responses >= 400, including health
failures; dashboard rates use the HTTP histogram consistently for numerator and
denominator. Unmatched request paths never become metric labels.

## Metrics and collection

| OpenTelemetry metric | Unit / collection |
| --- | --- |
| `http.server.request.duration` | Seconds; stable HTTP histogram with the instrumentation's standard boundaries from 5 ms to 10 s. Count supplies request volume. |
| `http.server.active_requests` | Requests; existing automatic instrumentation. |
| `process.cpu.time` | Cumulative CPU seconds with bounded `cpu.mode=user/system`; sampled by the export reader. |
| `process.memory.usage` | RSS bytes; sampled by the export reader. |
| `process.open_file_descriptor.count` | Open descriptors; absent on unsupported operating systems. |
| `holos.runtime.uptime` | Seconds since telemetry initialization. |
| `holos.runtime.event_loop.delay` | Seconds; histogram measured every second, boundaries 1 ms to 5 s. |
| `holos.runtime.thread_pool.busy/capacity/waiting` | Gauges sampled every second; brief spikes may be missed. |
| `holos.telemetry.heartbeat` | Gauge value 1, collected even while idle. |

For example, default translation produces
`http_server_request_duration_seconds_bucket`,
`http_server_request_duration_seconds_count`, `process_cpu_time_seconds_total`,
and `process_memory_usage_bytes`.

Metrics use instrument-specific attribute allowlists. HTTP labels contain reviewed
methods, route templates, status codes, and error categories; runtime metrics
contain no user dimensions. OS measurements that are unavailable are omitted
without failing requests; a safe warning is logged once per unavailable category
until it recovers. CPU/memory callbacks run during SDK collection, outside request
handling. The scheduler task starts and is cancelled with application lifespan.

Metric export runs in the background every 30 seconds by default.
`OTEL_METRIC_EXPORT_INTERVAL` accepts 1000-60000 milliseconds; smaller intervals
increase ingestion volume. Export timeouts remain five seconds, logs and traces
keep bounded batch queues, and shutdown drains providers. Telemetry backend
failure does not affect liveness or readiness. Keep at least 30 seconds of
termination grace. Trace sampling does not reduce aggregate request metrics.

## Alert defaults and response

| Alert | Initial condition | Investigate |
| --- | --- | --- |
| Server failures | >1% 5xx in a five-minute window, sustained five minutes, with at least 100 requests in that window | Correlated error traces, status codes, and recent deployments. |
| Slow requests | p95 >500 ms, sustained ten minutes, with at least 100 requests in the latest five-minute window | Slow routes, traffic, CPU, scheduler delay, and waiting work. |
| Blocked event loop | Worker p95 scheduler delay >100 ms for five minutes | Blocking calls on the event loop and CPU throttling. |
| Saturated thread pool | Worker has waiting tasks and >90% borrowed tokens for five minutes | Slow synchronous operations and downstream pressure before raising concurrency. |
| Missing telemetry | Latest source sample older than three minutes, or no series for three minutes | Application availability, export errors, authentication, network, and ruler health. |
| Failed external probe | Failure sustained two minutes; approximately three consecutive one-minute checks | DNS, TLS, ingress, and application readiness. |
| Missing external probe | No results for three minutes | Probe configuration, execution, and its monitoring pipeline. |

The minimum volume guards avoid misleading percentage alerts on tiny samples;
external probes cover low-traffic outages. These are starting thresholds, not
service-level commitments. Establish a production baseline before tuning them.
Production error/latency alerts aggregate across versions and workers, while the
dashboard allows investigating each release. Worker saturation alerts retain the
worker identity. Service freshness is not an expected-replica-count alert.

## Infrastructure monitoring after hosting is selected

Application measurements cannot reliably establish total host load, allocated
container limits, restart causes, or whether the host itself has disappeared.
Choose the relevant infrastructure integration at deployment:

- Docker/Linux containers: Grafana Alloy's cAdvisor integration (or the platform's
  existing container metrics). Measure CPU usage versus quota, throttling,
  memory working set versus limit, network I/O, and container lifecycle events.
- VM/Linux host: Alloy's Unix/node exporter integration, covering CPU, available
  memory, disk space/I/O, and network errors. Obtain process/service restart state
  from the service manager.
- Managed container platforms: use their supported integration for allocation,
  replicas, restart counts, and out-of-memory termination events.

Start with sustained CPU >80% of allocated capacity and memory >85% of the real
limit for ten minutes. Do not divide by an absent/zero quota or an unlimited memory
limit. Alert on out-of-memory terminations and repeated unexpected restarts;
set thresholds and label mappings for the chosen platform. Monitor disk space on
the host/platform rather than once per application worker. These rules are
intentionally deferred until the platform exposes those measurements.

## Validate changes

```sh
uv run --locked ruff check .
uv run --locked ruff format --check .
uv run --locked python -m scripts.export_openapi --check
uv run --locked pytest
promtool check rules monitoring/recording-rules.yml monitoring/alerts.yml
promtool test rules monitoring/rules.test.yml
uv run --locked python -m scripts.check_dashboard
```

`promtool` must be on `PATH`. CI installs a pinned, checksum-verified version and
checks the rule expressions, alert behavior, and every dashboard query. Rule
tests cover correct cross-worker aggregation, 4xx/5xx separation, low/zero
traffic, counter resets, environment isolation, stale source samples, worker
saturation, and independent probe failures. Python tests use isolated providers
and dummy local collectors, never production telemetry credentials.
