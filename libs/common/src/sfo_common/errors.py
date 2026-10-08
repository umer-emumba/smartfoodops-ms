import logging

from fastapi import FastAPI, Request, status
from fastapi.encoders import jsonable_encoder
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from sqlalchemy.exc import TimeoutError as SQLAlchemyPoolTimeout
from starlette.exceptions import HTTPException as StarletteHTTPException


class AppError(Exception):
    """Base for all app-raised errors. Carries the fields the shared error
    envelope needs: {"error": {"code", "message", "details"}}.
    """

    status_code = status.HTTP_400_BAD_REQUEST
    code = "bad_request"

    def __init__(self, message: str, details: dict | None = None) -> None:
        self.message = message
        self.details = details or {}
        super().__init__(message)


class NotFoundError(AppError):
    status_code = status.HTTP_404_NOT_FOUND
    code = "not_found"


class ValidationAppError(AppError):
    status_code = status.HTTP_422_UNPROCESSABLE_CONTENT
    code = "validation_error"


class ConflictError(AppError):
    """Optimistic-locking / uniqueness conflicts."""

    status_code = status.HTTP_409_CONFLICT
    code = "conflict"


class UnauthorizedError(AppError):
    status_code = status.HTTP_401_UNAUTHORIZED
    code = "unauthorized"


class ForbiddenError(AppError):
    status_code = status.HTTP_403_FORBIDDEN
    code = "forbidden"


class PayloadTooLargeError(AppError):
    status_code = status.HTTP_413_CONTENT_TOO_LARGE
    code = "payload_too_large"


class TooManyRequestsError(AppError):
    status_code = status.HTTP_429_TOO_MANY_REQUESTS
    code = "rate_limited"


class ServiceUnavailableError(AppError):
    """A downstream service this request depends on is unreachable or erroring."""

    status_code = status.HTTP_503_SERVICE_UNAVAILABLE
    code = "upstream_unavailable"


class BadGatewayError(AppError):
    """An upstream service answered, but not usefully (connection refused, a
    malformed response) — raised by api-gateway's proxy."""

    status_code = status.HTTP_502_BAD_GATEWAY
    code = "bad_gateway"


class GatewayTimeoutError(AppError):
    status_code = status.HTTP_504_GATEWAY_TIMEOUT
    code = "gateway_timeout"


def _envelope(code: str, message: str, details: dict | None = None) -> dict:
    return {"error": {"code": code, "message": message, "details": details or {}}}


_logger = logging.getLogger(__name__)


def register_exception_handlers(app: FastAPI) -> None:
    """Every service calls this from its app factory so all services return the
    same JSON error shape regardless of which one produced the error.
    """

    @app.exception_handler(AppError)
    async def _handle_app_error(_: Request, exc: AppError) -> JSONResponse:
        return JSONResponse(
            status_code=exc.status_code,
            content=_envelope(exc.code, exc.message, exc.details),
        )

    @app.exception_handler(StarletteHTTPException)
    async def _handle_http_exception(_: Request, exc: StarletteHTTPException) -> JSONResponse:
        return JSONResponse(
            status_code=exc.status_code,
            content=_envelope("http_error", str(exc.detail)),
        )

    @app.exception_handler(RequestValidationError)
    async def _handle_validation_error(_: Request, exc: RequestValidationError) -> JSONResponse:
        return JSONResponse(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            content=_envelope(
                "validation_error",
                "Request validation failed",
                # A custom validator's error carries the raised exception object
                # in `ctx`, which isn't JSON-serializable — stringify it, or
                # every custom-validator 422 would itself crash into a 500.
                {"errors": jsonable_encoder(exc.errors(), custom_encoder={Exception: str})},
            ),
        )

    @app.exception_handler(SQLAlchemyPoolTimeout)
    async def _handle_pool_timeout(_: Request, exc: SQLAlchemyPoolTimeout) -> JSONResponse:
        """Every DB connection in this replica is busy (see
        sfo_common.db.make_engine's short pool_timeout) — a retryable 503,
        not a 500."""
        _logger.warning("Database connection pool exhausted")
        return JSONResponse(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            content=_envelope("service_busy", "Service is busy, please retry"),
        )

    @app.exception_handler(Exception)
    async def _handle_unexpected(_: Request, exc: Exception) -> JSONResponse:
        """Last resort: an unhandled bug still returns the standard envelope
        (not Starlette's plain-text 500), without leaking internals."""
        _logger.exception("Unhandled error", exc_info=exc)
        return JSONResponse(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            content=_envelope("internal_error", "Internal server error"),
        )
