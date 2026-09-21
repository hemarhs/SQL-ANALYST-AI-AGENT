"""LangGraph text-to-SQL agent: generate -> execute -> (repair loop) -> summarize."""
import os
import re
import time
from typing import Optional, TypedDict

from langchain_core.messages import HumanMessage, SystemMessage
from langchain_groq import ChatGroq
from langgraph.graph import END, StateGraph

from db import DB_PATH, SAMPLE_NOTES, run_query, schema_text

MAX_ATTEMPTS = 3
MODEL = os.getenv("GROQ_MODEL", "openai/gpt-oss-120b")
UNANSWERABLE = "unanswerable"

SYSTEM = f"""You are a senior data analyst writing SQLite queries.
Rules:
- Reply with exactly one read-only SELECT (CTEs allowed) inside a ```sql fence. No explanation.
- Use ONLY the tables and columns listed in the schema, spelled exactly as given. Never invent a column.
- Quote every identifier with double quotes.
- Use SQLite syntax. Dates are ISO text ('YYYY-MM-DD'); group by month with strftime('%Y-%m', "col").
- When several tables share a column name, join on it.
- Alias output columns with readable snake_case names, and round money to 2 decimals.
- Prefer aggregates over raw dumps when the question asks "how many", "total", "average", "top" or "by".
- Add LIMIT 100 unless the result is naturally small.
- If the question cannot be answered from this schema, return exactly:
  SELECT '{UNANSWERABLE}' AS message;"""


class State(TypedDict, total=False):
    question: str
    history: list
    schema: str
    system: str
    db_path: str
    sql: str
    error: Optional[str]
    columns: list
    rows: list
    truncated: bool
    attempts: int
    trace: list
    answer: str
    chart: Optional[dict]


_llm = None


def llm() -> ChatGroq:
    global _llm
    if _llm is None:
        _llm = ChatGroq(model=MODEL, temperature=0, max_retries=2, timeout=45)
    return _llm


def _ms(t: float) -> int:
    return int((time.time() - t) * 1000)


# --------------------------------------------------------------------------- #
# nodes
# --------------------------------------------------------------------------- #
def generate(state: State) -> State:
    t = time.time()
    repairing = bool(state.get("error"))
    ctx = ""
    for h in state.get("history", [])[-3:]:
        ctx += f"Earlier question: {h['question']}\nEarlier SQL: {h['sql']}\n"
    prompt = f"{ctx}\nQuestion: {state['question']}"
    if repairing:
        prompt += (f"\n\nYour previous SQL failed.\nSQL: {state['sql']}\nError: {state['error']}\n"
                   "Return a corrected query that uses only the columns in the schema.")
    msg = llm().invoke([
        SystemMessage(state["system"] + "\n\nSchema:\n" + state["schema"]),
        HumanMessage(prompt),
    ])
    content = msg.content if isinstance(msg.content, str) else str(msg.content)
    m = re.search(r"```(?:sql)?\s*(.*?)```", content, re.S | re.I)
    sql = (m.group(1) if m else content).strip()
    trace = state["trace"] + [{
        "step": "Repair SQL" if repairing else "Write SQL",
        "ok": True, "ms": _ms(t),
        "note": state["error"][:140] if repairing else None,
    }]
    return {"sql": sql, "trace": trace}


def execute(state: State) -> State:
    t = time.time()
    try:
        cols, rows, truncated = run_query(state["sql"], state["db_path"])
        note = f"{len(rows)} row{'' if len(rows) == 1 else 's'}" + (" (capped)" if truncated else "")
        trace = state["trace"] + [{"step": "Run query", "ok": True, "ms": _ms(t), "note": note}]
        return {"columns": cols, "rows": rows, "truncated": truncated, "error": None, "trace": trace}
    except Exception as e:  # SQL errors and safety violations both feed the repair loop
        trace = state["trace"] + [{"step": "Run query", "ok": False, "ms": _ms(t), "note": str(e)[:140]}]
        return {"error": str(e), "attempts": state.get("attempts", 0) + 1, "trace": trace}


def route(state: State) -> str:
    if state.get("error"):
        return "generate" if state.get("attempts", 0) < MAX_ATTEMPTS else "summarize"
    return "summarize"


def suggest_chart(cols: list, rows: list) -> Optional[dict]:
    """Pick a chart only when one genuinely helps: a label column plus a measure."""
    if len(cols) < 2 or not (2 <= len(rows) <= 60):
        return None
    numeric = [i for i in range(len(cols))
               if all(isinstance(r[i], (int, float)) and not isinstance(r[i], bool) for r in rows)]
    label = next((i for i in range(len(cols)) if i not in numeric), None)
    if label is None:
        label = 0
        numeric = [i for i in numeric if i != 0]
    y = next((i for i in numeric if i != label), None)
    if y is None:
        return None
    temporal = bool(re.search(r"month|date|day|week|year|quarter|period|time", cols[label], re.I))
    labels = [str(r[label]) for r in rows]
    if len(set(labels)) != len(labels):  # duplicate categories would stack silently
        return None
    return {"type": "line" if temporal else "bar", "x": label, "y": y, "y_label": cols[y]}


def summarize(state: State) -> State:
    t = time.time()
    if state.get("error"):
        return {
            "answer": "I couldn't produce a working query for that question. Try rephrasing it, "
                      "or name the column you mean — the schema is in the sidebar.",
            "chart": None,
            "trace": state["trace"] + [{"step": "Summarize", "ok": False, "ms": 0, "note": "gave up after 3 attempts"}],
        }
    rows, cols = state.get("rows", []), state.get("columns", [])
    # the model's explicit "I can't answer this from the schema" signal
    if len(rows) == 1 and len(cols) == 1 and str(rows[0][0]).lower() == UNANSWERABLE:
        return {
            "answer": "That question can't be answered from this dataset — the columns it needs aren't there. "
                      "Check the schema in the sidebar and try asking about the fields it lists.",
            "columns": [], "rows": [], "chart": None,
            "trace": state["trace"] + [{"step": "Summarize", "ok": True, "ms": _ms(t), "note": "not answerable"}],
        }
    if not rows:
        return {
            "answer": "The query ran, but no rows matched. Try widening the filter or the date range.",
            "chart": None,
            "trace": state["trace"] + [{"step": "Summarize", "ok": True, "ms": _ms(t), "note": "0 rows"}],
        }
    sample = rows[:20]
    msg = llm().invoke([
        SystemMessage("You are a data analyst. Answer the question in 2-3 plain sentences using ONLY the rows shown. "
                      "Quote concrete numbers from the data and name the columns they come from. "
                      "Never invent figures. No markdown, no bullet points, no preamble."),
        HumanMessage(f"Question: {state['question']}\nColumns: {cols}\n"
                     f"Rows (first {len(sample)} of {len(rows)}): {sample}"),
    ])
    content = msg.content if isinstance(msg.content, str) else str(msg.content)
    return {
        "answer": content.strip(),
        "chart": suggest_chart(cols, rows),
        "trace": state["trace"] + [{"step": "Summarize", "ok": True, "ms": _ms(t), "note": None}],
    }


def build():
    g = StateGraph(State)
    g.add_node("generate", generate)
    g.add_node("execute", execute)
    g.add_node("summarize", summarize)
    g.set_entry_point("generate")
    g.add_edge("generate", "execute")
    g.add_conditional_edges("execute", route, {"generate": "generate", "summarize": "summarize"})
    g.add_edge("summarize", END)
    return g.compile()


_graph = None


def ask(question: str, history: list | None = None, source: str = "sample",
        db_path: str = "", table: str | None = None) -> dict:
    """Answer one question. ``table`` scopes the schema to a single uploaded dataset."""
    global _graph
    _graph = _graph or build()
    t = time.time()
    path = db_path or str(DB_PATH)
    schema = schema_text(path, sample_row=(source != "sample"), only=table)
    if not schema.strip():
        raise ValueError("There is no data to query yet. Upload a CSV first.")
    system = SYSTEM + ("\n\n" + SAMPLE_NOTES if source == "sample" else "")
    if table:
        system += f'\n\nAnswer using the table "{table}" only.'
    out = _graph.invoke({
        "question": question, "history": history or [], "schema": schema, "system": system,
        "db_path": path, "attempts": 0, "error": None,
        "trace": [{"step": "Read schema", "ok": True, "ms": 0,
                   "note": f'table "{table}"' if table else None}],
    })
    return {
        "question": question,
        "sql": out.get("sql", ""),
        "columns": out.get("columns", []),
        "rows": out.get("rows", []),
        "truncated": out.get("truncated", False),
        "answer": out["answer"],
        "chart": out.get("chart"),
        "trace": out["trace"],
        "error": out.get("error"),
        "total_ms": _ms(t),
        "source": source,
        "table": table,
    }
