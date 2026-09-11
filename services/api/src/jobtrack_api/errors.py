"""Error types the routers raise, and how they reach the client.

Responses carry a stable `code` and a short `detail`. The code is what a client
branches on; the detail is for a human reading a log or a toast. Neither ever contains
the internal reason for an authentication failure.
"""

from __future__ import annotations

from typing import Any

from fastapi import FastAPI, Request, status
from fastapi.responses import JSONResponse
from pydantic import BaseModel


class ApiError(Exception):
    """Base for errors that map to a specific response."""

    status_code: int = status.HTTP_500_INTERNAL_SERVER_ERROR
    code: str = "internal_error"

    def __init__(self, detail: str) -> None:
        self.detail = detail
        super().__init__(detail)

    def headers(self) -> dict[str, str]:
        return {}


class UnauthenticatedError(ApiError):
    """No usable credentials were presented."""

    status_code = status.HTTP_401_UNAUTHORIZED
    code = "unauthenticated"

    def headers(self) -> dict[str, str]:
        # Required by RFC 9110 for a 401, and it is what tells a client to re-auth
        # rather than to retry.
        return {"WWW-Authenticate": "Bearer"}


class NotFoundError(ApiError):
    """The resource does not exist, or belongs to someone else.

    Those two cases return the same response on purpose. A 403 for a row owned by
    another user would confirm that the row exists, which is a membership oracle over
    other people's data.
    """

    status_code = status.HTTP_404_NOT_FOUND
    code = "not_found"


class ConflictError(ApiError):
    """The request contradicts the current state, such as an illegal stage change."""

    status_code = status.HTTP_409_CONFLICT
    code = "conflict"


class UnprocessableInputError(ApiError):
    """The input was well-formed but could not be used.

    A link to a board with no adapter, a posting that has been taken down, a paste
    too short to be a description. The detail is written to be shown to the person
    who typed it, because in every one of those cases they are the one who can fix it.
    """

    status_code = status.HTTP_400_BAD_REQUEST
    code = "unprocessable_input"


class ErrorBody(BaseModel):
    """The shape of every error response."""

    code: str
    detail: str


def install_error_handlers(app: FastAPI) -> None:
    async def handle(request: Request, exc: Exception) -> JSONResponse:
        assert isinstance(exc, ApiError)  # noqa: S101 - narrowed by the registration below
        return JSONResponse(
            status_code=exc.status_code,
            content=ErrorBody(code=exc.code, detail=exc.detail).model_dump(),
            headers=exc.headers(),
        )

    app.add_exception_handler(ApiError, handle)


def error_responses(*errors: type[ApiError]) -> dict[int | str, dict[str, Any]]:
    """Document error responses on a route, so the generated client knows about them."""
    return {
        error.status_code: {"model": ErrorBody, "description": error.__doc__ or error.code}
        for error in errors
    }
