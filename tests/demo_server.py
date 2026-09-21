"""Runs the real app on :8099 with a scripted LLM, so the UI can be driven offline."""
import os
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "backend"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

WORK = Path(os.environ.get("DEMO_DIR") or tempfile.mkdtemp(prefix="sqa-demo-"))
WORK.mkdir(parents=True, exist_ok=True)
os.environ["GROQ_API_KEY"] = "demo-key"
os.environ["APP_SECRET"] = "demo-secret"

import db      # noqa: E402
import auth    # noqa: E402

db.DB_PATH = WORK / "shop.db"
db.DATA_DIR = WORK / "data"
auth.APP_DB = WORK / "app.db"

import agent   # noqa: E402
import re      # noqa: E402


class Msg:
    def __init__(self, content):
        self.content = content


class DemoLLM:
    """Deterministic SQL for the demo questions — the graph, safety and rendering are all real."""

    def invoke(self, messages):
        system, human = messages[0].content, messages[-1].content
        if system.startswith("You are a data analyst."):
            return Msg(self._narrate(human))
        q = human.rsplit("Question:", 1)[-1].split("\n")[0].strip().lower()
        schema = system.split("Schema:\n", 1)[-1]
        names = re.findall(r"^(\w+)\(", schema, re.M)
        t = (names or ["orders"])[0]

        if "order_items" in names:                       # the sample shop database
            if "month" in q or "2025" in q or "trend" in q:
                return Msg("""```sql
SELECT strftime('%Y-%m', "o"."order_date") AS "month",
       ROUND(SUM("i"."quantity" * "i"."unit_price"), 2) AS "revenue"
FROM "orders" "o"
JOIN "order_items" "i" ON "i"."order_id" = "o"."id"
WHERE "o"."status" <> 'cancelled' AND "o"."order_date" >= '2025-01-01' AND "o"."order_date" < '2026-01-01'
GROUP BY "month"
ORDER BY "month"
```""")
            if "customer" in q or "spend" in q:
                return Msg("""```sql
SELECT "c"."name" AS "customer",
       ROUND(SUM("i"."quantity" * "i"."unit_price"), 2) AS "total_spend"
FROM "customers" "c"
JOIN "orders" "o" ON "o"."customer_id" = "c"."id"
JOIN "order_items" "i" ON "i"."order_id" = "o"."id"
WHERE "o"."status" <> 'cancelled'
GROUP BY "c"."name"
ORDER BY "total_spend" DESC
LIMIT 5
```""")
            if "categor" in q:
                return Msg("""```sql
SELECT "p"."category",
       ROUND(SUM("i"."quantity" * "i"."unit_price"), 2) AS "revenue"
FROM "order_items" "i"
JOIN "products" "p" ON "p"."id" = "i"."product_id"
JOIN "orders" "o" ON "o"."id" = "i"."order_id"
WHERE "o"."status" <> 'cancelled'
GROUP BY "p"."category"
ORDER BY "revenue" DESC
```""")
            if "country" in q or "return" in q:
                return Msg("""```sql
SELECT "c"."country",
       ROUND(100.0 * SUM(CASE WHEN "o"."status" = 'returned' THEN 1 ELSE 0 END) / COUNT(*), 2) AS "return_rate_pct"
FROM "orders" "o"
JOIN "customers" "c" ON "c"."id" = "o"."customer_id"
GROUP BY "c"."country"
ORDER BY "return_rate_pct" DESC
```""")
            if "segment" in q or "average order" in q:
                return Msg("""```sql
SELECT "c"."segment",
       ROUND(AVG("t"."order_total"), 2) AS "avg_order_value"
FROM (SELECT "o"."id", "o"."customer_id", SUM("i"."quantity" * "i"."unit_price") AS "order_total"
      FROM "orders" "o" JOIN "order_items" "i" ON "i"."order_id" = "o"."id"
      WHERE "o"."status" <> 'cancelled' GROUP BY "o"."id") "t"
JOIN "customers" "c" ON "c"."id" = "t"."customer_id"
GROUP BY "c"."segment"
ORDER BY "avg_order_value" DESC
```""")
            return Msg('```sql\nSELECT "id", "order_date", "status" FROM "orders" ORDER BY "order_date" DESC LIMIT 10\n```')

        if "month" in q or "trend" in q or "over time" in q:
            return Msg(f"""```sql
SELECT strftime('%Y-%m', "order_date") AS "month",
       ROUND(SUM("revenue"), 2) AS "total_revenue"
FROM "{t}"
GROUP BY "month"
ORDER BY "month"
LIMIT 100
```""")
        if "region" in q or " by " in q:
            return Msg(f"""```sql
SELECT "region",
       ROUND(SUM("revenue"), 2) AS "total_revenue",
       SUM("units_sold") AS "units"
FROM "{t}"
GROUP BY "region"
ORDER BY "total_revenue" DESC
LIMIT 100
```""")
        if "how many" in q or "count" in q:
            return Msg(f'```sql\nSELECT COUNT(*) AS "row_count" FROM "{t}"\n```')
        if "rep" in q or "top" in q:
            return Msg(f"""```sql
SELECT "sales_rep", ROUND(SUM("revenue"), 2) AS "total_revenue"
FROM "{t}"
GROUP BY "sales_rep"
ORDER BY "total_revenue" DESC
LIMIT 5
```""")
        return Msg(f'```sql\nSELECT * FROM "{t}" LIMIT 10\n```')

    @staticmethod
    def _narrate(human):
        h = human.lower()
        if "'month'" in h or '"month"' in h or "month" in h.split("columns:")[-1][:60]:
            if "revenue" in h and "202" in h:
                return ("Revenue builds through the year: it starts near 52,000 a month in January, peaks in the "
                        "middle of the year, and the twelve 2025 months together account for the bulk of the "
                        "order value in the database. No single month contributes more than about 11% of the total.")
        if "region" in human.lower():
            return ("North America leads with 71,250 in total_revenue across 57 units, ahead of Europe on "
                    "48,044.50. Latin America is the smallest region at 7,790, roughly a ninth of North "
                    "America's total.")
        if "month" in human.lower():
            return ("Revenue climbs from 18,922 in January to a peak of 31,562.75 in March, dips to 22,801 "
                    "in April, and recovers to 24,077 by June. March and May are the two strongest months "
                    "in the period.")
        return ("The result set contains the rows matching your question, with the figures shown in the "
                "table below.")


agent._llm = DemoLLM()
agent.llm = lambda: agent._llm
agent.DB_PATH = db.DB_PATH

import main  # noqa: E402

if __name__ == "__main__":
    import uvicorn
    print("demo dir:", WORK)
    uvicorn.run(main.app, host="127.0.0.1", port=int(os.environ.get("PORT", 8099)), log_level="warning")
