"""Export the generated API contract, or check the committed copy for drift."""

import argparse
import json
from pathlib import Path

from app.main import app

SCHEMA_PATH = Path(__file__).resolve().parents[1] / "docs" / "openapi.json"


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--check", action="store_true", help="Fail if the exported schema is stale."
    )
    args = parser.parse_args()
    schema = json.dumps(app.openapi(), indent=2, sort_keys=True) + "\n"
    if args.check:
        if not SCHEMA_PATH.exists() or SCHEMA_PATH.read_text(encoding="utf-8") != schema:
            parser.exit(1, "OpenAPI schema is stale. Run: uv run python -m scripts.export_openapi\n")
        print("OpenAPI schema is up to date.")
    else:
        SCHEMA_PATH.parent.mkdir(parents=True, exist_ok=True)
        SCHEMA_PATH.write_text(schema, encoding="utf-8")
        print(f"Exported {SCHEMA_PATH}")


if __name__ == "__main__":
    main()
