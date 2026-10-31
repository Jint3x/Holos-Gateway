"""Verify the built production image, optionally with runtime OTLP credentials."""

import argparse
import json
import os
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path
from uuid import uuid4

TELEMETRY_VARIABLES = (
    "otlp_endpoint",
    "otlp_key",
    "OTEL_EXPORTER_OTLP_ENDPOINT",
    "OTEL_EXPORTER_OTLP_HEADERS",
    "OTEL_EXPORTER_OTLP_PROTOCOL",
    "OTEL_SERVICE_NAME",
    "OTEL_RESOURCE_ATTRIBUTES",
    "OTEL_TRACES_SAMPLER",
    "OTEL_TRACES_SAMPLER_ARG",
    "LOG_LEVEL",
)


def docker(*args):
    return subprocess.run(
        ["docker", *args],
        check=True,
        capture_output=True,
        text=True,
        timeout=90,
    ).stdout.strip()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("image", help="Locally built production image tag")
    parser.add_argument(
        "--with-telemetry",
        action="store_true",
        help="Forward the current OTLP environment by name; requires endpoint and auth",
    )
    parser.add_argument(
        "--ca-bundle", type=Path, help="Mount a runtime HTTPS trust bundle read-only"
    )
    args = parser.parse_args()
    runtime = []
    if args.with_telemetry:
        has_endpoint = any(
            os.getenv(name) for name in ("OTEL_EXPORTER_OTLP_ENDPOINT", "otlp_endpoint")
        )
        has_auth = any(
            os.getenv(name) for name in ("OTEL_EXPORTER_OTLP_HEADERS", "otlp_key")
        )
        if not has_endpoint or not has_auth:
            parser.error("Telemetry verification requires a runtime endpoint and auth")
        for name in TELEMETRY_VARIABLES:
            if name in os.environ:
                runtime.extend(["--env", name])
    else:
        runtime.extend(["--env", "OTEL_SDK_DISABLED=true"])
    if args.ca_bundle:
        bundle = args.ca_bundle.resolve(strict=True)
        runtime.extend(
            [
                "--mount",
                f"type=bind,src={bundle},dst=/run/otlp-ca.pem,readonly",
                "--env",
                "REQUESTS_CA_BUNDLE=/run/otlp-ca.pem",
            ]
        )

    name = f"holos-smoke-{uuid4().hex[:12]}"
    try:
        docker(
            "run",
            "--detach",
            "--name",
            name,
            "--publish",
            "127.0.0.1::8000",
            "--read-only",
            "--tmpfs",
            "/tmp:rw,noexec,nosuid,size=16m",
            "--cap-drop=ALL",
            "--security-opt=no-new-privileges",
            "--health-interval=1s",
            "--health-start-period=1s",
            "--stop-timeout=30",
            *runtime,
            args.image,
        )
        deadline = time.monotonic() + 30
        while time.monotonic() < deadline:
            state = json.loads(docker("inspect", "--format", "{{json .State}}", name))
            if not state["Running"]:
                raise RuntimeError("Container exited before becoming healthy")
            if state["Health"]["Status"] == "healthy":
                break
            time.sleep(0.5)
        else:
            raise RuntimeError("Container did not become healthy within 30 seconds")

        port = docker("port", name, "8000/tcp").rsplit(":", 1)[1]
        client = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        cases = (
            ("/", 200, {"message": "Hello World"}),
            ("/?name=Ada", 200, {"message": "Hello Ada"}),
            (
                "/?name=%20%20",
                400,
                {"detail": "Name must contain a non-whitespace character."},
            ),
        )
        for path, expected_status, expected_body in cases:
            try:
                response = client.open(f"http://127.0.0.1:{port}{path}", timeout=5)
            except urllib.error.HTTPError as error:
                response = error
            with response:
                if (
                    response.status != expected_status
                    or json.load(response) != expected_body
                ):
                    raise RuntimeError(
                        "Container HTTP response did not match the API contract"
                    )
        docker(
            "exec",
            name,
            "python",
            "-c",
            "import importlib.util, os; "
            "assert os.getuid() != 0; "
            "assert importlib.util.find_spec('pytest') is None; "
            "assert importlib.util.find_spec('ruff') is None",
        )
        if args.with_telemetry:
            docker(
                "exec",
                name,
                "python",
                "-c",
                "from app.observability import TelemetrySettings; "
                "settings = TelemetrySettings.from_env(); "
                "assert settings.endpoint and settings.headers",
            )
        print(
            "Healthy production image: HTTP contracts, non-root user, runtime dependencies"
        )
        docker("stop", "--time", "30", name)
        state = json.loads(docker("inspect", "--format", "{{json .State}}", name))
        logs = subprocess.run(
            ["docker", "logs", name],
            check=True,
            capture_output=True,
            text=True,
            timeout=10,
        )
        output = logs.stdout + logs.stderr
        if (
            state["ExitCode"] not in {0, 143}
            or state["OOMKilled"]
            or "application.stopping" not in output
            or "Application shutdown complete." not in output
        ):
            raise RuntimeError(
                "Container did not complete graceful application shutdown"
            )
        print("Graceful shutdown completed within the 30-second termination budget")
        if args.with_telemetry:
            print(
                "Current OTLP endpoint and auth were forwarded at runtime; values withheld"
            )
    finally:
        subprocess.run(
            ["docker", "rm", "--force", name],
            capture_output=True,
            timeout=45,
        )


if __name__ == "__main__":
    try:
        main()
    except (RuntimeError, subprocess.SubprocessError) as error:
        print(f"Container smoke check failed: {error}", file=sys.stderr)
        sys.exit(1)
