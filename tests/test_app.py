"""End-to-end API tests. Run from the repo root:  python tests/test_app.py"""
import os
import shutil
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "backend"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

# isolate the test run from any real database
WORK = Path(tempfile.mkdtemp(prefix="sqa-test-"))
os.environ["GROQ_API_KEY"] = "test-key"
os.environ["APP_SECRET"] = "test-secret"

import db  # noqa: E402
import auth  # noqa: E402

db.DB_PATH = WORK / "shop.db"
db.DATA_DIR = WORK / "data"
auth.APP_DB = WORK / "app.db"

import agent  # noqa: E402
from fake_llm import FakeLLM  # noqa: E402

agent._llm = FakeLLM()
agent.llm = lambda: agent._llm
agent.DB_PATH = db.DB_PATH

import main  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

main.db = db
c = TestClient(main.app)

PASS, FAIL = [], []


def check(name, cond, extra=""):
    (PASS if cond else FAIL).append(name)
    print(("  PASS  " if cond else "  FAIL  ") + name + (f"   <- {extra}" if extra and not cond else ""))


def head(t):
    print("\n" + t)
    print("-" * len(t))


SALES_CSV = b"""Order ID,Order Date,Region,Sales Rep,Units Sold,Unit Price,Revenue
1001,03/14/2025,North,Amara Okafor,12,"$1,250.00","$15,000.00"
1002,03/18/2025,South,Ben Ito,4,$980.50,"$3,922.00"
1003,04/02/2025,North,Amara Okafor,7,"$1,250.00","$8,750.00"
1004,04/11/2025,East,Chen Wei,20,$310.25,"$6,205.00"
1005,05/07/2025,West,Dara Singh,,"$410.00",
1006,05/21/2025,South,Ben Ito,9,$980.50,"$8,824.50"
"""

MESSY_CSV = "name;score;active;joined\nAnja;91;yes;2024-01-05\nBörje;77;no;2024-02-11\nCleo;83;yes;2024-03-02\n".encode("utf-8")

LONG_NAME = ("quarterly_regional_sales_performance_and_forecast_rollup_"
             "with_channel_breakdown_final_v7_2025.csv")


def main_flow():
    head("health + auth")
    r = c.get("/api/health")
    check("health responds", r.status_code == 200 and r.json()["ok"], r.text)

    r = c.post("/api/signup", json={"name": "Hema", "email": "hema@example.com", "password": "supersecret1"})
    check("signup works", r.status_code == 200, r.text)
    tok = r.json()["token"]
    H = {"Authorization": "Bearer " + tok}

    r = c.post("/api/signup", json={"name": "Hema", "email": "hema@example.com", "password": "supersecret1"})
    check("duplicate signup rejected", r.status_code == 409, r.text)

    r = c.post("/api/login", json={"email": "hema@example.com", "password": "wrongpassword"})
    check("bad password rejected", r.status_code == 401, r.text)

    r = c.post("/api/login", json={"email": "HEMA@example.com", "password": "supersecret1"})
    check("login is case-insensitive on email", r.status_code == 200, r.text)

    r = c.get("/api/me", headers={"Authorization": "Bearer garbage"})
    check("forged token rejected", r.status_code == 401, r.text)

    r = c.get("/api/schema", headers=H)
    check("sample schema has 4 tables", len(r.json()["tables"]) == 4, r.text)
    check("sample examples present", len(r.json()["examples"]) == 5)

    head("uploads")
    r = c.post("/api/upload?filename=sales_q2.csv", content=SALES_CSV, headers=H)
    check("csv upload accepted", r.status_code == 200, r.text)
    d = r.json()
    check("table named from the file", d["table"] == "sales_q2", d)
    check("all 6 rows ingested", d["rows"] == 6, d)
    types = {c_["name"]: c_["type"] for c_ in d["columns"]}
    check("currency column parsed as REAL", types.get("revenue") == "REAL", types)
    check("thousands separators parsed", types.get("unit_price") == "REAL", types)
    check("US slash dates become DATE", types.get("order_date") == "DATE", types)
    check("integers stay INTEGER", types.get("units_sold") == "INTEGER", types)
    check("text stays TEXT", types.get("sales_rep") == "TEXT", types)

    r = c.get("/api/preview?table=sales_q2", headers=H)
    p = r.json()
    check("preview returns rows", r.status_code == 200 and len(p["rows"]) == 6, r.text)
    check("date normalised to ISO", p["rows"][0][p["columns"].index("order_date")] == "2025-03-14", p["rows"][0])
    check("money value is numeric", p["rows"][0][p["columns"].index("revenue")] == 15000.0, p["rows"][0])
    check("blank cell became NULL", p["rows"][4][p["columns"].index("units_sold")] is None, p["rows"][4])

    r = c.post("/api/upload?filename=" + MESSY_CSV_NAME, content=MESSY_CSV, headers=H)
    check("semicolon-delimited csv accepted", r.status_code == 200, r.text)
    mtypes = {c_["name"]: c_["type"] for c_ in r.json()["columns"]}
    check("yes/no became INTEGER", mtypes.get("active") == "INTEGER", mtypes)
    check("iso dates detected", mtypes.get("joined") == "DATE", mtypes)

    r = c.post("/api/upload?filename=" + LONG_NAME, content=SALES_CSV, headers=H)
    check("very long filename accepted", r.status_code == 200, r.text)
    check("long filename preserved in metadata",
          r.json()["filename"] == LONG_NAME, r.json())

    r = c.get("/api/schema?source=mine", headers=H)
    tables = r.json()["tables"]
    check("three datasets listed", len(tables) == 3, [t["table"] for t in tables])
    check("metadata table is hidden", all(not t["table"].startswith("_") for t in tables))
    check("filenames surfaced", any(t["filename"] == LONG_NAME for t in tables), tables)

    r = c.post("/api/upload?filename=empty.csv", content=b"", headers=H)
    check("empty file rejected", r.status_code == 400, r.text)
    r = c.post("/api/upload?filename=notes.pdf", content=b"x,y\n1,2\n", headers=H)
    check("non-csv rejected", r.status_code == 400, r.text)
    r = c.post("/api/upload?filename=head_only.csv", content=b"a,b,c\n", headers=H)
    check("header-only file rejected", r.status_code == 400, r.text)

    head("scoped examples")
    r = c.get("/api/schema?source=mine&table=sales_q2", headers=H)
    ex = r.json()["examples"]
    check("examples reference the real columns",
          any("revenue" in e.lower() or "region" in e.lower() for e in ex), ex)
    check("active table echoed", r.json()["active"] == "sales_q2", r.json()["active"])
    r = c.get("/api/schema?source=mine&table=nope", headers=H)
    check("unknown table rejected", r.status_code == 404, r.text)

    head("asking questions")
    r = c.post("/api/ask", json={"question": "How many rows are there?", "source": "mine", "table": "sales_q2"}, headers=H)
    a = r.json()
    check("ask succeeds on my data", r.status_code == 200, r.text)
    check("returns rows", a["rows"] == [[6]], a.get("rows"))
    check("records the active table", a["table"] == "sales_q2", a.get("table"))
    check("trace recorded", len(a["trace"]) >= 3, a.get("trace"))

    r = c.post("/api/ask", json={"question": "Total revenue by region", "source": "mine", "table": "sales_q2"}, headers=H)
    a = r.json()
    check("grouped query returns several rows", len(a["rows"]) == 4, a.get("rows"))
    check("chart suggested for label+measure", a["chart"] and a["chart"]["type"] == "bar", a.get("chart"))

    r = c.post("/api/ask", json={"question": "query a bad column please", "source": "mine", "table": "sales_q2"}, headers=H)
    a = r.json()
    check("repair loop recovers from a bad column",
          r.status_code == 200 and any(s["step"] == "Repair SQL" for s in a["trace"]), a.get("trace"))

    r = c.post("/api/ask", json={"question": "drop everything", "source": "mine", "table": "sales_q2"}, headers=H)
    a = r.json()
    check("DDL is refused and repaired", r.status_code == 200 and "DROP" not in (a["sql"] or "").upper(), a.get("sql"))

    r = c.post("/api/ask", json={"question": "this is unanswerable here", "source": "mine", "table": "sales_q2"}, headers=H)
    a = r.json()
    check("unanswerable handled gracefully",
          "can't be answered" in a["answer"] and a["rows"] == [], a.get("answer"))

    r = c.post("/api/ask", json={"question": "Show me the rows", "source": "sample"}, headers=H)
    check("ask works on the sample database", r.status_code == 200 and r.json()["rows"], r.text)

    r = c.post("/api/ask", json={"question": "hi", "source": "mine"}, headers=H)
    check("too-short question rejected", r.status_code == 422, r.status_code)

    head("history + delete")
    r = c.get("/api/history", headers=H)
    check("history saved", len(r.json()) >= 5, len(r.json()))
    check("history keeps the table", any(m["payload"].get("table") == "sales_q2" for m in r.json()))

    r = c.delete("/api/tables/sales_q2", headers=H)
    check("dataset deleted", r.status_code == 200, r.text)
    r = c.get("/api/schema?source=mine", headers=H)
    check("dataset gone from the list", all(t["table"] != "sales_q2" for t in r.json()["tables"]), r.text)
    r = c.delete("/api/tables/sales_q2", headers=H)
    check("deleting twice is a clean 404", r.status_code == 404, r.text)
    r = c.delete("/api/tables/..%2F..%2Fapp", headers=H)
    check("path traversal in table name blocked", r.status_code in (404, 405, 422), r.status_code)

    r = c.delete("/api/history", headers=H)
    check("history cleared", r.status_code == 200 and c.get("/api/history", headers=H).json() == [], r.text)

    head("isolation between accounts")
    r = c.post("/api/signup", json={"name": "Other", "email": "other@example.com", "password": "anotherpass1"})
    H2 = {"Authorization": "Bearer " + r.json()["token"]}
    r = c.get("/api/schema?source=mine", headers=H2)
    check("new account sees no datasets", r.json()["tables"] == [], r.text)
    r = c.post("/api/ask", json={"question": "How many rows?", "source": "mine"}, headers=H2)
    check("asking with no data gives a clear error", r.status_code == 400, r.text)

    head("query safety unit checks")
    for bad in ["DROP TABLE customers", "SELECT 1; DROP TABLE customers",
                "INSERT INTO customers VALUES (1)", "PRAGMA table_info(customers)",
                "SELECT 1 -- ;\nDELETE FROM customers", "UPDATE customers SET name='x'"]:
        try:
            db.validate(bad)
            check("blocked: " + bad[:34], False)
        except db.UnsafeQuery:
            check("blocked: " + bad[:34], True)
    for good in ["SELECT * FROM customers",
                 "WITH x AS (SELECT 1 AS a) SELECT a FROM x",
                 "SELECT status FROM orders WHERE status = 'deleted'",
                 "SELECT COUNT(*) AS created FROM orders"]:
        try:
            db.validate(good)
            check("allowed: " + good[:34], True)
        except db.UnsafeQuery as e:
            check("allowed: " + good[:34], False, str(e))

    head("hallucinated column guard")
    real = [
        'SELECT strftime(\'%Y-%m\', "order_date") AS "month", ROUND(SUM("quantity" * "unit_price"), 2) AS "revenue" '
        'FROM "orders" JOIN "order_items" ON "orders"."id" = "order_items"."order_id" '
        'GROUP BY "month" ORDER BY "month" LIMIT 100',
        'WITH "spend" AS (SELECT "customer_id", SUM("quantity" * "unit_price") AS "total" '
        'FROM "orders" o JOIN "order_items" i ON o."id" = i."order_id" GROUP BY 1) '
        'SELECT c."name", "total" FROM "spend" JOIN "customers" c ON c."id" = "spend"."customer_id" '
        'ORDER BY "total" DESC LIMIT 5',
        'SELECT "category", COUNT(*) AS "n" FROM "products" GROUP BY "category" ORDER BY "n" DESC',
        'SELECT "country", AVG("price") AS "avg_price" FROM "customers", "products" GROUP BY 1 LIMIT 10',
    ]
    con = db._ro(db.DB_PATH)
    for q in real:
        try:
            db.check_identifiers(q, con)
            check("real query accepted: " + q[:38], True)
        except db.UnsafeQuery as e:
            check("real query accepted: " + q[:38], False, str(e))
    for q in ['SELECT "revenu" FROM "orders"', 'SELECT "id", "made_up_col" FROM "products"']:
        try:
            db.check_identifiers(q, con)
            check("hallucination caught: " + q[:38], False)
        except db.UnsafeQuery:
            check("hallucination caught: " + q[:38], True)
    con.close()

    head("static frontend")
    r = c.get("/")
    check("index.html served", r.status_code == 200 and "Ask Your Data" in r.text, r.status_code)


MESSY_CSV_NAME = "team scores (2024).csv"

try:
    main_flow()
finally:
    shutil.rmtree(WORK, ignore_errors=True)

print("\n" + "=" * 46)
print(f"{len(PASS)} passed, {len(FAIL)} failed")
if FAIL:
    print("failed:", *FAIL, sep="\n  - ")
sys.exit(1 if FAIL else 0)
