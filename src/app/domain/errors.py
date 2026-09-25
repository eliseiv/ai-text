"""Domain error codes. The wire ``code`` is the contract (see ``app.errors``).

Messages are user-facing Russian: the client may show ``message`` as is, but must branch on
``code``.
"""

from __future__ import annotations

from app.errors import AppError, ConflictError, ForbiddenError, NotFoundError, ValidationFailedError


class TopicNotFoundError(NotFoundError):
    code = "topic_not_found"


class SourceNotFoundError(NotFoundError):
    code = "source_not_found"


class MessageNotFoundError(NotFoundError):
    code = "message_not_found"


class SummaryNotFoundError(NotFoundError):
    code = "summary_not_found"


class FileLinkInvalidError(NotFoundError):
    """Expired/forged link, or the file's source/topic was deleted meanwhile."""

    code = "file_link_invalid"


class SourceDeletedError(AppError):
    """410: the source existed but was deleted — the client shows «Источник удалён»."""

    status_code = 410
    code = "source_deleted"


class DemoReadOnlyError(ForbiddenError):
    code = "demo_read_only"


class LimitExceededError(ValidationFailedError):
    code = "limit_exceeded"


class UnsupportedFileError(ValidationFailedError):
    code = "unsupported_file"


class PdfRejectedError(ValidationFailedError):
    """Corrupted / password-protected / too many pages. ``code`` carries the exact reason."""

    def __init__(self, code: str, message: str) -> None:
        self.code = code
        super().__init__(message)


class InvalidUrlError(ValidationFailedError):
    code = "invalid_url"


class EmptyTextError(ValidationFailedError):
    code = "empty_text"


class NoSourcesSelectedError(ValidationFailedError):
    code = "no_sources_selected"


class NoReadySourcesError(ValidationFailedError):
    code = "no_ready_sources"


class InvalidStateError(ConflictError):
    code = "invalid_state"


class FileLinksNotConfiguredError(AppError):
    status_code = 503
    code = "file_links_not_configured"
