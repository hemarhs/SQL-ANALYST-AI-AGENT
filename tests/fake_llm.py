"""A deterministic stand-in for ChatGroq so the whole agent graph can be tested offline."""
import re


class Msg:
    def __init__(self, content):
        self.content = content


class FakeLLM:
    """Answers the 'write SQL' turn from a keyword map and the 'summarize' turn with a sentence."""

    calls = 0

    def invoke(self, messages):
        FakeLLM.calls += 1
        system = messages[0].content
        human = messages[-1].content
        if system.startswith("You are a data analyst."):
            return Msg("Revenue peaked in the North region at 23,750 based on the total column.")

        # only the current question matters, not the follow-up context above it
        q = human.rsplit("Question:", 1)[-1].split("\n")[0].strip().lower()
        repairing = "your previous sql failed" in human.lower()
        schema = system.split("Schema:\n", 1)[-1]
        tables = re.findall(r"^(\w+)\(", schema, re.M)
        t = tables[0] if tables else "orders"
        cols = re.findall(r'"([a-z0-9_]+)"\s+(INTEGER|REAL|TEXT|DATE)', schema)

        if repairing:
            return Msg(f'```sql\nSELECT COUNT(*) AS row_count FROM "{t}"\n```')
        if "bad column" in q:
            return Msg(f'```sql\nSELECT "nope_missing_col" FROM "{t}"\n```')
        if "unanswerable" in q:
            return Msg("```sql\nSELECT 'unanswerable' AS message\n```")
        if "drop everything" in q:                              # safety probe
            return Msg(f'```sql\nDROP TABLE "{t}"\n```')
        if "how many" in q or "count" in q:
            return Msg(f'```sql\nSELECT COUNT(*) AS row_count FROM "{t}"\n```')
        if " by " in q:
            text = next((c for c, ty in cols if ty == "TEXT"), None)
            num = next((c for c, ty in cols if ty in ("INTEGER", "REAL")), None)
            if text and num:
                return Msg(f'```sql\nSELECT "{text}" AS label, ROUND(SUM("{num}"),2) AS total '
                           f'FROM "{t}" GROUP BY 1 ORDER BY 2 DESC LIMIT 100\n```')
        return Msg(f'```sql\nSELECT * FROM "{t}" LIMIT 10\n```')
