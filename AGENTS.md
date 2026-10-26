# Agent instructions

## Python tooling

This project uses **uv** as its primary Python tool for interpreter management,
virtual environments, dependencies, and running commands. Use Python 3.14 and
respect `.python-version` and `pyproject.toml`.

- Install dependencies with `uv sync --locked`.
- Run Python tools through `uv run --locked`.
- Add or remove dependencies with `uv add` or `uv remove`, and commit both
  `pyproject.toml` and `uv.lock` when dependencies change.
- Do not introduce pip, Poetry, or a separate requirements file as an alternative
  dependency workflow.

## API schemas

Always create or update the OpenAPI schema when adding or changing an endpoint.
Declare request parameters, request bodies, successful responses, and expected
error responses using FastAPI route metadata and Pydantic models. Keep the
runtime documentation and committed schema consistent.

After an API change, run these commands from the repository root:

```sh
uv run --locked python -m scripts.export_openapi
uv run --locked python -m scripts.export_openapi --check
uv run --locked pytest
```

Commit the generated `docs/openapi.json` with the endpoint change. Do not edit
that generated file manually. Add or update tests for the endpoint's successful
and error responses.
