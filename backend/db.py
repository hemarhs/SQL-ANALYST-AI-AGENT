"""Sample e-commerce database + safe, read-only query execution."""
import csv
import io
import itertools
import random
import re
import sqlite3
import time
from datetime import date, timedelta
from pathlib import Path

DB_PATH = Path(__file__).parent / "shop.db"
DATA_DIR = Path(__file__).parent / "data"
MAX_ROWS = 500
MAX_UPLOAD_BYTES = 5 * 1024 * 1024
MAX_ROWS_IN = 50_000
MAX_COLS = 60
MAX_TABLES = 5
QUERY_TIMEOUT_S = 5
BLOCKED = re.compile(
    r"\b(insert|update|delete|drop|alter|create|attach|detach|pragma|vacuum|reindex)\b", re.I
)


class UnsafeQuery(ValueError):
    pass


def seed() -> None:
    if DB_PATH.exists():
        return
    rnd = random.Random(7)
    con = sqlite3.connect(DB_PATH)
    con.executescript(
        """
        CREATE TABLE customers(id INTEGER PRIMARY KEY, name TEXT, email TEXT, country TEXT, segment TEXT, signed_up DATE);
        CREATE TABLE products(id INTEGER PRIMARY KEY, name TEXT, category TEXT, price REAL);
        CREATE TABLE orders(id INTEGER PRIMARY KEY, customer_id INTEGER REFERENCES customers(id), order_date DATE, status TEXT);
        CREATE TABLE order_items(id INTEGER PRIMARY KEY, order_id INTEGER REFERENCES orders(id), product_id INTEGER REFERENCES products(id), quantity INTEGER, unit_price REAL);
        """
    )
    first = "Aria Noah Mira Liam Zoya Ethan Isha Lucas Nora Arjun Emma Kabir Sofia Omar Maya Leo Ivy Rohan Chloe Sam".split()
    last = "Patel Nguyen Smith Garcia Khan Silva Kim Muller Rossi Brown Singh Lopez Chen Ali Jones".split()
    countries = ["United States"] * 5 + ["India"] * 3 + ["United Kingdom"] * 2 + ["Germany", "Canada", "Australia", "Brazil"]
    customers = []
    for i in range(1, 201):
        n = f"{rnd.choice(first)} {rnd.choice(last)}"
        signed = date(2024, 1, 1) + timedelta(days=rnd.randint(0, 540))
        customers.append((i, n, f"{n.lower().replace(' ', '.')}{i}@example.com",
                          rnd.choice(countries), rnd.choice(["consumer", "consumer", "small_business", "enterprise"]),
                          signed.isoformat()))
    con.executemany("INSERT INTO customers VALUES (?,?,?,?,?,?)", customers)

    catalog = {
        "Electronics": ["Wireless Earbuds", "Smart Watch", "Bluetooth Speaker", "USB-C Hub", "Webcam HD", "Mechanical Keyboard", "Gaming Mouse", "Power Bank"],
        "Home": ["Desk Lamp", "Air Purifier", "Coffee Grinder", "Throw Blanket", "Storage Bins", "Wall Clock", "Scented Candle", "Plant Pot Set"],
        "Fitness": ["Yoga Mat", "Resistance Bands", "Foam Roller", "Dumbbell Set", "Jump Rope", "Water Bottle", "Gym Bag", "Running Belt"],
        "Books": ["SQL Cookbook", "Deep Learning Handbook", "Data Storytelling", "Python Patterns", "System Design Primer", "Statistics Made Clear"],
        "Apparel": ["Hoodie", "Running Shoes", "Denim Jacket", "Wool Socks", "Baseball Cap", "Rain Shell"],
    }
    products, pid = [], 1
    for cat, names in catalog.items():
        base = {"Electronics": 90, "Home": 40, "Fitness": 30, "Books": 35, "Apparel": 55}[cat]
        for nm in names:
            products.append((pid, nm, cat, round(base * rnd.uniform(0.4, 2.0), 2)))
            pid += 1
    con.executemany("INSERT INTO products VALUES (?,?,?,?)", products)

    start = date(2025, 1, 1)
    oid = iid = 1
    for _ in range(2500):
        c = rnd.choice(customers)
        d = max(start + timedelta(days=int(rnd.triangular(0, 607, 500))), date.fromisoformat(c[5]))
        status = rnd.choices(["delivered", "shipped", "cancelled", "returned"], [70, 10, 8, 12])[0]
        con.execute("INSERT INTO orders VALUES (?,?,?,?)", (oid, c[0], d.isoformat(), status))
        for p in rnd.sample(products, rnd.randint(1, 4)):
            con.execute("INSERT INTO order_items VALUES (?,?,?,?,?)", (iid, oid, p[0], rnd.randint(1, 3), p[3]))
            iid += 1
        oid += 1
    con.commit()
    con.close()


SAMPLE_NOTES = """Dataset notes:
- Revenue = SUM(order_items.quantity * order_items.unit_price), excluding orders with status 'cancelled' unless asked otherwise.
- Dates are ISO strings.
- FKs: orders.customer_id->customers.id, order_items.order_id->orders.id, order_items.product_id->products.id"""


# ---------- per-user sandboxes ----------
def user_db_path(uid: int) -> Path:
    DATA_DIR.mkdir(exist_ok=True)
    return DATA_DIR / f"user_{int(uid)}.db"  # built from an integer id only


def resolve(source: str, uid: int) -> Path:
    if source == "mine":
        p = user_db_path(uid)
        if not p.exists():
            sqlite3.connect(p).close()
        return p
    return DB_PATH


def _ro(path: Path = DB_PATH) -> sqlite3.Connection:
    return sqlite3.connect(f"{Path(path).resolve().as_uri()}?mode=ro", uri=True)


def _tables(con: sqlite3.Connection) -> list[str]:
    return [r[0] for r in con.execute("SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'")]


def schema_json(path: Path = DB_PATH) -> list[dict]:
    con = _ro(path)
    out = []
    for t in _tables(con):
        cols = [{"name": r[1], "type": r[2], "pk": bool(r[5])} for r in con.execute(f'PRAGMA table_info("{t}")')]
        n = con.execute(f'SELECT COUNT(*) FROM "{t}"').fetchone()[0]
        out.append({"table": t, "rows": n, "columns": cols})
    con.close()
    return out


def schema_text(path: Path = DB_PATH, sample_row: bool = False) -> str:
    """Compact schema for the LLM prompt: low-cardinality text values, and optionally one sample row."""
    con = _ro(path)
    lines = []
    for tbl in schema_json(path):
        t, parts = tbl["table"], []
        for c in tbl["columns"][:MAX_COLS]:
            desc = f"{c['name']} {c['type']}"
            if c["type"] == "TEXT" and not c["name"].endswith(("name", "email")):
                vals = [r[0] for r in con.execute(f'SELECT DISTINCT "{c["name"]}" FROM "{t}" WHERE "{c["name"]}" IS NOT NULL LIMIT 9')]
                if 0 < len(vals) <= 8:
                    desc += f" (values: {', '.join(map(str, vals))})"
            parts.append(desc)
        lines.append(f"{t}({'; '.join(parts)})  -- {tbl['rows']} rows")
        if sample_row:
            row = con.execute(f'SELECT * FROM "{t}" LIMIT 1').fetchone()
            if row:
                lines.append("  sample row: " + str(dict(zip([c["name"] for c in tbl["columns"]], row)))[:300])
    con.close()
    return "\n".join(lines)


def example_questions(tables: list[dict]) -> list[str]:
    if not tables:
        return []
    t = tables[0]["table"]
    return [f"Show me 10 rows from {t}", f"How many rows are in {t}?", f"Summarize the numeric columns in {t}"]


def validate(sql: str) -> str:
    s = sql.strip().rstrip(";").strip()
    if ";" in s:
        raise UnsafeQuery("Only a single statement is allowed.")
    if not re.match(r"^(select|with)\b", s, re.I):
        raise UnsafeQuery("Only SELECT queries are allowed.")
    if BLOCKED.search(s):
        raise UnsafeQuery("Query contains a blocked keyword.")
    return s


def run_query(sql: str, path: Path = DB_PATH) -> tuple[list[str], list[list]]:
    s = validate(sql)
    con = _ro(path)
    deadline = time.time() + QUERY_TIMEOUT_S
    con.set_progress_handler(lambda: 1 if time.time() > deadline else 0, 20000)  # stop runaway queries
    try:
        cur = con.execute(s)
        cols = [d[0] for d in cur.description]
        rows = [list(r) for r in cur.fetchmany(MAX_ROWS)]
        return cols, rows
    except sqlite3.OperationalError as e:
        if "interrupted" in str(e):
            raise TimeoutError(f"Query took longer than {QUERY_TIMEOUT_S}s and was stopped. Try a narrower question.")
        raise
    finally:
        con.close()


# ---------- CSV ingestion ----------
INT_RE = re.compile(r"^-?(0|[1-9]\d{0,17})$")
FLOAT_RE = re.compile(r"^-?(\d+\.?\d*|\.\d+)([eE][+-]?\d+)?$")
LEAD0 = re.compile(r"^-?0\d")
DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}([ T]\d{2}:\d{2}(:\d{2})?)?$")


def _snake(s: str, fallback: str) -> str:
    s = re.sub(r"[^0-9a-zA-Z]+", "_", s.strip()).strip("_").lower()[:40] or fallback
    return "c_" + s if s[0].isdigit() else s


def _infer(values: list[str]) -> str:
    vals = [v for v in values if v != ""]
    if not vals or any(LEAD0.match(v) for v in vals):
        return "TEXT"
    if all(INT_RE.match(v) for v in vals):
        return "INTEGER"
    if all(FLOAT_RE.match(v) for v in vals):
        return "REAL"
    if all(DATE_RE.match(v) for v in vals):
        return "DATE"
    return "TEXT"


def ingest_csv(uid: int, filename: str, raw: bytes) -> dict:
    try:
        text = raw.decode("utf-8-sig")
    except UnicodeDecodeError:
        text = raw.decode("latin-1")
    try:
        dialect = csv.Sniffer().sniff(text[:4096], delimiters=",;\t|")
    except csv.Error:
        dialect = csv.excel
    rows = list(itertools.islice(csv.reader(io.StringIO(text), dialect), MAX_ROWS_IN + 2))
    rows = [r for r in rows if any(c.strip() for c in r)]
    if len(rows) < 2:
        raise ValueError("The file needs a header row and at least one data row.")
    if len(rows) - 1 > MAX_ROWS_IN:
        raise ValueError(f"Too many rows (max {MAX_ROWS_IN:,}). Upload a smaller file.")
    header, data = rows[0], rows[1:]
    if len(header) > MAX_COLS:
        raise ValueError(f"Too many columns (max {MAX_COLS}).")

    names, seen = [], set()
    for i, h in enumerate(header):
        n = _snake(h, f"col_{i + 1}")
        base, k = n, 2
        while n in seen:
            n, k = f"{base}_{k}", k + 1
        seen.add(n)
        names.append(n)
    width = len(names)
    data = [(r + [""] * width)[:width] for r in data]
    data = [[c.strip() for c in r] for r in data]
    types = [_infer([r[i] for r in data]) for i in range(width)]

    def conv(v: str, t: str):
        if v == "":
            return None
        return int(v) if t == "INTEGER" else float(v) if t == "REAL" else v

    table = _snake(Path(filename).stem, "data")
    path = user_db_path(uid)
    con = sqlite3.connect(path)
    try:
        existing = _tables(con)
        if table not in existing and len(existing) >= MAX_TABLES:
            raise ValueError(f"You can keep up to {MAX_TABLES} tables. Remove one first.")
        con.execute(f'DROP TABLE IF EXISTS "{table}"')
        col_sql = ", ".join('"%s" %s' % (n, t) for n, t in zip(names, types))
        con.execute(f'CREATE TABLE "{table}" ({col_sql})')
        con.executemany(f'INSERT INTO "{table}" VALUES ({",".join("?" * width)})',
                        ([conv(v, t) for v, t in zip(r, types)] for r in data))
        con.commit()
    finally:
        con.close()
    return {"table": table, "rows": len(data), "columns": [{"name": n, "type": t} for n, t in zip(names, types)]}


def drop_table(uid: int, name: str) -> None:
    con = sqlite3.connect(user_db_path(uid))
    try:
        if name not in _tables(con):
            raise ValueError("Table not found.")
        con.execute(f'DROP TABLE "{name}"')
        con.commit()
    finally:
        con.close()
