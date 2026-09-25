from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse


class AppError(Exception):
    """Base for every typed exception this backend raises.

    KD8: `message` is always a hand-authored, static string -- never
    `str(exception)` or a passthrough of an underlying library/upstream
    error body, so internal topology never reaches a client.
    """

    status_code: int = 500
    error_code: str = "internal_error"
    message: str = "Something went wrong."

    def __init__(self, message: str | None = None) -> None:
        super().__init__(message or self.message)
        if message is not None:
            self.message = message


class UnauthorizedError(AppError):
    status_code = 401
    error_code = "unauthorized"
    message = "Session is missing, invalid, or expired."


class InsufficientBalanceError(AppError):
    status_code = 402
    error_code = "insufficient_balance"
    message = "Wallet balance is below the estimated cost for this job."


class WorkspaceUnprovisionedError(AppError):
    status_code = 503
    error_code = "workspace_account_unprovisioned"
    message = "Wallet is not yet provisioned. Retry after your next sign-in."


class NotFoundError(AppError):
    status_code = 404
    error_code = "not_found"
    message = "The requested resource does not exist."


class ValidationAppError(AppError):
    status_code = 422
    error_code = "validation_error"
    message = "The request could not be validated."


def _error_body(error_code: str, message: str) -> dict[str, str]:
    return {"error_code": error_code, "message": message}


def register_exception_handlers(app: FastAPI) -> None:
    @app.exception_handler(AppError)
    async def handle_app_error(_request: Request, exc: AppError) -> JSONResponse:
        return JSONResponse(
            status_code=exc.status_code,
            content=_error_body(exc.error_code, exc.message),
        )

    @app.exception_handler(Exception)
    async def handle_unexpected_error(
        _request: Request, _exc: Exception
    ) -> JSONResponse:
        # Never leak internal exception text -- see KD8.
        return JSONResponse(
            status_code=500,
            content=_error_body("internal_error", "Something went wrong."),
        )
