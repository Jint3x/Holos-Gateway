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

## Linting and formatting

Use Ruff for linting, import sorting, and formatting. Before committing Python
changes, run these checks from the repository root:

```sh
uv run --locked ruff check .
uv run --locked ruff format --check .
```

Use `uv run --locked ruff check --fix .` for safe lint fixes and
`uv run --locked ruff format .` to format code. Review automatic changes; do not
enable unsafe fixes or disable rules just to make a check pass.

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

## Testing with pytest

Use **pytest** as the project's test framework and run it through uv. Keep tests
under `tests/`, name files `test_*.py`, and use descriptive `test_*` functions.

### Before committing

- While developing, run the relevant test file or individual test for fast
  feedback, for example `uv run --locked pytest tests/test_main.py`.
- Before committing, run the full suite and the repository checks from the root:

  ```sh
  uv run --locked ruff check .
  uv run --locked ruff format --check .
  uv run --locked python -m scripts.export_openapi --check
  uv run --locked pytest
  ```

- After an API change, regenerate `docs/openapi.json` before these checks and
  include it in the same commit as the endpoint change.
- Confirm tests were collected and completed. A run collecting no tests does not
  validate a change. Report failures, skips, and any checks you could not run;
  do not claim unrun checks passed.
- Investigate failures before committing. Do not weaken assertions, add skips,
  or suppress errors just to get a passing run. If an existing failure or an
  external blocker prevents validation, report it explicitly.
- Review the final diff for accidental changes, generated artifacts, and secrets.
  Rerun affected checks if you change the code after validation.

### Test design

- Test observable behavior and contracts rather than private implementation
  details. Use clear arrange, act, and assert steps and precise assertions.
- Cover the normal path, relevant edge cases, and expected failures. For bug
  fixes, add a regression test that fails with the original behavior when
  practical.
- Use `pytest.mark.parametrize` for input/output variations. Use small fixtures
  for shared setup and teardown; avoid broad fixtures with hidden state.
- Keep unit tests fast, deterministic, and independent of execution order. Use
  `tmp_path` for temporary files and `monkeypatch` for temporary environment or
  dependency changes. Clean up resources through fixture teardown or context
  managers; do not rely on sleeps or real wall-clock timing.
- Mock external boundaries such as network services when needed, not the
  behavior being tested. Unit tests must not require live services or secrets.
  Keep integration tests requiring external services explicitly identified and
  document their prerequisites.
- Exercise FastAPI routes with `TestClient` in a context manager so lifespan
  hooks run. Assert status codes, response bodies, and relevant headers for
  successful and error responses. Keep OpenAPI contract coverage current.
- Add tests when they protect meaningful behavior; avoid tests that merely
  duplicate implementation details or assertions with no useful failure mode.
