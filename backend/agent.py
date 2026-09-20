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

SYSTEM = """You are a senior data analyst writing SQLite queries.
Rules:
- Reply with exactly one read-only SELECT (CTEs allowed) inside a ```sql fence. No explanation.
- Use only the tables and columns in the schema. Never invent columns.
- Use SQLite syntax and date functions (strftime). Double-quote identifiers that are SQL keywords.
- When several tables share a column name, join on it.
- Alias output columns with readable snake_case names. Add LIMIT 100 unless the result is naturally small.
- If the question cannot be answered from the schema, return: SELECT 'unanswerable' AS message;"""


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
    attempts: int
    trace: list
    answer: str
    chart: Optional[dict]


_llm = None


def llm() -> ChatGroq:
    global _llm
    if _llm is None:
        _llm = ChatGroq(model=MODEL, temperature=0)
    return _llm


def _ms(t: float) -> int:
    return int((time.time() - t) * 1000)


def generate(state: State) -> State:
    t = time.time()
    repairing = bool(state.get("error"))
    ctx = ""
    for h in state.get("history", [])[-3:]:
        ctx += f"Earlier question: {h['question']}\nEarlier SQL: {h['sql']}\n"
    prompt = f"{ctx}\nQuestion: {state['question']}"
    if repairing:
        prompt += f"\n\nYour previous SQL failed.\nSQL: {state['sql']}\nError: {state['error']}\nReturn a corrected query."
    msg = llm().invoke([SystemMessage(state["system"] + "\n\nSchema:\n" + state["schema"]), HumanMessage(prompt)])
    m = re.search(r"```(?:sql)?\s*(.*?)```", msg.content, re.S | re.I)
    sql = (m.group(1) if m else msg.content).strip()
    trace = state["trace"] + [{"step": "Repair SQL" if repairing else "Write SQL", "ok": True, "ms": _ms(t),
                               "note": state["error"][:140] if repairing else None}]
    return {"sql": sql, "trace": trace}


def execute(state: State) -> State:
    t = time.time()
    try:
        cols, rows = run_query(state["sql"], state["db_path"])
        trace = state["trace"] + [{"step": "Run query", "ok": True, "ms": _ms(t), "note": f"{len(rows)} rows"}]
        return {"columns": cols, "rows": rows, "error": None, "trace": trace}
    except Exception as e:  # SQL errors and safety violations both feed the repair loop
        trace = state["trace"] + [{"step": "Run query", "ok": False, "ms": _ms(t), "note": str(e)[:140]}]
        return {"error": str(e), "attempts": state.get("attempts", 0) + 1, "trace": trace}


def route(state: State) -> str:
    if state.get("error"):
        return "generate" if state.get("attempts", 0) < MAX_ATTEMPTS else "summarize"
    return "summarize"


def suggest_chart(cols: list, rows: list) -> Optional[dict]:
    if len(cols) < 2 or not (2 <= len(rows) <= 60):
        return None
    num = [i for i in range(1, len(cols)) if all(isinstance(r[i], (int, float)) for r in rows)]
    if not num:
        return None
    kind = "line" if re.search(r"month|date|day|week|year|quarter", cols[0], re.I) else "bar"
    return {"type": kind, "x": 0, "y": num[0]}


def summarize(state: State) -> State:
    t = time.time()
    if state.get("error"):
        return {"answer": "I couldn't produce a working query for that. Try rephrasing or naming the tables involved.",
                "chart": None, "trace": state["trace"] + [{"step": "Summarize", "ok": False, "ms": 0, "note": "gave up"}]}
    sample = state["rows"][:20]
    msg = llm().invoke([
        SystemMessage("You are a data analyst. In 2-3 plain sentences, answer the question using only the data shown. "
                      "Cite concrete numbers. No markdown."),
        HumanMessage(f"Question: {state['question']}\nColumns: {state['columns']}\nRows (first 20 of {len(state['rows'])}): {sample}"),
    ])
    return {"answer": msg.content.strip(), "chart": suggest_chart(state["columns"], state["rows"]),
            "trace": state["trace"] + [{"step": "Summarize", "ok": True, "ms": _ms(t), "note": None}]}


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


def ask(question: str, history: list | None = None, source: str = "sample", db_path: str = "") -> dict:
    global _graph
    _graph = _graph or build()
    t = time.time()
    system = SYSTEM + ("\n\n" + SAMPLE_NOTES if source == "sample" else "")
    schema = schema_text(db_path, sample_row=(source != "sample")) if db_path else schema_text()
    out = _graph.invoke({"question": question, "history": history or [], "schema": schema, "system": system,
                         "db_path": db_path or str(DB_PATH), "attempts": 0, "error": None,
                         "trace": [{"step": "Read schema", "ok": True, "ms": 0, "note": None}]})
    return {"question": question, "sql": out["sql"], "columns": out.get("columns", []), "rows": out.get("rows", []),
            "answer": out["answer"], "chart": out.get("chart"), "trace": out["trace"], "error": out.get("error"),
            "total_ms": _ms(t), "source": source}
