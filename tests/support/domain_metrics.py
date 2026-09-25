"""A domain metric module (DomainRegistry.metrics_module effect test)."""

from __future__ import annotations

from prometheus_client import Counter

demo_domain_metric = Counter(
    "demo_domain_metric_total", "Registered by importing this module at startup."
)
