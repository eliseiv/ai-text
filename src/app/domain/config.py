"""``DomainSettings`` — everything the sources/Q&A domain reads from env.

Registered via ``DomainRegistry.settings_cls``; the core keeps reading its own fields through the
``CoreSettings`` base. Limits here are THE contract with the iOS client: ``GET /v1/limits`` serves
exactly these values, and every validator reads the same fields — the UI can never promise a format
or a size the server then rejects (acceptance: «Обещания совпадают с форматами и лимитами»).
"""

from __future__ import annotations

from typing import Literal

from pydantic import Field

from app.config import CoreSettings, get_settings

_MB = 1024 * 1024


class DomainSettings(CoreSettings):
    # --- LLM (OpenAI) -------------------------------------------------------------------------
    # `openai` in prod. `fake` is a deterministic offline model for local runs and tests — it
    # quotes real sentences from the context, so the whole citation pipeline is exercised.
    llm_provider: Literal["openai", "fake"] = Field(default="openai", alias="LLM_PROVIDER")
    # SECRETS. Server-side only: the iOS app never sees a model key. The backup key (a second
    # account) takes over on auth/quota errors of the primary one.
    openai_api_key: str = Field(default="", alias="OPENAI_API_KEY")
    openai_api_key_backup: str = Field(default="", alias="OPENAI_API_KEY_BACKUP")
    # Optional gateway in front of the API. Empty => https://api.openai.com/v1.
    openai_base_url: str = Field(default="", alias="OPENAI_BASE_URL")
    # SECRET (credentials inside). Comma-separated proxies (socks5://user:pass@host:port, http://…)
    # tried in order on connection errors — a direct call from RU answers 403.
    openai_proxy_urls: str = Field(default="", alias="OPENAI_PROXY_URLS")
    # Answers (and summaries unless OPENAI_SUMMARY_MODEL is set).
    openai_model: str = Field(default="gpt-5.1", alias="OPENAI_MODEL")
    openai_summary_model: str = Field(default="", alias="OPENAI_SUMMARY_MODEL")
    # The support check is a yes/no judgement per claim: a small model is enough.
    openai_check_model: str = Field(default="gpt-5-mini", alias="OPENAI_CHECK_MODEL")
    # Reasoning effort of gpt-5*/o-series (ignored by other models).
    openai_reasoning_effort: str = Field(default="low", alias="OPENAI_REASONING_EFFORT")
    openai_check_reasoning_effort: str = Field(
        default="minimal", alias="OPENAI_CHECK_REASONING_EFFORT"
    )
    # Includes reasoning tokens.
    openai_max_output_tokens: int = Field(default=16000, alias="OPENAI_MAX_OUTPUT_TOKENS")
    openai_timeout_seconds: float = Field(default=180.0, alias="OPENAI_TIMEOUT_SECONDS")
    openai_max_retries: int = Field(default=2, alias="OPENAI_MAX_RETRIES")
    # Second LLM pass: does each quote actually SUPPORT its claim (not only exist)? Claims that
    # fail are dropped before the answer is shown.
    citation_support_check: bool = Field(default=True, alias="CITATION_SUPPORT_CHECK")

    # --- Pricing: credits per operation (docs/pricing.md). Capped by the core's
    # PRICING_MAX_CREDITS_PER_GENERATION.
    answer_credits: int = Field(default=1, alias="ANSWER_CREDITS")
    # A summary that fits one pass (SUMMARY_SINGLE_PASS_CHARS) costs SUMMARY_CREDITS; every extra
    # part of a long topic (SUMMARY_BATCH_CHARS each) adds SUMMARY_CREDITS_PER_EXTRA_PART.
    summary_credits: int = Field(default=3, alias="SUMMARY_CREDITS")
    summary_credits_per_extra_part: int = Field(default=2, alias="SUMMARY_CREDITS_PER_EXTRA_PART")

    # --- Context budgets ------------------------------------------------------------------
    # Q&A: all selected text goes to the model while it fits; above this, full-text retrieval
    # picks the best chunks and the answer is flagged `partialContext`.
    answer_context_chars: int = Field(default=100_000, alias="ANSWER_CONTEXT_CHARS")
    answer_history_turns: int = Field(default=6, alias="ANSWER_HISTORY_TURNS")
    question_max_chars: int = Field(default=2000, alias="QUESTION_MAX_CHARS")
    # Summary: one pass while it fits, map-reduce above; never more than max_batches maps. Text
    # beyond that is NOT silently dropped — the summary reports its coverage.
    summary_single_pass_chars: int = Field(default=150_000, alias="SUMMARY_SINGLE_PASS_CHARS")
    summary_batch_chars: int = Field(default=120_000, alias="SUMMARY_BATCH_CHARS")
    summary_max_batches: int = Field(default=8, alias="SUMMARY_MAX_BATCHES")
    chunk_target_chars: int = Field(default=1500, alias="CHUNK_TARGET_CHARS")

    # --- Import limits (served by GET /v1/limits) -----------------------------------------
    pdf_max_bytes: int = Field(default=30 * _MB, alias="PDF_MAX_BYTES")
    pdf_max_pages: int = Field(default=300, alias="PDF_MAX_PAGES")
    # A page "has a text layer" when it yields at least this many non-space characters.
    pdf_min_chars_per_page: int = Field(default=20, alias="PDF_MIN_CHARS_PER_PAGE")
    # Below this share of text pages the PDF is treated as a scan and rejected (no OCR in MVP).
    pdf_min_text_page_ratio: float = Field(default=0.3, alias="PDF_MIN_TEXT_PAGE_RATIO")
    text_max_chars: int = Field(default=300_000, alias="TEXT_MAX_CHARS")
    text_min_chars: int = Field(default=50, alias="TEXT_MIN_CHARS")
    web_max_bytes: int = Field(default=5 * _MB, alias="WEB_MAX_BYTES")
    web_timeout_seconds: float = Field(default=15.0, alias="WEB_TIMEOUT_SECONDS")
    web_max_redirects: int = Field(default=5, alias="WEB_MAX_REDIRECTS")
    web_user_agent: str = Field(
        default="Mozilla/5.0 (compatible; SourcesBot/1.0; +https://example.invalid/bot)",
        alias="WEB_USER_AGENT",
    )
    topic_max_sources: int = Field(default=20, alias="TOPIC_MAX_SOURCES")
    user_max_topics: int = Field(default=100, alias="USER_MAX_TOPICS")
    user_storage_max_bytes: int = Field(default=1024 * _MB, alias="USER_STORAGE_MAX_BYTES")

    # --- Files ----------------------------------------------------------------------------
    files_dir: str = Field(default="./.data/files", alias="FILES_DIR")
    # SECRET. HMAC key of the temporary file links. Empty => original-file links answer 503.
    files_signing_secret: str = Field(default="", alias="FILES_SIGNING_SECRET")
    file_link_ttl_seconds: int = Field(default=600, alias="FILE_LINK_TTL_SECONDS")

    # --- Deletion -------------------------------------------------------------------------
    # A deleted topic/source is invisible IMMEDIATELY; files, indexes and history are purged by the
    # worker after this delay (the agreed retention window). 0 = purge right away.
    purge_delay_seconds: int = Field(default=0, alias="PURGE_DELAY_SECONDS")

    # --- Demo -----------------------------------------------------------------------------
    demo_enabled: bool = Field(default=True, alias="DEMO_ENABLED")
    # Questions in the demo topic that do not touch the user's credits/trial.
    demo_free_questions: int = Field(default=3, alias="DEMO_FREE_QUESTIONS")

    # --- Worker ---------------------------------------------------------------------------
    worker_concurrency: int = Field(default=4, alias="WORKER_CONCURRENCY")
    worker_poll_seconds: float = Field(default=1.0, alias="WORKER_POLL_SECONDS")
    # A job whose lease expired (worker died) is picked up again.
    worker_lease_seconds: int = Field(default=1200, alias="WORKER_LEASE_SECONDS")
    worker_max_attempts: int = Field(default=3, alias="WORKER_MAX_ATTEMPTS")

    # --- Plan presentation (price/period come from config, never hardcoded in the app) ----
    # JSON: {"<productId>": {"price": "299", "currency": "RUB", "period": "P1M"}}
    plan_display_raw: str = Field(default="{}", alias="PLAN_DISPLAY")
    support_url: str = Field(default="", alias="SUPPORT_URL")
    privacy_url: str = Field(default="", alias="PRIVACY_URL")
    terms_url: str = Field(default="", alias="TERMS_URL")

    def plan_display(self) -> dict[str, dict[str, str]]:
        import json

        try:
            parsed = json.loads(self.plan_display_raw or "{}")
        except (ValueError, json.JSONDecodeError):
            return {}
        if not isinstance(parsed, dict):
            return {}
        return {
            str(k): {str(a): str(b) for a, b in v.items()}
            for k, v in parsed.items()
            if isinstance(v, dict)
        }


def domain_settings() -> DomainSettings:
    """``get_settings()`` typed for the domain. The registry guarantees the subclass."""
    settings = get_settings()
    if not isinstance(settings, DomainSettings):  # pragma: no cover - wiring error
        raise RuntimeError("DomainSettings is not registered in DomainRegistry.settings_cls")
    return settings
