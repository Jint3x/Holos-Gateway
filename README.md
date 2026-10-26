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

Interactive API documentation is available at `/docs`; the OpenAPI schema is at
`/openapi.json`.

## Test

```sh
uv run --locked pytest
```
