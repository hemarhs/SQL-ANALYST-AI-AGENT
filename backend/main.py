"""FastAPI application: auth, datasets, and the text-to-SQL endpoint."""
import logging
import os
import re
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()

from fastapi import Depends, FastAPI, Header, HTTPException, Request  # noqa: E402
from fastapi.middleware.cors import CORSMiddleware  # noqa: E402
from fastapi.middleware.gzip import GZipMiddleware  # noqa: E402
from fastapi.responses import JSONResponse  # noqa: E402
from fastapi.staticfiles import StaticFiles  # noqa: E402
from pydantic import BaseModel, Field  # noqa: E402
from starlette.concurrency import run_in_threadpool  # noqa: E402

import agent  # noqa: E402
import auth  # noqa: E402
import db  # noqa: E402

log = logging.getLogger("uvicorn.error")

db.seed()
auth.init()

app = FastAPI(title="Ask Your Data — text-to-SQL analyst", version="2.0.0")

# CORS: lets a separately hosted frontend (e.g. Vercel) call this API from the browser.
# Auth uses an Authorization header, not cookies, so credentials stay off.
DEFAULT_ORIGINS = [
    "https://sql-analyst-ai-agent.vercel.app",
    "http://localhost:8080",
    "http://127.0.0.1:8080",
    "http://localhost:8000",
    "http://127.0.0.1:8000",
]
_env = os.getenv("ALLOWED_ORIGINS", "")
ALLOWED_ORIGINS = [o.strip().rstrip("/") for o in _env.split(",") if o.strip()] or DEFAULT_ORIGINS
app.add_middleware(
    CORSMiddleware,
    allow_origins=ALLOWED_ORIGINS,
    allow_origin_regex=r"https://.*\.vercel\.app",
    allow_methods=["GET", "POST", "DELETE", "OPTIONS"],
    allow_headers=["Authorization", "Content-Type"],
    max_age=600,
)
app.add_middleware(GZipMiddleware, minimum_size=1024)

SAMPLE_EXAMPLES = [
    "Monthly revenue for 2025",
    "Top 5 customers by total spend",
    "Revenue by product category",
    "Which country has the highest return rate?",
    "Average order value by customer segment",
]
EMAIL = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
TABLE_NAME = re.compile(r"^[A-Za-z][A-Za-z0-9_]{0,63}$")


# --------------------------------------------------------------------------- #
# models
# --------------------------------------------------------------------------- #
class SignupBody(BaseModel):
    name: str = Field(min_length=1, max_length=60)
    email: str = Field(max_length=120)
    password: str = Field(min_length=8, max_length=128)


class LoginBody(BaseModel):
    email: str = Field(max_length=120)
    password: str = Field(max_length=128)


class AskBody(BaseModel):
    question: str = Field(min_length=3, max_length=500)
    source: str = "sample"           # "sample" (shared demo data) or "mine" (the user's uploads)
    table: str | None = None         # when set, answer from this dataset only


# --------------------------------------------------------------------------- #
# helpers
# --------------------------------------------------------------------------- #
def current_user(authorization: str = Header(default="")):
    uid = auth.verify_token(authorization.removeprefix("Bearer ").strip())
    user = auth.get_user(uid) if uid else None
    if not user:
        raise HTTPException(401, "Session expired. Please log in again.")
    return dict(user)


def session(uid: int) -> dict:
    return {"token": auth.make_token(uid), "user": dict(auth.get_user(uid))}


def data_path(source: str, user: dict):
    if source not in ("sample", "mine"):
        raise HTTPException(422, "Unknown data source.")
    return db.resolve(source, user["id"])


def checked_table(path, table: str | None) -> str | None:
    """Reject anything that is not one of this user's own tables."""
    if not table:
        return None
    if not TABLE_NAME.match(table) or not db.table_exists(path, table):
        raise HTTPException(404, "That dataset no longer exists. Pick another one.")
    return table


@app.middleware("http")
async def no_stale_html(request: Request, call_next):
    """The single-page app must never be served from a stale browser cache —
    otherwise a UI change looks like it did not deploy."""
    resp = await call_next(request)
    ctype = resp.headers.get("content-type", "")
    if ctype.startswith("text/html"):
        resp.headers["Cache-Control"] = "no-cache, no-store, must-revalidate"
        resp.headers["Pragma"] = "no-cache"
        resp.headers["Expires"] = "0"
    return resp


@app.exception_handler(Exception)
async def unhandled(request: Request, exc: Exception):  # pragma: no cover - safety net
    log.exception("Unhandled error on %s", request.url.path)
    return JSONResponse({"detail": "Something went wrong on the server. Please try again."}, status_code=500)


# --------------------------------------------------------------------------- #
# routes
# --------------------------------------------------------------------------- #
@app.get("/api/health")
def health():
    return {"ok": True, "model": agent.MODEL, "llm_configured": bool(os.getenv("GROQ_API_KEY"))}


@app.post("/api/signup")
def signup(b: SignupBody):
    if not EMAIL.match(b.email):
        raise HTTPException(422, "Enter a valid email address.")
    uid = auth.create_user(b.name, b.email, b.password)
    if not uid:
        raise HTTPException(409, "An account with this email already exists. Log in instead.")
    return session(uid)


@app.post("/api/login")
def login(b: LoginBody):
    row = auth.login(b.email, b.password)
    if not row:
        raise HTTPException(401, "Incorrect email or password.")
    return session(row["id"])


@app.get("/api/me")
def me(user=Depends(current_user)):
    return user


@app.get("/api/schema")
def schema(source: str = "sample", table: str | None = None, user=Depends(current_user)):
    path = data_path(source, user)
    tables = db.schema_json(path)
    active = checked_table(path, table) if source == "mine" else None
    scoped = [t for t in tables if t["table"] == active] if active else tables
    examples = SAMPLE_EXAMPLES if source == "sample" else db.example_questions(scoped)
    return {"tables": tables, "active": active, "examples": examples,
            "limits": {"max_tables": db.MAX_TABLES, "max_mb": db.MAX_UPLOAD_BYTES // (1024 * 1024),
                       "max_rows": db.MAX_ROWS_IN, "max_cols": db.MAX_COLS}}


@app.get("/api/preview")
def preview(table: str, source: str = "mine", user=Depends(current_user)):
    path = data_path(source, user)
    if not TABLE_NAME.match(table):
        raise HTTPException(422, "Bad dataset name.")
    try:
        return db.preview(path, table)
    except ValueError as e:
        raise HTTPException(404, str(e))


@app.post("/api/upload")
async def upload(request: Request, filename: str = "data.csv", user=Depends(current_user)):
    too_big = HTTPException(413, f"File is too large (max {db.MAX_UPLOAD_BYTES // (1024 * 1024)} MB).")
    if int(request.headers.get("content-length") or 0) > db.MAX_UPLOAD_BYTES:
        raise too_big
    raw = await request.body()
    if len(raw) > db.MAX_UPLOAD_BYTES:
        raise too_big
    if not filename.lower().endswith((".csv", ".tsv", ".txt")):
        raise HTTPException(400, "Upload a .csv file. In Excel use File > Save As, then CSV.")
    if not raw.strip():
        raise HTTPException(400, "That file is empty.")
    try:
        return await run_in_threadpool(db.ingest_csv, user["id"], filename, raw)
    except ValueError as e:
        raise HTTPException(400, str(e))


@app.delete("/api/tables/{name}")
def delete_table(name: str, user=Depends(current_user)):
    if not TABLE_NAME.match(name):
        raise HTTPException(422, "Bad dataset name.")
    try:
        db.drop_table(user["id"], name)
    except ValueError as e:
        raise HTTPException(404, str(e))
    return {"ok": True}


@app.get("/api/history")
def get_history(user=Depends(current_user)):
    return auth.history(user["id"])


@app.delete("/api/history")
def clear_history(user=Depends(current_user)):
    auth.clear_history(user["id"])
    return {"ok": True}


@app.post("/api/ask")
async def ask(body: AskBody, user=Depends(current_user)):
    if not os.getenv("GROQ_API_KEY"):
        raise HTTPException(503, "The language model is not configured on the server (GROQ_API_KEY is missing).")
    path = data_path(body.source, user)
    table = checked_table(path, body.table) if body.source == "mine" else None
    if body.source == "mine" and not db.schema_json(path):
        raise HTTPException(400, "Upload a CSV first, then ask questions about it.")

    # Follow-up context: this user's own recent successful queries on the same dataset.
    recent = [
        {"question": m["question"], "sql": m["payload"]["sql"]}
        for m in auth.history(user["id"], 8)
        if not m["payload"].get("error")
        and m["payload"].get("sql")
        and m["payload"].get("source", "sample") == body.source
        and (m["payload"].get("table") or None) == table
    ][-3:]

    try:
        result = await run_in_threadpool(agent.ask, body.question, recent, body.source, str(path), table)
    except ValueError as e:
        raise HTTPException(400, str(e))
    except Exception as e:
        log.exception("Agent failure")
        detail = str(e)
        if "api_key" in detail.lower() or "authentication" in detail.lower():
            detail = "The language model rejected the API key. Check GROQ_API_KEY on the server."
        elif "rate limit" in detail.lower() or "429" in detail:
            detail = "The language model is rate limited right now. Wait a few seconds and try again."
        else:
            detail = f"The agent could not complete that request: {detail[:200]}"
        raise HTTPException(502, detail)

    auth.save_message(user["id"], body.question, result)
    return result


# The single-page frontend. Mounted last so it never shadows /api routes.
FRONTEND = Path(__file__).parent.parent / "frontend"
if FRONTEND.is_dir():
    app.mount("/", StaticFiles(directory=FRONTEND, html=True), name="ui")
