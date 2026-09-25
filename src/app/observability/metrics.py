"""Prometheus metrics of the CORE.

Core-only: nothing here mentions a domain. The source's ``token_usage_total`` /
``chat_run_latency_seconds`` / ``byok_usage_share`` / ``tool_call_*`` / ``site_file_write_total``
/ ``preview_request_total`` / ``anthropic_upstream_errors_total`` / ``llm_upstream_errors_total``
are LLM-chat specific and are gone; a domain registers its own metric module
(``DomainRegistry.metrics_module``) — the default registry is process-global, so simply importing
that module registers them.

Two payment metrics exist ON PURPOSE, and merging them would be a regression:

* ``payment_events_total`` fires only when a ``payments`` ROW is created;
* ``webhook_outcome_total`` fires on EVERY webhook exit path — including ``user_not_found``,
  which happens BEFORE the row exists (webhooks never provision users). An alert built
  on ``payment_events_total`` would be dead exactly on the scenario it exists for ("payment
  received, credits never granted") — the incident that happened twice in the source.
"""

from __future__ import annotations

from prometheus_client import CONTENT_TYPE_LATEST, Counter, Gauge, Histogram, Info, generate_latest

# --- Service identity ---
# SERVICE_NAME / SERVICE_VERSION are labels of the SERVICE, not just an OpenAPI title: several
# template-born services scrape into one Prometheus and log into one aggregator, and without this
# their series/records are indistinguishable. Set once at startup (main.lifespan).
service_info = Info("service", "Identity of this service instance (name, version, environment).")

# --- Generation ---
generation_latency_seconds = Histogram(
    "generation_latency_seconds",
    "Latency of the provider call.",
    ["kind", "provider", "model"],
)
generation_total = Counter(
    "generation_total",
    "Generations by outcome (succeeded | failed | blocked | replayed). `impact` is THE alerting "
    "label (none | revenue_loss | user_blocked | upstream) — a TOTAL function of the columns of "
    "`generations` (R-OBS-3/6), never a judgement. Alerts match `impact`, never a list of "
    "statuses/reasons.",
    ["kind", "provider", "status", "impact"],
)
generation_credits_charged_total = Counter(
    "generation_credits_charged_total",
    "Credits charged for generations.",
    ["kind", "provider"],
)
generation_units_total = Counter(
    "generation_units_total",
    "Units produced by generations (images/seconds/pages/calls) — the domain-agnostic "
    "generalization of the source's token_usage_total.",
    ["kind", "unit_kind"],
)
generation_upstream_errors_total = Counter(
    "generation_upstream_errors_total",
    "Provider (upstream) errors. Bounded-enum labels only, never user content: status_code is "
    "the numeric HTTP status or 'none' for timeout/connection errors.",
    ["provider", "status_code", "error_type"],
)
generations_inflight = Gauge(
    "generations_inflight",
    "Unfinished generations (status ∈ {pending, running}), by status. Read off the partial index "
    "ix_generations_inflight — the very count the inflight-guard (step 1.5) already needs, so the "
    "metric is free. Feeds the GenerationStuck alert (TD-006).",
    ["status"],
)

# --- Policy / wallet ---
blocked_requests_total = Counter(
    "blocked_requests_total",
    "Business blocks by reason. WITHOUT `rate_limited` — that is a gateway concern (HTTP 429). "
    "`impact` is MANDATORY, including for reasons added by a domain PolicyGate (R-OBS-5): a gate "
    "refusing an ALREADY PAID operation must not label it `none`, or it repeats the billing defect "
    "where a post-payment refusal was called routine and stayed unalerted.",
    ["reason", "impact"],
)
wallet_debit_total = Counter(
    "wallet_debit_total",
    "Wallet debit attempts by result (success | fail).",
    ["result"],
)

# --- Billing ---
payment_events_total = Counter(
    "payment_events_total",
    "Outcome of writing to the payments JOURNAL (only when a payments row was created). "
    "layer ∈ {delivery, grant, none}: at status='replayed' it tells the delivery dedup (layer 1) "
    "apart from the grant dedup (layer 2) — losing layer 2 would silently double grants.",
    ["channel", "kind", "status", "layer"],
)
billing_outcome_total = Counter(
    "billing_outcome_total",
    "Outcome of EVERY billing operation, on EVERY exit path — including the paths that never "
    "reach the payments journal (user_not_found: 'paid, but not credited'; invalid_transaction / "
    "unknown_product / subscription_required in consumable IAP). Emitted together with the "
    "mandatory outcome log. op ∈ {webhook, subscription_sync, token_purchase, "
    "checkout}; result ∈ {applied, duplicate, ignored, noop, rejected, error}; `impact` is THE "
    "ALERTING label (none | lost_payment | refund_needed | upstream) — an alert matches the "
    "INVARIANT ('what does this mean for money'), never a regex over today's list of reasons; "
    "`reason` stays for diagnostics and runbook choice. ONE counter for all channels: a "
    "per-channel counter (token_purchase_total, adapty_total, …) is FORBIDDEN — by the third "
    "channel someone forgets it and a new blind spot appears, the very class of bug this "
    "single-counter design removes.",
    ["channel", "op", "result", "impact", "reason"],
)
cloudpayments_verify_errors_total = Counter(
    "cloudpayments_verify_errors_total",
    "Failures of the aggregator payment verification (reason ∈ {timeout, non_2xx, malformed}). "
    "Each sample equals one retriable 500.",
    ["reason"],
)

# --- Admin ---
admin_grant_total = Counter(
    "admin_grant_total",
    "Admin grants by kind (credits | subscription).",
    ["kind"],
)

# --- Gateway ---
http_responses_total = Counter(
    "http_responses_total",
    "HTTP responses by status — includes 429 (rate-limit observability).",
    ["status"],
)


def render_metrics() -> tuple[bytes, str]:
    return generate_latest(), CONTENT_TYPE_LATEST
