"""Holos Gateway telemetry policy, applied before HTTP instrumentation starts."""

import os

# The service uses stable HTTP telemetry exclusively, in seconds.
os.environ["OTEL_SEMCONV_STABILITY_OPT_IN"] = "http"
