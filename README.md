# Holos Gateway

A minimal FastAPI service running on Python 3.14.

## Setup

Install [uv](https://docs.astral.sh/uv/), then run from the repository root:

```sh
uv python install 3.14
uv sync --locked
```

## Run

```sh
uv run --locked uvicorn app.main:create_app --factory --no-access-log --host 0.0.0.0 --port 8000 --reload
```

`GET /` returns HTTP 200 with:

```json
{"message": "Hello World"}
```

## Docker

Docker packages the interpreter, application, and locked dependencies together.
Both targets use the same digest-pinned Python 3.14 Debian slim base and
[`scripts/start.sh`](scripts/start.sh). Production runs as a non-root user with
runtime dependencies. Development also includes uv, pytest, and Ruff and adds
`--reload` to that startup script. Update the Python and uv image digests in
`Dockerfile` deliberately when taking upstream updates.

### Development

With Docker Engine/Desktop and Docker Compose installed:

```sh
docker compose up --build
```

Open <http://localhost:8000> or <http://localhost:8000/docs>. Editing the mounted
`app/` source triggers reload without rebuilding. The Python environment lives
at `/opt/venv`, outside the mounts. Update dependencies with the existing local
uv workflow, then rebuild after changing `pyproject.toml` or `uv.lock`.
Local uv development remains supported.

Compose forwards the current shell's `otlp_endpoint` and `otlp_key`, or standard
OTLP endpoint/header variables, at runtime. Without an endpoint, logging stays
local. To explicitly disable remote export for a development session:

```sh
OTEL_SDK_DISABLED=true docker compose up --build
```

Run checks in the development container:

```sh
docker compose exec gateway uv run --locked ruff check .
docker compose exec gateway uv run --locked ruff format --check .
docker compose exec gateway uv run --locked python -m scripts.export_openapi --check
docker compose exec gateway uv run --locked pytest
```

On Linux, match the container's development user to your host user so it can read
private source files and write the mounted schema:

```sh
export LOCAL_UID=$(id -u) LOCAL_GID=$(id -g)
docker compose up --build
```

After an API change, regenerate the mounted schema with
`docker compose exec gateway uv run --locked python -m scripts.export_openapi`.
Production always uses its fixed non-root user, independently of these
development settings. Use `docker compose down` to stop development.

### Production

```sh
docker build --target production -t holos-gateway:local .
docker run --detach --name holos-gateway \
  --publish 127.0.0.1:8000:8000 \
  --stop-timeout 30 \
  --read-only --tmpfs /tmp:rw,noexec,nosuid,size=16m \
  --cap-drop ALL --security-opt no-new-privileges \
  --env otlp_endpoint --env otlp_key \
  holos-gateway:local
```

`--env NAME` forwards an already-set variable without putting its value in the
command or image. For standard OTLP configuration, replace the two environment
arguments with `--env OTEL_EXPORTER_OTLP_ENDPOINT` and
`--env OTEL_EXPORTER_OTLP_HEADERS`. Inject credentials from the deployment
platform's secret store. Never use build arguments or committed files for them.
Avoid sharing `docker inspect` or expanded `docker compose config` output because
runtime configuration can include credentials.

The image listens on port 8000, checks `GET /` for health, and logs to stderr.
The example binds to the local machine; configure your platform's HTTPS
ingress/reverse proxy for external traffic. No persistent volume is needed.
Stop with `docker stop --time 30 holos-gateway` to drain telemetry, then remove
the stopped container with `docker rm holos-gateway`.

Verify the production image with remote export disabled:

```sh
uv run --locked python -m scripts.smoke_container holos-gateway:local
```

To verify startup with the current runtime OTLP credentials instead:

```sh
uv run --locked python -m scripts.smoke_container holos-gateway:local --with-telemetry
```

The smoke check starts a temporary container, checks HTTP responses, Docker
health, the non-root user and absence of development dependencies, then checks
graceful shutdown and removes the container. Confirm live ingestion in Grafana
separately; see [observability operations](docs/observability.md).

### CI and releases

CI runs repository checks locally and inside the development image, then builds
and smoke-tests the production image. Tests use no Grafana credentials.
Successful pushes to `main` publish `ghcr.io/jint3x/holos-gateway:latest` and a
`sha-<full-commit-sha>` tag; `v*` tags also publish their release tag. Publication
uses GitHub Actions' temporary `GITHUB_TOKEN` with package-write permission.
PR builds do not publish. Deployment is a separate platform operation; select a
commit tag or digest to deploy or roll back to a specific image.

### Builds behind an HTTPS proxy

When an environment requires additional trusted CAs, supply a combined trust
bundle as a temporary BuildKit secret:

```sh
docker build --secret id=proxy_ca,src=/path/to/ca-bundle.pem \
  --target production -t holos-gateway:local .
```

For Compose builds, supply the same secret through a local Compose override's
`build.secrets` configuration. The secret is used only while uv downloads
dependencies and is not copied into an image. At runtime, mount the trusted
bundle read-only and set `REQUESTS_CA_BUNDLE` to its container path if OTLP HTTPS
requires it. The smoke command supports `--ca-bundle /path/to/ca-bundle.pem`.
Keep certificate verification enabled.

## API contract and error responses

Interactive documentation is at `/docs` (Swagger UI) and `/redoc`. The running
service exposes its OpenAPI schema at `/openapi.json`; a generated copy is
committed as [`docs/openapi.json`](docs/openapi.json) for review and client tools.
FastAPI route declarations and Pydantic models are the source of truth. Do not
edit the generated JSON manually.

After changing an endpoint, regenerate the schema from the repository root:

```sh
uv run --locked python -m scripts.export_openapi
```

Check for drift without rewriting the file (also run in CI):

```sh
uv run --locked python -m scripts.export_openapi --check
```

The greeting endpoint accepts an optional `name` query parameter:

| Request | Status | Example response |
| --- | --- | --- |
| `GET /` | 200 | `{"message":"Hello World"}` |
| `GET /?name=Ada` | 200 | `{"message":"Hello Ada"}` |
| `GET /?name=%20%20` | 400 | `{"detail":"Name must contain a non-whitespace character."}` |
| `GET /?name=` | 422 | `{"detail":[{"type":"string_too_short","loc":["query","name"],"msg":"String should have at least 1 character","input":"","ctx":{"min_length":1}}]}` |

Names longer than 80 characters also return 422 with a `string_too_long`
validation error. Surrounding whitespace is trimmed after length validation.
The OpenAPI document describes success, application errors, and FastAPI's
structured validation errors. These are real input errors, so they can be tried
in Swagger UI without adding a simulated-failure endpoint.

## Test

```sh
uv run --locked pytest
```

## Lint and format

Ruff checks Python code, sorts imports, and formats the project. Run the same
checks used by CI:

```sh
uv run --locked ruff check .
uv run --locked ruff format --check .
```

To apply safe lint fixes and format code locally:

```sh
uv run --locked ruff check --fix .
uv run --locked ruff format .
```

Review the resulting diff before committing. Ruff targets Python 3.14 and uses
an 88-character line length. Preview rules and unsafe fixes are not enabled.

## Observability

Logs, metrics, and traces use OpenTelemetry with OTLP HTTP/protobuf export to
Grafana. The container supplies the endpoint and authentication through runtime
environment variables; no credentials are stored in the repository. The default
service name is `holos-api-gateway`. Without an endpoint, the service runs with
JSON console logs and no remote export.

See [observability configuration and operations](docs/observability.md) for the
standard `OTEL_*` variables, the `otlp_endpoint`/`otlp_key` container bindings,
sampling, privacy controls, and verification. Use the factory command above;
providers are drained and shut down with the application lifespan.
