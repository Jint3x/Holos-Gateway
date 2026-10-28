"""Holos Gateway service defaults, applied before HTTP instrumentation starts."""

import os

# Use one HTTP convention by default. Deployments can explicitly opt into the
# legacy/dual conventions during migration; dashboards use the stable metrics.
os.environ.setdefault("OTEL_SEMCONV_STABILITY_OPT_IN", "http")
