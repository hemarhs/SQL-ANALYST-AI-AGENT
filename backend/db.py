"""Sample e-commerce database, CSV ingestion, and safe read-only query execution.

Every user gets an isolated SQLite file under ``backend/data/``. Uploaded CSVs
become one table each, plus a private ``_datasets`` metadata table that keeps the
original file name, the upload time and the row/column counts so the UI can show
a real file list instead of bare table names.
"""
import csv
import io
import itertools
import os
import random
import re
import sqlite3
import time
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

# Set DATA_DIR to a mounted disk in production; on a free host without one the
# databases live next to the code and are wiped on every redeploy.
STORAGE = Path(os.getenv("DATA_DIR") or Path(__file__).parent)
STORAGE.mkdir(parents=True, exist_ok=True)

DB_PATH = STORAGE / "shop.db"
DATA_DIR = STORAGE / "data"

MAX_ROWS = 500            # rows returned to the browser per query
MAX_UPLOAD_BYTES = 5 * 1024 * 1024
MAX_ROWS_IN = 50_000      # rows accepted from one CSV
MAX_COLS = 60
MAX_TABLES = 10           # datasets a single user may keep
QUERY_TIMEOUT_S = 5
PREVIEW_ROWS = 50

META = "_datasets"        # private metadata table inside each user's database

BLOCKED = re.compile(
    r"\b(insert|update|delete|drop|alter|create|replace|attach|detach|pragma|"
    r"vacuum|reindex|truncate|grant|revoke)\b",
    re.I,
)


class UnsafeQuery(ValueError):
    """Raised when a generated query is not a plain read-only SELECT."""


# --------------------------------------------------------------------------- #
# sample database
# --------------------------------------------------------------------------- #
def seed() -> None:
    """Create the shared demo shop database once, on first boot."""
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
- Dates are ISO strings ('YYYY-MM-DD'); use strftime for month/year grouping.
- FKs: orders.customer_id->customers.id, order_items.order_id->orders.id, order_items.product_id->products.id"""


# --------------------------------------------------------------------------- #
# per-user sandboxes
# --------------------------------------------------------------------------- #
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
    """Read-only connection. Nothing that runs user/LLM SQL ever gets a writable one."""
    return sqlite3.connect(f"{Path(path).resolve().as_uri()}?mode=ro", uri=True)


def _tables(con: sqlite3.Connection) -> list[str]:
    rows = con.execute(
        "SELECT name FROM sqlite_master WHERE type='table' "
        "AND name NOT LIKE 'sqlite_%' AND name NOT LIKE '\\_%' ESCAPE '\\' ORDER BY name"
    )
    return [r[0] for r in rows]


def _ensure_meta(con: sqlite3.Connection) -> None:
    con.execute(
        f'CREATE TABLE IF NOT EXISTS "{META}"('
        '"table_name" TEXT PRIMARY KEY, "filename" TEXT, "uploaded" REAL, '
        '"row_count" INTEGER, "col_count" INTEGER, "bytes" INTEGER)'
    )


def _meta_map(path: Path) -> dict:
    """Metadata for every uploaded table, keyed by table name. Empty for the sample db."""
    try:
        con = _ro(path)
    except sqlite3.OperationalError:
        return {}
    try:
        rows = con.execute(f'SELECT * FROM "{META}"').fetchall()
        cols = [d[0] for d in con.execute(f'SELECT * FROM "{META}" LIMIT 0').description]
        return {r[0]: dict(zip(cols, r)) for r in rows}
    except sqlite3.OperationalError:
        return {}
    finally:
        con.close()


# --------------------------------------------------------------------------- #
# schema introspection
# --------------------------------------------------------------------------- #
def schema_json(path: Path = DB_PATH, only: str | None = None) -> list[dict]:
    """Tables, columns, row counts — plus upload metadata when there is any."""
    con = _ro(path)
    meta = _meta_map(path)
    out = []
    try:
        for t in _tables(con):
            if only and t != only:
                continue
            cols = [{"name": r[1], "type": r[2], "pk": bool(r[5])}
                    for r in con.execute(f'PRAGMA table_info("{t}")')]
            n = con.execute(f'SELECT COUNT(*) FROM "{t}"').fetchone()[0]
            m = meta.get(t, {})
            out.append({
                "table": t,
                "rows": n,
                "columns": cols,
                "filename": m.get("filename") or t,
                "uploaded": m.get("uploaded"),
                "bytes": m.get("bytes"),
            })
    finally:
        con.close()
    return out


def schema_text(path: Path = DB_PATH, sample_row: bool = False, only: str | None = None) -> str:
    """Compact schema for the LLM prompt: types, low-cardinality values, one sample row."""
    con = _ro(path)
    lines = []
    try:
        for tbl in schema_json(path, only):
            t, parts = tbl["table"], []
            for c in tbl["columns"][:MAX_COLS]:
                desc = f'"{c["name"]}" {c["type"]}'
                if c["type"] in ("TEXT", "DATE") and not c["name"].endswith(("name", "email", "id")):
                    vals = [r[0] for r in con.execute(
                        f'SELECT DISTINCT "{c["name"]}" FROM "{t}" WHERE "{c["name"]}" IS NOT NULL LIMIT 9')]
                    if 0 < len(vals) <= 8:
                        desc += " (values: " + ", ".join(str(v)[:24] for v in vals) + ")"
                parts.append(desc)
            label = f'  -- {tbl["rows"]} rows'
            if tbl.get("filename") and tbl["filename"] != t:
                label += f', from file "{tbl["filename"]}"'
            lines.append(f'{t}({"; ".join(parts)}){label}')
            if sample_row:
                row = con.execute(f'SELECT * FROM "{t}" LIMIT 1').fetchone()
                if row:
                    pairs = dict(zip([c["name"] for c in tbl["columns"]], row))
                    lines.append("  sample row: " + str(pairs)[:400])
    finally:
        con.close()
    return "\n".join(lines)


def preview(path: Path, table: str, limit: int = PREVIEW_ROWS) -> dict:
    """First N rows of one table, for the data-preview panel."""
    con = _ro(path)
    try:
        if table not in _tables(con):
            raise ValueError("Dataset not found.")
        cur = con.execute(f'SELECT * FROM "{table}" LIMIT ?', (max(1, min(limit, 200)),))
        cols = [d[0] for d in cur.description]
        rows = [list(r) for r in cur.fetchall()]
        total = con.execute(f'SELECT COUNT(*) FROM "{table}"').fetchone()[0]
        return {"table": table, "columns": cols, "rows": rows, "total": total}
    finally:
        con.close()


_NUMERIC = ("INTEGER", "REAL")


def example_questions(tables: list[dict]) -> list[str]:
    """Questions that actually fit the uploaded data, not generic filler."""
    if not tables:
        return []
    t = tables[0]
    name, cols = t["table"], t["columns"]
    nums = [c["name"] for c in cols if c["type"] in _NUMERIC and not c["name"].endswith("id")]
    dates = [c["name"] for c in cols if c["type"] == "DATE"]
    texts = [c["name"] for c in cols
             if c["type"] == "TEXT" and not c["name"].endswith(("id", "email", "name"))]
    cats = texts or [c["name"] for c in cols if c["type"] == "TEXT"]

    qs: list[str] = []
    if nums and cats:
        qs.append(f"Total {nums[0].replace('_', ' ')} by {cats[0].replace('_', ' ')}")
    if nums and dates:
        qs.append(f"{nums[0].replace('_', ' ').capitalize()} over time by month")
    if nums:
        qs.append(f"Top 10 rows by {nums[0].replace('_', ' ')}")
    if cats:
        qs.append(f"How many rows per {cats[0].replace('_', ' ')}?")
    qs.append(f"Show me the first 10 rows of {name}")
    if nums:
        qs.append(f"Average, minimum and maximum {nums[0].replace('_', ' ')}")
    # de-duplicate, keep order, cap at 5
    seen, out = set(), []
    for q in qs:
        if q.lower() not in seen:
            seen.add(q.lower())
            out.append(q)
    return out[:5]


# --------------------------------------------------------------------------- #
# query safety
# --------------------------------------------------------------------------- #
def _strip_noise(sql: str) -> str:
    """Remove comments and quoted text so the safety checks can't be fooled by them
    — and so a legitimate value like 'deleted' is not mistaken for a DELETE."""
    s = re.sub(r"/\*.*?\*/", " ", sql, flags=re.S)
    s = re.sub(r"--[^\n]*", " ", s)
    s = re.sub(r"'(?:''|[^'])*'", " '' ", s)
    s = re.sub(r'"(?:""|[^"])*"', ' "" ', s)
    s = re.sub(r"\[[^\]]*\]", " [] ", s)
    return s


_IDENT = re.compile(r'"((?:""|[^"])+)"')
_ALIAS = re.compile(r'\bas\s+(?:"([^"]+)"|([A-Za-z_][A-Za-z0-9_]*))', re.I)
_CTE = re.compile(r'(?:\bwith\b|,)\s*(?:"([^"]+)"|([A-Za-z_][A-Za-z0-9_]*))\s+as\s*\(', re.I)


def _known_names(con: sqlite3.Connection) -> set[str]:
    names: set[str] = set()
    for t in _tables(con):
        names.add(t.lower())
        for r in con.execute(f'PRAGMA table_info("{t}")'):
            names.add(str(r[1]).lower())
    return names


def check_identifiers(sql: str, con: sqlite3.Connection) -> None:
    """Reject invented column names.

    SQLite treats an unresolvable double-quoted name as a *string literal*, so a
    hallucinated `"revenu"` silently returns the word "revenu" in every row instead
    of failing. That would be a confidently wrong answer, so catch it here and let
    the agent's repair loop fix the query.
    """
    known = _known_names(con)
    for m in _CTE.finditer(sql):
        known.add((m.group(1) or m.group(2)).lower())
    for m in _ALIAS.finditer(sql):
        known.add((m.group(1) or m.group(2)).lower())
    for t in list(known):                      # table aliases:  FROM "orders" o
        for m in re.finditer(rf'\b{re.escape(t)}"?\s+(?:as\s+)?"?([A-Za-z_][A-Za-z0-9_]*)"?', sql, re.I):
            known.add(m.group(1).lower())

    unknown = []
    for m in _IDENT.finditer(sql):
        name = m.group(1).replace('""', '"').strip().lower()
        if name and name not in known:
            unknown.append(m.group(1))
    if unknown:
        seen = list(dict.fromkeys(unknown))[:4]
        raise UnsafeQuery(
            "Unknown column or table: " + ", ".join(f'"{u}"' for u in seen)
            + ". Use only the table and column names given in the schema."
        )


def validate(sql: str) -> str:
    s = sql.strip().rstrip(";").strip()
    if not s:
        raise UnsafeQuery("The model returned an empty query.")
    bare = _strip_noise(s)
    if ";" in bare:
        raise UnsafeQuery("Only a single statement is allowed.")
    if not re.match(r"^\s*(select|with)\b", bare, re.I):
        raise UnsafeQuery("Only SELECT queries are allowed.")
    if BLOCKED.search(bare):
        raise UnsafeQuery("Query contains a blocked keyword.")
    return s


def run_query(sql: str, path: Path = DB_PATH) -> tuple[list[str], list[list], bool]:
    """Run a validated SELECT read-only, with a wall-clock cap and a row cap."""
    s = validate(sql)
    con = _ro(path)
    if hasattr(con, "setconfig"):  # Python 3.12+: also switch off the double-quoted-string fallback
        try:
            con.setconfig(sqlite3.SQLITE_DBCONFIG_DQS_DML, False)
        except Exception:  # pragma: no cover - depends on the SQLite build
            pass
    deadline = time.time() + QUERY_TIMEOUT_S
    con.set_progress_handler(lambda: 1 if time.time() > deadline else 0, 20_000)
    try:
        check_identifiers(s, con)
        cur = con.execute(s)
        if cur.description is None:
            raise UnsafeQuery("That statement does not return any rows.")
        cols = [d[0] for d in cur.description]
        rows = [list(r) for r in cur.fetchmany(MAX_ROWS)]
        truncated = cur.fetchone() is not None
        return cols, rows, truncated
    except sqlite3.OperationalError as e:
        if "interrupted" in str(e):
            raise TimeoutError(
                f"Query took longer than {QUERY_TIMEOUT_S}s and was stopped. Try a narrower question."
            )
        raise
    finally:
        con.close()


# --------------------------------------------------------------------------- #
# CSV ingestion
# --------------------------------------------------------------------------- #
INT_RE = re.compile(r"^-?(0|[1-9]\d{0,17})$")
FLOAT_RE = re.compile(r"^-?(\d+\.?\d*|\.\d+)([eE][+-]?\d+)?$")
LEAD0 = re.compile(r"^-?0\d")
ISO_RE = re.compile(r"^\d{4}-\d{2}-\d{2}([ T]\d{2}:\d{2}(:\d{2})?)?$")
SLASH_RE = re.compile(r"^(\d{1,4})/(\d{1,2})/(\d{2,4})$")
CURRENCY = re.compile(r"^\s*[-+]?[$€£¥₹]\s?[\d,]*\.?\d+\s*$|^\s*[-+]?[\d,]*\.?\d+\s?[$€£¥₹]\s*$")
THOUSANDS = re.compile(r"^-?\d{1,3}(,\d{3})+(\.\d+)?$")
BOOLS = {"true": 1, "false": 0, "yes": 1, "no": 0, "t": 1, "f": 0, "y": 1, "n": 0}


def _snake(s: str, fallback: str) -> str:
    s = re.sub(r"[^0-9a-zA-Z]+", "_", s.strip()).strip("_").lower()[:40] or fallback
    return "c_" + s if s[0].isdigit() else s


def _clean_number(v: str) -> str | None:
    """'$1,234.50' -> '1234.50'. Returns None when the value is not a number."""
    t = v.strip()
    if CURRENCY.match(t) or THOUSANDS.match(t):
        t = re.sub(r"[,$€£¥₹\s]", "", t)
    if INT_RE.match(t) or FLOAT_RE.match(t):
        return t
    return None


def _slash_order(values: list[str]) -> str | None:
    """Work out whether d/m/y or m/d/y, using values that can only be read one way."""
    order = None
    for v in values:
        m = SLASH_RE.match(v)
        if not m:
            return None
        a, b, _ = (int(x) for x in m.groups())
        if len(m.group(1)) == 4:
            order = order or "ymd"
            if order != "ymd":
                return None
            continue
        if a > 12 and b <= 12:
            if order in ("mdy",):
                return None
            order = "dmy"
        elif b > 12 and a <= 12:
            if order in ("dmy",):
                return None
            order = "mdy"
        if a > 31 or b > 31:
            return None
    return order or "mdy"  # ambiguous: assume month-first, the most common CSV export


def _to_iso(v: str, order: str) -> str | None:
    m = SLASH_RE.match(v.strip())
    if not m:
        return None
    a, b, c = m.groups()
    try:
        if order == "ymd":
            y, mo, d = int(a), int(b), int(c)
        elif order == "dmy":
            d, mo, y = int(a), int(b), int(c)
        else:
            mo, d, y = int(a), int(b), int(c)
        if y < 100:
            y += 2000 if y < 70 else 1900
        return datetime(y, mo, d).date().isoformat()
    except ValueError:
        return None


def _profile(values: list[str]) -> tuple[str, str | None]:
    """Return (sqlite type, transform tag) for one column of raw strings."""
    vals = [v for v in values if v != ""]
    if not vals:
        return "TEXT", None
    if not any(LEAD0.match(v) for v in vals):
        if all(INT_RE.match(v) for v in vals):
            return "INTEGER", None
        if all(FLOAT_RE.match(v) for v in vals):
            return "REAL", None
        if all(_clean_number(v) is not None for v in vals):
            return "REAL", "number"
    if all(ISO_RE.match(v) for v in vals):
        return "DATE", None
    if all(SLASH_RE.match(v) for v in vals):
        order = _slash_order(vals)
        if order and all(_to_iso(v, order) for v in vals):
            return "DATE", "date:" + order
    if len(set(v.lower() for v in vals)) <= 2 and all(v.lower() in BOOLS for v in vals):
        return "INTEGER", "bool"
    return "TEXT", None


def _convert(v: str, typ: str, tag: str | None):
    if v == "":
        return None
    if tag == "bool":
        return BOOLS[v.lower()]
    if tag == "number":
        return float(_clean_number(v))
    if tag and tag.startswith("date:"):
        return _to_iso(v, tag.split(":", 1)[1])
    if typ == "INTEGER":
        return int(v)
    if typ == "REAL":
        return float(v)
    return v


def ingest_csv(uid: int, filename: str, raw: bytes) -> dict:
    """Parse a CSV/TSV upload into one table in the user's private database."""
    try:
        text = raw.decode("utf-8-sig")
    except UnicodeDecodeError:
        text = raw.decode("latin-1")
    try:
        dialect = csv.Sniffer().sniff(text[:8192], delimiters=",;\t|")
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
    data = [[c.strip() for c in (r + [""] * width)[:width]] for r in data]
    profiles = [_profile([r[i] for r in data]) for i in range(width)]
    types = [p[0] for p in profiles]

    table = _snake(Path(filename).stem, "data")
    if table == META.strip("_"):
        table = table + "_1"
    path = user_db_path(uid)
    con = sqlite3.connect(path)
    try:
        _ensure_meta(con)
        existing = _tables(con)
        replacing = table in existing
        if not replacing and len(existing) >= MAX_TABLES:
            raise ValueError(f"You can keep up to {MAX_TABLES} datasets. Remove one first.")
        con.execute(f'DROP TABLE IF EXISTS "{table}"')
        col_sql = ", ".join('"%s" %s' % (n, t) for n, t in zip(names, types))
        con.execute(f'CREATE TABLE "{table}" ({col_sql})')
        con.executemany(
            f'INSERT INTO "{table}" VALUES ({",".join("?" * width)})',
            ([_convert(v, t, tag) for v, (t, tag) in zip(r, profiles)] for r in data),
        )
        con.execute(
            f'INSERT OR REPLACE INTO "{META}" VALUES (?,?,?,?,?,?)',
            (table, Path(filename).name[:120], datetime.now(timezone.utc).timestamp(),
             len(data), width, len(raw)),
        )
        con.commit()
    except (sqlite3.DatabaseError, ValueError) as e:
        con.rollback()
        if isinstance(e, ValueError):
            raise
        raise ValueError(f"Could not read that file: {e}")
    finally:
        con.close()

    return {
        "table": table,
        "filename": Path(filename).name[:120],
        "rows": len(data),
        "replaced": replacing,
        "columns": [{"name": n, "type": t} for n, t in zip(names, types)],
    }


def drop_table(uid: int, name: str) -> None:
    con = sqlite3.connect(user_db_path(uid))
    try:
        if name not in _tables(con):
            raise ValueError("Dataset not found.")
        con.execute(f'DROP TABLE "{name}"')
        _ensure_meta(con)
        con.execute(f'DELETE FROM "{META}" WHERE "table_name"=?', (name,))
        con.commit()
    finally:
        con.close()


def table_exists(path: Path, name: str) -> bool:
    con = _ro(path)
    try:
        return name in _tables(con)
    finally:
        con.close()
