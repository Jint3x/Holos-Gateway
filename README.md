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
uv run --locked uvicorn app.main:app --host 0.0.0.0 --port 8000 --reload
```

`GET /` returns HTTP 200 with:

```json
{"message": "Hello World"}
```

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
