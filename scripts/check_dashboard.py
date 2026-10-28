"""Parse every dashboard PromQL expression with promtool before shipping it."""

import json
import subprocess
import tempfile
from pathlib import Path

DASHBOARD = Path(__file__).resolve().parents[1] / "monitoring/grafana-dashboard.json"


def main():
    dashboard = json.loads(DASHBOARD.read_text(encoding="utf-8"))
    replacements = {
        "$service": "holos-api-gateway",
        "$environment": ".*",
        "$version": ".*",
        "$worker": ".*",
        "$route": ".*",
        "$probe_job": "holos-gateway-production",
    }
    rules = []
    for panel in dashboard["panels"]:
        for target in panel.get("targets", []):
            expression = target["expr"]
            for variable, value in replacements.items():
                expression = expression.replace(variable, value)
            if "$" in expression:
                raise ValueError(f"Unresolved dashboard variable in {panel['title']}")
            rules.append(
                {"record": f"dashboard_query_{len(rules)}", "expr": expression}
            )
    if not rules:
        raise ValueError("Dashboard has no queries")
    # JSON is also valid YAML, the native Prometheus rule configuration format.
    with tempfile.TemporaryDirectory(prefix="holos-dashboard-") as directory:
        rules_path = Path(directory) / "queries.yml"
        rules_path.write_text(
            json.dumps({"groups": [{"name": "dashboard-validation", "rules": rules}]}),
            encoding="utf-8",
        )
        subprocess.run(["promtool", "check", "rules", str(rules_path)], check=True)
    print(f"Validated {len(rules)} dashboard queries.")


if __name__ == "__main__":
    main()
