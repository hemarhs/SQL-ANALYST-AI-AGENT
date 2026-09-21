"""Users, sessions (signed tokens) and per-user chat history, stored in app.db."""
import base64
import hashlib
import hmac
import json
import logging
import os
import secrets
import sqlite3
import time
from pathlib import Path

STORAGE = Path(os.getenv("DATA_DIR") or Path(__file__).parent)
APP_DB = STORAGE / "app.db"
TOKEN_TTL = 7 * 24 * 3600
DEV_SECRET = "dev-secret-change-me"

log = logging.getLogger("uvicorn.error")


def _secret() -> bytes:
    return os.getenv("APP_SECRET", DEV_SECRET).encode()


def conn() -> sqlite3.Connection:
    c = sqlite3.connect(APP_DB)
    c.row_factory = sqlite3.Row
    return c


def init() -> None:
    with conn() as c:
        c.executescript(
            """
            CREATE TABLE IF NOT EXISTS users(
                id INTEGER PRIMARY KEY, email TEXT UNIQUE NOT NULL, name TEXT NOT NULL,
                pw_hash TEXT NOT NULL, created REAL NOT NULL);
            CREATE TABLE IF NOT EXISTS messages(
                id INTEGER PRIMARY KEY, user_id INTEGER NOT NULL REFERENCES users(id),
                question TEXT NOT NULL, payload TEXT NOT NULL, created REAL NOT NULL);
            CREATE INDEX IF NOT EXISTS idx_msg_user ON messages(user_id, id);
            """
        )
    if os.getenv("APP_SECRET", DEV_SECRET) == DEV_SECRET:
        log.warning("APP_SECRET is unset — using the development default. "
                    "Set APP_SECRET in production or every login token is forgeable.")


def hash_pw(pw: str) -> str:
    salt = secrets.token_bytes(16)
    h = hashlib.scrypt(pw.encode(), salt=salt, n=2**14, r=8, p=1)
    return f"{salt.hex()}:{h.hex()}"


def check_pw(pw: str, stored: str) -> bool:
    salt_hex, h_hex = stored.split(":")
    h = hashlib.scrypt(pw.encode(), salt=bytes.fromhex(salt_hex), n=2**14, r=8, p=1)
    return hmac.compare_digest(h.hex(), h_hex)


def make_token(uid: int) -> str:
    body = base64.urlsafe_b64encode(json.dumps({"uid": uid, "exp": time.time() + TOKEN_TTL}).encode()).decode()
    sig = hmac.new(_secret(), body.encode(), hashlib.sha256).hexdigest()
    return f"{body}.{sig}"


def verify_token(token: str):
    try:
        body, sig = token.split(".")
        good = hmac.new(_secret(), body.encode(), hashlib.sha256).hexdigest()
        if not hmac.compare_digest(sig, good):
            return None
        data = json.loads(base64.urlsafe_b64decode(body))
        return data["uid"] if data["exp"] > time.time() else None
    except Exception:
        return None


def create_user(name: str, email: str, pw: str):
    try:
        with conn() as c:
            cur = c.execute("INSERT INTO users(email,name,pw_hash,created) VALUES(?,?,?,?)",
                            (email.lower(), name.strip(), hash_pw(pw), time.time()))
            return cur.lastrowid
    except sqlite3.IntegrityError:
        return None


def login(email: str, pw: str):
    with conn() as c:
        row = c.execute("SELECT * FROM users WHERE email=?", (email.lower(),)).fetchone()
    return row if row and check_pw(pw, row["pw_hash"]) else None


def get_user(uid: int):
    with conn() as c:
        return c.execute("SELECT id,email,name FROM users WHERE id=?", (uid,)).fetchone()


def save_message(uid: int, question: str, payload: dict) -> None:
    with conn() as c:
        c.execute("INSERT INTO messages(user_id,question,payload,created) VALUES(?,?,?,?)",
                  (uid, question, json.dumps(payload), time.time()))


def history(uid: int, limit: int = 50) -> list[dict]:
    with conn() as c:
        rows = c.execute("SELECT id,question,payload FROM messages WHERE user_id=? ORDER BY id DESC LIMIT ?",
                         (uid, limit)).fetchall()
    return [{"id": r["id"], "question": r["question"], "payload": json.loads(r["payload"])} for r in reversed(rows)]


def clear_history(uid: int) -> None:
    with conn() as c:
        c.execute("DELETE FROM messages WHERE user_id=?", (uid,))
