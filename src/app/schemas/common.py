"""Shared schema base and the error envelope."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict


class StrictModel(BaseModel):
    """Base for every request/response schema: unknown fields are rejected (``422``).

    ``extra="forbid"`` on the API boundary is a security property, not pedantry: a silently
    ignored field is a field the client believes it sent.
    """

    model_config = ConfigDict(extra="forbid")


class ErrorBody(BaseModel):
    code: str
    message: str
    requestId: str | None = None


class ErrorResponse(BaseModel):
    error: ErrorBody
