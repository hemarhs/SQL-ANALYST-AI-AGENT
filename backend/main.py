import os
import re
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()

from fastapi import Depends, FastAPI, Header, HTTPException, Request  # noqa: E402
from starlette.concurrency import run_in_threadpool  # noqa: E402
from fastapi.staticfiles import StaticFiles  # noqa: E402
from pydantic import BaseModel, Field  # noqa: E402

import agent  # noqa: E402
import auth  # noqa: E402
import db  # noqa: E402

db.seed()
auth.init()
app = FastAPI(title="Text-to-SQL Data Analyst")

EXAMPLES = [
    "Monthly revenue for 2025",
    "Top 5 customers by total spend",
    "Revenue by product category",
    "Which country has the highest return rate?",
    "Average order value by customer segment",
]
EMAIL = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


class SignupBody(BaseModel):
    name: str = Field(min_length=1, max_length=60)
    email: str = Field(max_length=120)
    password: str = Field(min_length=8, max_length=128)


class LoginBody(BaseModel):
    email: str = Field(max_length=120)
    password: str = Field(max_length=128)


class AskBody(BaseModel):
    question: str = Field(min_length=3, max_length=500)
    source: str = "sample"  # "sample" (shared demo data) or "mine" (the user's own uploads)


def current_user(authorization: str = Header(default="")):
    uid = auth.verify_token(authorization.removeprefix("Bearer ").strip())
    user = auth.get_user(uid) if uid else None
    if not user:
        raise HTTPException(401, "Session expired. Please log in again.")
    return dict(user)


def session(uid: int) -> dict:
    user = dict(auth.get_user(uid))
    return {"token": auth.make_token(uid), "user": user}


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


def data_path(source: str, user: dict):
    if source not in ("sample", "mine"):
        raise HTTPException(422, "Unknown data source.")
    return db.resolve(source, user["id"])


@app.get("/api/schema")
def schema(source: str = "sample", user=Depends(current_user)):
    tables = db.schema_json(data_path(source, user))
    return {"tables": tables, "examples": EXAMPLES if source == "sample" else db.example_questions(tables)}


@app.post("/api/upload")
async def upload(request: Request, filename: str = "data.csv", user=Depends(current_user)):
    too_big = HTTPException(413, "File is too large (max 5 MB).")
    if int(request.headers.get("content-length") or 0) > db.MAX_UPLOAD_BYTES:
        raise too_big
    raw = await request.body()
    if len(raw) > db.MAX_UPLOAD_BYTES:
        raise too_big
    if not filename.lower().endswith((".csv", ".tsv", ".txt")):
        raise HTTPException(400, "Upload a .csv file. In Excel, use Save As, then CSV.")
    if not raw.strip():
        raise HTTPException(400, "The file is empty.")
    try:
        return await run_in_threadpool(db.ingest_csv, user["id"], filename, raw)
    except ValueError as e:
        raise HTTPException(400, str(e))


@app.delete("/api/tables/{name}")
def delete_table(name: str, user=Depends(current_user)):
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
def ask(body: AskBody, user=Depends(current_user)):
    if not os.getenv("GROQ_API_KEY"):
        raise HTTPException(500, "GROQ_API_KEY is not set. Add it to backend/.env and restart the server.")
    path = data_path(body.source, user)
    if body.source == "mine" and not db.schema_json(path):
        raise HTTPException(400, "Upload a CSV first, then ask questions about it.")
    # follow-up context: this user's own recent successful queries on the same data source
    recent = [{"question": m["question"], "sql": m["payload"]["sql"]}
              for m in auth.history(user["id"], 6)
              if not m["payload"].get("error") and m["payload"].get("source", "sample") == body.source][-3:]
    try:
        result = agent.ask(body.question, recent, body.source, str(path))
    except Exception as e:
        raise HTTPException(502, f"Agent failed: {e}")
    auth.save_message(user["id"], body.question, result)
    return result


app.mount("/", StaticFiles(directory=Path(__file__).parent.parent / "frontend", html=True), name="ui")
