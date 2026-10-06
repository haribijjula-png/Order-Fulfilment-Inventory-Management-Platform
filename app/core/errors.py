import logging
from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from sqlalchemy.exc import IntegrityError
from starlette.exceptions import HTTPException as HTTPExc

log = logging.getLogger("app")


class AppError(Exception):
    def __init__(self, code: str, message: str, status_code: int = 400, details=None):
        self.code, self.message, self.status_code, self.details = code, message, status_code, details


def body(code, message, details=None):
    err = {"code": code, "message": message}
    if details:
        err["details"] = details
    return {"success": False, "error": err}


def register_handlers(app: FastAPI):
    @app.exception_handler(AppError)
    async def _app(_: Request, e: AppError):
        return JSONResponse(body(e.code, e.message, e.details), status_code=e.status_code)

    @app.exception_handler(RequestValidationError)
    async def _val(_: Request, e: RequestValidationError):
        d = [{"loc": list(x["loc"]), "msg": x["msg"]} for x in e.errors()]
        return JSONResponse(body("VALIDATION_ERROR", "Request validation failed", d), status_code=422)

    @app.exception_handler(HTTPExc)
    async def _http(_: Request, e: HTTPExc):
        codes = {401: "UNAUTHORIZED", 403: "FORBIDDEN", 404: "NOT_FOUND", 405: "METHOD_NOT_ALLOWED"}
        return JSONResponse(body(codes.get(e.status_code, "HTTP_ERROR"), str(e.detail)), status_code=e.status_code)

    @app.exception_handler(IntegrityError)
    async def _integrity(_: Request, e: IntegrityError):
        log.warning("integrity error: %s", e.orig)
        return JSONResponse(body("CONFLICT", "Request conflicts with existing data"), status_code=409)

    @app.exception_handler(Exception)
    async def _any(_: Request, e: Exception):
        log.exception("unhandled error")
        return JSONResponse(body("INTERNAL_ERROR", "Internal server error"), status_code=500)
