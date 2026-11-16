"""Errori applicativi con codice stabile e messaggio leggibile, serializzati in modo uniforme."""
from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse


class AppError(Exception):
    status_code = 400
    code = "bad_request"

    def __init__(self, message: str, *, details: dict | None = None, code: str | None = None):
        super().__init__(message)
        self.message = message
        self.details = details or {}
        if code:
            self.code = code


class NotFound(AppError):
    status_code = 404
    code = "not_found"


class Forbidden(AppError):
    status_code = 403
    code = "forbidden"


class Conflict(AppError):
    status_code = 409
    code = "conflict"


class SafetyBlock(AppError):
    """Operazione bloccata da un controllo di sicurezza (kill switch, dati obsoleti, limiti...)."""
    status_code = 423
    code = "safety_block"


class DataUnavailable(AppError):
    """Dati affidabili mancanti: la raccomandazione/operazione viene sospesa."""
    status_code = 503
    code = "data_unavailable"


class NotYetImplemented(AppError):
    status_code = 501
    code = "not_implemented"


def _body(code: str, message: str, details: dict | None = None) -> dict:
    return {"error": {"code": code, "message": message, "details": details or {}}}


def install_error_handlers(app: FastAPI) -> None:
    @app.exception_handler(AppError)
    async def _app_error(_: Request, exc: AppError):
        return JSONResponse(status_code=exc.status_code, content=_body(exc.code, exc.message, exc.details))

    @app.exception_handler(RequestValidationError)
    async def _validation(_: Request, exc: RequestValidationError):
        errors = [{"loc": list(e.get("loc", [])), "msg": e.get("msg")} for e in exc.errors()]
        return JSONResponse(status_code=422, content=_body("validation_error", "Richiesta non valida", {"errors": errors}))
