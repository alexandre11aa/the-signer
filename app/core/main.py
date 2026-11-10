"""FastAPI entrypoint. Stateless: no database, no sessions, no disk writes, no body logging."""
import asyncio
import base64
import binascii
import json
import logging

from fastapi import FastAPI, Request
from fastapi.concurrency import run_in_threadpool
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field, ValidationError
from starlette.exceptions import HTTPException as StarletteHTTPException

from . import config
from .utils import UserInputError
from .signer import sign_pdf

# Only our own message-free logger; uvicorn access log is disabled in the Dockerfile.
logging.getLogger("uvicorn.access").disabled = True

# No docs/openapi endpoints exposed.
app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)


_running: set[asyncio.Task] = set()


# ---------- request models ----------
class SignRequest(BaseModel):
    pdf_b64: str
    pfx_b64: str
    password: str = Field(max_length=256)
    pages: str = Field(max_length=500)        # e.g. "1,3,5-8" or "all"
    position: int = Field(ge=1, le=9)         # 3x3 grid, 1 = top-left, 9 = bottom-right
    style: str                                # "default" | "adobe" | "custom"
    logo_b64: str | None = None               # PNG 264x264, only for style "custom"
    tz_offset_minutes: int = Field(default=0, ge=-840, le=840)  # client's offset from UTC


# ---------- helpers ----------
def error_response(code: str, status: int = 400) -> JSONResponse:
    # Only a fixed code is returned: never echo user input, exceptions or tracebacks.
    return JSONResponse({"error": code}, status_code=status, headers={"Cache-Control": "no-store"})


async def read_limited_body(request: Request) -> bytes:
    """Reads the body fully in memory, aborting as soon as the limit is exceeded."""
    declared = request.headers.get("content-length")
    if declared and declared.isdigit() and int(declared) > config.MAX_BODY_BYTES:
        raise UserInputError("payload_too_large")
    chunks, size = [], 0
    async for chunk in request.stream():
        size += len(chunk)
        if size > config.MAX_BODY_BYTES:
            raise UserInputError("payload_too_large")
        chunks.append(chunk)
    return b"".join(chunks)


def decode_b64(value: str, max_bytes: int) -> bytes:
    try:
        data = base64.b64decode(value, validate=True)
    except (binascii.Error, ValueError):
        raise UserInputError("invalid_base64")
    if not data or len(data) > max_bytes:
        raise UserInputError("invalid_file_size")
    return data


# ---------- middleware ----------
@app.middleware("http")
async def security_headers(request: Request, call_next):
    try:
        response = await call_next(request)
    except Exception:
        # Answer here instead of letting the exception reach Starlette's outer error
        # middleware: that one re-raises, and Uvicorn would then print a traceback.
        response = error_response("internal_error", 500)
    response.headers["Content-Security-Policy"] = (
        "default-src 'none'; script-src 'self'; worker-src 'self'; style-src 'self'; "
        "connect-src 'self'; img-src 'self' data: blob:; base-uri 'none'; "
        "form-action 'none'; frame-ancestors 'none'")
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["Referrer-Policy"] = "no-referrer"
    response.headers["Cache-Control"] = "no-store"
    response.headers["Strict-Transport-Security"] = "max-age=63072000; includeSubDomains"
    return response


# ---------- error handlers (generic on purpose) ----------
@app.exception_handler(RequestValidationError)
async def on_validation_error(request: Request, exc: RequestValidationError):
    # The default handler echoes the submitted input (password included). Do not.
    return error_response("invalid_request", 422)


@app.exception_handler(ValidationError)
async def on_model_error(request: Request, exc: ValidationError):
    return error_response("invalid_request", 422)


@app.exception_handler(UserInputError)
async def on_user_error(request: Request, exc: UserInputError):
    status = 413 if exc.code == "payload_too_large" else 400
    return error_response(exc.code, status)


@app.exception_handler(StarletteHTTPException)
async def on_http_error(request: Request, exc: StarletteHTTPException):
    return error_response("http_error", exc.status_code)


@app.exception_handler(Exception)
async def on_unexpected_error(request: Request, exc: Exception):
    return error_response("internal_error", 500)


# ---------- routes ----------
@app.get("/health")
async def health():
    return {"status": "ok"}


@app.post("/api/sign")
async def sign(request: Request):
    payload = SignRequest.model_validate_json(await read_limited_body(request))
    if payload.style not in config.STYLES:
        raise UserInputError("invalid_style")
    pdf = decode_b64(payload.pdf_b64, config.MAX_PDF_BYTES)
    pfx = decode_b64(payload.pfx_b64, config.MAX_PFX_BYTES)
    logo = None
    if payload.style == "custom":
        if not payload.logo_b64:
            raise UserInputError("invalid_logo")
        logo = decode_b64(payload.logo_b64, config.MAX_LOGO_BYTES)
    # The answer is a stream of JSON lines: {"done":n,"total":m} after each signed page, then
    # one last line, either {"pdf":"<base64>"} or {"error":"<code>"}.
    loop = asyncio.get_running_loop()
    queue: asyncio.Queue = asyncio.Queue()

    def report(done: int, total: int) -> None:       # called from the worker thread
        loop.call_soon_threadsafe(queue.put_nowait, {"done": done, "total": total})

    async def run() -> None:
        try:
            signed = await asyncio.wait_for(
                run_in_threadpool(sign_pdf, pdf, pfx, payload.password,
                                  payload.pages, payload.position, payload.style, logo,
                                  payload.tz_offset_minutes, report),
                timeout=config.SIGN_TIMEOUT_SECONDS)
            item = {"pdf": base64.b64encode(signed).decode("ascii")}
        except asyncio.TimeoutError:
            item = {"error": "timeout"}
        except UserInputError as exc:
            item = {"error": exc.code}
        except Exception:
            item = {"error": "internal_error"}
        await queue.put(item)

    async def stream():
        task = asyncio.create_task(run())
        _running.add(task)                            # the loop only keeps weak references
        task.add_done_callback(_running.discard)
        while True:
            item = await queue.get()
            yield json.dumps(item).encode("ascii") + b"\n"
            if "pdf" in item or "error" in item:
                break

    return StreamingResponse(stream(), media_type="application/x-ndjson",
                             headers={"X-Accel-Buffering": "no"})


# Frontend is served from the same origin, so no CORS is needed.
@app.get("/", include_in_schema=False)
async def index():
    return FileResponse(config.TEMPLATE_DIR / "index.html")


app.mount("/static", StaticFiles(directory=config.STATIC_DIR), name="static")
