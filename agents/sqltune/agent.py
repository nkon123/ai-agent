"""sqltune — 쿼리를 튜닝 기준으로 진단하고, 플랜과 수행을 비교한다.

    안전 검사 → 정적 규칙 진단 → 실행계획(EXPLAIN) → 플랜 규칙 진단
    → 인덱스 제안 → (선택) 실제 수행 비교 → (선택) LLM 개선안

단독 실행:
    python agents/sqltune/agent.py -f slow.sql
    python agents/sqltune/agent.py -q "SELECT * FROM ORD_HDR WHERE TO_CHAR(REG_DT,'YYYYMMDD')='20260101'"
    python agents/sqltune/agent.py -f slow.sql --run     # 실제 수행까지 비교

안전 원칙 (이 에이전트에서 특히 중요하다):
    - SELECT 만 받는다. DML/DDL/PL-SQL 블록은 거부한다.
    - 인덱스는 **제안만** 한다. CREATE INDEX 를 실행하지 않는다.
    - 실제 수행(--run)은 기본이 꺼져 있다. 실행은 곧 운영 DB 부하다.
      플랜만으로도 대부분의 문제는 보인다.
    - 판정 기준은 tuning_rules.md 한 곳에 있다. 규칙 엔진과 LLM 이 같은
      문서를 본다 — 기준이 이원화되면 서로 다른 답이 나온다.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path
from typing import Any, Literal, TypedDict

_ROOT = Path(__file__).resolve().parents[2]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from langgraph.graph import END, START, StateGraph  # noqa: E402

from config import (  # noqa: E402
    SQLTUNE_EXECUTE,
    SQLTUNE_MAX_ROWS,
    SQLTUNE_RULES_FILE,
    SQLTUNE_RUNS,
    SQLTUNE_TIMEOUT_SEC,
    USE_LLM,
)
from core import oracle  # noqa: E402
from core.cache import cached  # noqa: E402
from core.text import read_text, strip_comments  # noqa: E402

Detail = Literal["full", "summary", "minimal"]

# SELECT 가 아닌 것. 하나라도 있으면 거부한다.
# UTL_/DBMS_ 는 파일·네트워크 접근이나 작업 실행이 가능해 조회로 위장한
# 부작용을 만들 수 있다(UTL_HTTP, UTL_FILE 등). 튜닝에 필요 없으므로 막는다.
_FORBIDDEN = (
    ("dml", r"(?i)(?<![A-Za-z0-9_])(INSERT|UPDATE|DELETE|MERGE)(?![A-Za-z0-9_])"),
    ("ddl", r"(?i)(?<![A-Za-z0-9_])(CREATE|ALTER|DROP|TRUNCATE|RENAME)(?![A-Za-z0-9_])"),
    ("grant", r"(?i)(?<![A-Za-z0-9_])(GRANT|REVOKE)(?![A-Za-z0-9_])"),
    ("plsql", r"(?i)(?<![A-Za-z0-9_])(BEGIN|DECLARE|EXECUTE\s+IMMEDIATE)(?![A-Za-z0-9_])"),
    ("lock", r"(?i)(?<![A-Za-z0-9_])(FOR\s+UPDATE|LOCK\s+TABLE)(?![A-Za-z0-9_])"),
    ("package", r"(?i)(?<![A-Za-z0-9_])(UTL_|DBMS_)[A-Za-z0-9_]+"),
)


def is_safe_select(sql: str) -> tuple[bool, str]:
    """조회 전용 단일 문장인지 검사한다. (안전한가, 이유)

    주석과 문자열을 지운 사본으로 본다. 원문으로 검사하면 문자열 안의
    'DELETE' 같은 단어에 걸리고, 반대로 주석으로 위장한 DML 을 놓친다.
    """
    if not (sql or "").strip():
        return False, "빈 쿼리다"
    try:
        body = strip_comments(sql, "sql", mask_strings=True)
    except ValueError:
        body = sql

    if not re.match(r"(?is)\s*(SELECT|WITH)\b", body):
        return False, "SELECT(또는 WITH)로 시작하지 않는다 — 조회만 다룬다"

    # 세미콜론 뒤에 내용이 더 있으면 여러 문장이다.
    tail = body.split(";", 1)[1] if ";" in body else ""
    if tail.strip():
        return False, "한 번에 한 문장만 받는다(세미콜론 뒤에 내용이 있다)"

    for name, pattern in _FORBIDDEN:
        m = re.search(pattern, body)
        if m:
            return False, f"조회가 아닌 요소가 있다({name}: {m.group(0)})"
    return True, ""


def strip_trailing_semicolon(sql: str) -> str:
    """EXPLAIN PLAN FOR 뒤에 세미콜론이 붙으면 문법 오류가 난다."""
    return re.sub(r";\s*$", "", (sql or "").strip())


# --------------------------------------------------------------------------
# 튜닝 기준 문서
# --------------------------------------------------------------------------


@cached(ttl=600, maxsize=2, key=lambda path: path)
def load_rules(path: str) -> str:
    """기준 문서를 읽는다. LLM 프롬프트에 그대로 넣는다.

    캐시하는 이유: 파일이 몇 KB 라 매 요청 읽을 이유가 없다.
    """
    try:
        return read_text(path)
    except OSError as e:
        return f"(튜닝 기준 문서를 읽지 못했다: {e})"


# --------------------------------------------------------------------------
# 정적 규칙 — 쿼리 본문만 보고 잡는 것
# --------------------------------------------------------------------------

# (규칙명, 정규식, 설명). tuning_rules.md 의 규칙명과 같아야 한다.
_TEXT_RULES: tuple[tuple[str, str, str], ...] = (
    (
        "func-on-column",
        r"(?i)(?:WHERE|AND|OR)\s+(?:TO_CHAR|TO_DATE|SUBSTR|TRIM|UPPER|LOWER|"
        r"TRUNC|ROUND)\s*\(\s*([A-Za-z][A-Za-z0-9_.]*)",
        "인덱스 컬럼에 함수를 씌우면 인덱스를 타지 못한다. 범위 조건으로 바꿀 것",
    ),
    (
        "nvl-on-column",
        r"(?i)(?:WHERE|AND|OR)\s+NVL\s*\(\s*([A-Za-z][A-Za-z0-9_.]*)",
        "컬럼에 NVL 을 씌우면 인덱스를 타지 못한다. OR ... IS NULL 로 풀 것",
    ),
    (
        "leading-wildcard",
        r"(?i)([A-Za-z][A-Za-z0-9_.]*)\s+LIKE\s+'%",
        "선행 와일드카드는 인덱스를 타지 못한다",
    ),
    (
        "negation",
        r"(?i)([A-Za-z][A-Za-z0-9_.]*)\s*(?:!=|<>|NOT\s+IN)",
        "부정 조건은 인덱스로 걸러내지 못한다. 대상 값 열거를 검토할 것",
    ),
    (
        "select-star",
        r"(?i)SELECT\s+\*",
        "필요한 컬럼만 적으면 커버링 인덱스로 테이블 접근을 없앨 수 있다",
    ),
    (
        "or-condition",
        r"(?i)\sOR\s",
        "서로 다른 컬럼의 OR 는 UNION ALL 분리를 검토할 것",
    ),
)


def check_text_rules(sql: str) -> list[dict[str, str]]:
    """쿼리 본문에서 잡히는 문제. 근거(실제 조각)를 함께 남긴다."""
    try:
        body = strip_comments(sql, "sql", mask_strings=False)
    except ValueError:
        body = sql
    flat = " ".join(body.split())

    found: list[dict[str, str]] = []
    for name, pattern, note in _TEXT_RULES:
        m = re.search(pattern, flat)
        if m:
            found.append(
                {
                    "rule": name,
                    "note": note,
                    "evidence": flat[max(0, m.start() - 20) : m.end() + 30].strip(),
                }
            )

    # 리터럴 사용은 바인드 변수와 함께 봐야 의미가 있다.
    literals = len(re.findall(r"'[^']*'", flat)) + len(
        re.findall(r"(?<![A-Za-z0-9_:.])\d{2,}(?![A-Za-z0-9_])", flat)
    )
    binds = len(re.findall(r":[A-Za-z][A-Za-z0-9_]*", flat))
    if literals >= 2 and binds == 0:
        found.append(
            {
                "rule": "literal-value",
                "note": "리터럴만 쓰면 하드파싱이 반복된다. 반복 수행 SQL 은 바인드로",
                "evidence": f"리터럴 {literals}개, 바인드 0개",
            }
        )
    return found


# --------------------------------------------------------------------------
# 플랜 규칙 — 실행계획을 보고 잡는 것
# --------------------------------------------------------------------------


def check_plan_rules(plan: list[dict[str, Any]]) -> list[dict[str, str]]:
    """실행계획에서 잡히는 문제."""
    found: list[dict[str, str]] = []
    for row in plan:
        op = f"{row.get('OPERATION') or ''} {row.get('OPTIONS') or ''}".strip()
        obj = row.get("OBJECT_NAME") or ""
        up = op.upper()

        if "TABLE ACCESS" in up and "FULL" in up:
            found.append(
                {
                    "rule": "full-scan",
                    "note": (
                        "전체 스캔이다. 필요한 행 비율이 낮으면 인덱스를 검토한다. "
                        "대량(10~20% 이상)이면 전체 스캔이 정상일 수 있다"
                    ),
                    "evidence": f"{op} {obj} (예상 {row.get('CARDINALITY')}행)",
                }
            )
        if "INDEX" in up and ("FULL SCAN" in up or "SKIP SCAN" in up):
            found.append(
                {
                    "rule": "index-full-scan",
                    "note": "인덱스 선두 컬럼이 조건에 없다는 신호다",
                    "evidence": f"{op} {obj}",
                }
            )
        if "CARTESIAN" in up:
            found.append(
                {
                    "rule": "cartesian",
                    "note": "조인 조건 누락이 의심된다. 거의 항상 버그다",
                    "evidence": f"{op} {obj}",
                }
            )
        if "SORT" in up and "ORDER BY" in up:
            found.append(
                {
                    "rule": "sort-order-by",
                    "note": "정렬을 인덱스로 없앨 수 있는지 본다",
                    "evidence": op,
                }
            )
        # 인덱스가 걸러내지 못하고 테이블에서 버린 조건.
        filt = row.get("FILTER_PREDICATES")
        if filt and "TABLE ACCESS" in up:
            found.append(
                {
                    "rule": "filter-not-access",
                    "note": "인덱스가 걸러내지 못한 조건이다. access 술어로 옮길 수 있는지 본다",
                    "evidence": f"{obj}: {str(filt)[:80]}",
                }
            )
    return found


# --------------------------------------------------------------------------
# 인덱스 제안
# --------------------------------------------------------------------------


def _predicate_columns(sql: str) -> dict[str, list[str]]:
    """WHERE 절에서 등치/범위 컬럼과 ORDER BY 컬럼을 뽑는다.

    정규식이라 완벽하지 않다. 못 뽑은 것은 '확인 필요'로 남기고, 뽑은 것도
    사람이 검토할 제안일 뿐이다 — 인덱스는 자동으로 만들지 않는다.
    """
    try:
        body = strip_comments(sql, "sql", mask_strings=False)
    except ValueError:
        body = sql
    flat = " ".join(body.split())

    where = ""
    m = re.search(r"(?is)\bWHERE\b(.*?)(?:\bGROUP\s+BY\b|\bORDER\s+BY\b|$)", flat)
    if m:
        where = m.group(1)

    def cols(pattern: str) -> list[str]:
        out: list[str] = []
        for mm in re.finditer(pattern, where):
            name = mm.group(1).split(".")[-1].upper()
            if name not in out:
                out.append(name)
        return out

    equals = cols(r"(?i)(?<![A-Za-z0-9_.])([A-Za-z][A-Za-z0-9_.]*)\s*=\s*(?::|')")
    ranges = cols(
        r"(?i)(?<![A-Za-z0-9_.])([A-Za-z][A-Za-z0-9_.]*)\s*(?:>=|<=|>|<|BETWEEN)"
    )
    order: list[str] = []
    om = re.search(r"(?is)\bORDER\s+BY\b(.*)$", flat)
    if om:
        for part in om.group(1).split(","):
            name = part.strip().split()[0].split(".")[-1].upper() if part.strip() else ""
            if name and name.isidentifier() and name not in order:
                order.append(name)
    return {"equals": equals, "ranges": ranges, "order": order}


def suggest_index(
    sql: str, table: str, existing: list[dict[str, Any]] | None = None
) -> dict[str, Any]:
    """인덱스 후보를 만든다. **DDL 은 실행하지 않는다.**

    등치 조건 컬럼을 선두에, 범위 조건을 그 뒤에, ORDER BY 컬럼을 마지막에
    둔다(기준 문서 7항). 기존 인덱스와 선두 컬럼이 같으면 제안하지 않는다 —
    중복 인덱스는 조회를 빠르게 하지 않고 DML 만 느리게 한다.
    """
    parts = _predicate_columns(sql)
    columns = parts["equals"] + [c for c in parts["ranges"] if c not in parts["equals"]]
    columns += [
        c for c in parts["order"] if c not in columns
    ]
    if not columns:
        return {
            "columns": [],
            "ddl": "",
            "rule": "no-predicate",
            "note": "인덱스 후보를 만들 조건을 찾지 못했다 — 확인 필요",
        }

    for idx in existing or []:
        if idx["columns"][: len(columns)] == columns[: len(idx["columns"])]:
            return {
                "columns": columns,
                "ddl": "",
                "rule": "already-covered",
                "note": (
                    f"기존 인덱스 {idx['name']}({', '.join(idx['columns'])}) 의 "
                    "선두 컬럼이 같다. 새로 만들지 말 것"
                ),
            }

    name = f"IX_{table.upper()[:20]}_{'_'.join(c[:6] for c in columns[:3])}"[:30]
    return {
        "columns": columns,
        # 실행하지 않는다. 사람이 검토하고 직접 수행한다(안전 규칙 4-8).
        "ddl": f"CREATE INDEX {name} ON {table.upper()} ({', '.join(columns)});",
        "rule": "composite-index",
        "note": (
            f"등치 {len(parts['equals'])}개 · 범위 {len(parts['ranges'])}개 · "
            f"정렬 {len(parts['order'])}개 순으로 구성했다. "
            "실행 전에 DML 부하와 기존 인덱스를 확인할 것"
        ),
    }


def _main_table(sql: str) -> str:
    """FROM 절의 첫 테이블. 인덱스 제안 대상으로 쓴다."""
    m = re.search(r"(?is)\bFROM\s+([A-Za-z][A-Za-z0-9_$#.]*)", sql or "")
    return m.group(1).split(".")[-1].upper() if m else ""


# --------------------------------------------------------------------------
# LangGraph — 진단 흐름
# --------------------------------------------------------------------------


class TuneState(TypedDict, total=False):
    sql: str
    execute: bool
    safe: bool
    findings: list[dict[str, str]]
    plan: list[dict[str, Any]]
    index: dict[str, Any]
    run: dict[str, Any]
    proposal: dict[str, Any]
    decided_by: str
    rule: str
    warnings: list[str]


def guard(state: TuneState) -> TuneState:
    """조회 전용인지 먼저 본다. 여기서 막히면 아무것도 하지 않는다."""
    ok, reason = is_safe_select(state.get("sql", ""))
    if not ok:
        return {
            "safe": False,
            "decided_by": "rule",
            "rule": "unsafe-statement",
            "warnings": [f"실행하지 않았다 — {reason}"],
        }
    return {"safe": True, "warnings": []}


def static_check(state: TuneState) -> TuneState:
    """DB 없이 본문만 보고 잡는다. 접속이 안 돼도 여기까지는 항상 된다."""
    if not state.get("safe"):
        return {}
    return {"findings": check_text_rules(state["sql"])}


def plan_check(state: TuneState) -> TuneState:
    """실행계획을 받아 규칙으로 본다. 쿼리를 실행하지 않는다."""
    if not state.get("safe"):
        return {}

    warnings = list(state.get("warnings") or [])
    if not oracle.is_configured():
        warnings.append("Oracle 설정이 비어 있어 실행계획을 못 봤다 — 확인 필요")
        return {"plan": [], "warnings": warnings}

    sql = strip_trailing_semicolon(state["sql"])
    try:
        plan = oracle.explain_plan(sql)
    except Exception as e:
        # 실패를 삼키면 '문제 없음'으로 보인다. PLAN_TABLE 이 없는 경우가 흔하다.
        warnings.append(f"실행계획을 받지 못했다 — 확인 필요: {type(e).__name__}: {e}")
        return {"plan": [], "warnings": warnings}

    findings = list(state.get("findings") or []) + check_plan_rules(plan)
    return {"plan": plan, "findings": findings, "warnings": warnings}


def index_check(state: TuneState) -> TuneState:
    """인덱스 후보를 만든다. DDL 은 만들기만 하고 실행하지 않는다."""
    if not state.get("safe"):
        return {}

    table = _main_table(state["sql"])
    existing: list[dict[str, Any]] = []
    warnings = list(state.get("warnings") or [])
    if table and oracle.is_configured():
        try:
            existing = oracle.existing_indexes(table)
        except Exception as e:
            warnings.append(
                f"기존 인덱스를 확인하지 못했다 — 확인 필요: {type(e).__name__}: {e}"
            )
    return {
        "index": {"table": table, "existing": existing,
                  **suggest_index(state["sql"], table or "TABLE", existing)},
        "warnings": warnings,
    }


def run_check(state: TuneState) -> TuneState:
    """실제로 수행해 시간과 실제 계획을 받는다. 기본은 하지 않는다.

    실행은 곧 운영 DB 부하다. 사람이 --run 으로 켤 때만 돈다.
    """
    if not state.get("safe") or not state.get("execute"):
        return {}

    warnings = list(state.get("warnings") or [])
    if not oracle.is_configured():
        warnings.append("Oracle 설정이 비어 있어 수행 비교를 못 했다 — 확인 필요")
        return {"warnings": warnings}

    try:
        result = oracle.execute_with_stats(
            strip_trailing_semicolon(state["sql"]),
            max_rows=SQLTUNE_MAX_ROWS,
            timeout=SQLTUNE_TIMEOUT_SEC,
            runs=SQLTUNE_RUNS,
        )
    except Exception as e:
        warnings.append(f"수행 비교 실패 — 확인 필요: {type(e).__name__}: {e}")
        return {"warnings": warnings}

    if result.get("truncated"):
        # 상한까지만 읽었으면 수행 시간이 실제보다 짧다. 감추지 않는다.
        warnings.append(
            f"결과를 {SQLTUNE_MAX_ROWS}행까지만 읽었다. 실제 수행 시간은 더 길 수 있다"
        )
    return {"run": result, "warnings": warnings}


def propose(state: TuneState) -> TuneState:
    """LLM 에게 기준 문서를 주고 개선안을 받는다.

    LLM 이 규칙 판정을 뒤집지 않는다. 규칙이 찾은 문제를 근거로, 고친
    쿼리를 '초안'으로 만들 뿐이다. 실행은 사람이 판단한다.
    """
    if not state.get("safe") or not USE_LLM:
        return {}

    warnings = list(state.get("warnings") or [])
    findings = state.get("findings") or []
    try:
        from core.llm import get_llm

        rules = load_rules(SQLTUNE_RULES_FILE)
        issues = "\n".join(f"- {f['rule']}: {f['note']} (근거: {f['evidence']})"
                           for f in findings) or "- (규칙이 찾은 문제 없음)"
        prompt = (
            "너는 오라클 SQL 튜닝을 돕는다. 아래 '튜닝 기준'에 있는 항목만 근거로 삼아라.\n"
            "기준에 없는 내용을 지어내지 마라. 결과 건수가 달라지는 변경은 하지 마라.\n\n"
            f"[튜닝 기준]\n{rules[:6000]}\n\n"
            f"[규칙이 찾은 문제]\n{issues}\n\n"
            f"[원본 쿼리]\n{state['sql'][:3000]}\n\n"
            "고친 쿼리와 그렇게 고친 이유를 기준의 규칙명과 함께 간단히 답해라."
        )
        answer = str(get_llm().invoke(prompt).content).strip()
        return {
            "proposal": {"text": answer, "based_on": [f["rule"] for f in findings]},
            "warnings": warnings,
        }
    except Exception as e:
        warnings.append(f"LLM 개선안 생성 실패 — 확인 필요: {type(e).__name__}: {e}")
        return {"warnings": warnings}


@cached(ttl=3600, maxsize=1, key=lambda: "graph")
def _graph():
    g = StateGraph(TuneState)
    for name, fn in (
        ("guard", guard), ("static_check", static_check), ("plan_check", plan_check),
        ("index_check", index_check), ("run_check", run_check), ("propose", propose),
    ):
        g.add_node(name, fn)
    g.add_edge(START, "guard")
    g.add_edge("guard", "static_check")
    g.add_edge("static_check", "plan_check")
    g.add_edge("plan_check", "index_check")
    g.add_edge("index_check", "run_check")
    g.add_edge("run_check", "propose")
    g.add_edge("propose", END)
    return g.compile()


def run_sqltune(
    sql: str, execute: bool | None = None, detail: Detail = "full"
) -> dict[str, Any]:
    """진입점.

        full    : 플랜 행과 SQL 전문 포함 (화면용, MCP 리소스로 나간다)
        summary : 문제 목록과 인덱스 제안 (LLM 컨텍스트에 들어간다)
        minimal : 건수만

    execute: 실제 수행 여부. None 이면 config.SQLTUNE_EXECUTE 를 따른다.
    """
    state: TuneState = _graph().invoke(
        {
            "sql": sql,
            "execute": SQLTUNE_EXECUTE if execute is None else bool(execute),
        }
    )
    findings = state.get("findings") or []
    index = state.get("index") or {}
    base = {
        "safe": bool(state.get("safe")),
        "finding_count": len(findings),
        "decided_by": state.get("decided_by", "rule"),
        "rule": state.get("rule", "checked"),
        "warnings": state.get("warnings") or [],
    }

    if detail == "minimal":
        return base

    summary = {
        **base,
        "findings": [{k: f[k] for k in ("rule", "note", "evidence")} for f in findings],
        "index": {k: index.get(k) for k in ("table", "columns", "ddl", "rule", "note")},
        "executed": bool(state.get("run")),
        "elapsed_sec": (state.get("run") or {}).get("elapsed_sec"),
        "buffers": (state.get("run") or {}).get("buffers"),
        "plan_rows": len(state.get("plan") or []),
        "proposal": (state.get("proposal") or {}).get("text", ""),
    }
    if detail == "summary":
        return summary

    return {
        **summary,
        "sql": sql,
        "plan": state.get("plan") or [],
        "existing_indexes": index.get("existing") or [],
        "run": state.get("run") or {},
    }


def format_plan(plan: list[dict[str, Any]]) -> str:
    """실행계획을 사람이 읽는 표로."""
    if not plan:
        return "(실행계획 없음)"
    lines = [f"{'Id':>3} {'Operation':<34}{'Object':<22}{'Rows':>10} {'Cost':>8}"]
    lines.append("─" * 80)
    for r in plan:
        op = f"{r.get('OPERATION') or ''} {r.get('OPTIONS') or ''}".strip()
        lines.append(
            f"{r.get('ID', ''):>3} {op[:33]:<34}{(r.get('OBJECT_NAME') or '')[:21]:<22}"
            f"{str(r.get('CARDINALITY') or '-'):>10} {str(r.get('COST') or '-'):>8}"
        )
    return "\n".join(lines)


if __name__ == "__main__":
    import argparse
    import json

    ap = argparse.ArgumentParser(description="SQL 튜닝 진단")
    src = ap.add_mutually_exclusive_group(required=True)
    src.add_argument("-q", "--query", help="쿼리 문자열")
    src.add_argument("-f", "--file", help="쿼리 파일 (.sql)")
    ap.add_argument("--run", action="store_true",
                    help="실제로 수행해 시간·실제계획까지 비교 (운영 DB 부하 주의)")
    ap.add_argument("--detail", choices=["full", "summary", "minimal"], default="full")
    ap.add_argument("--json", action="store_true", help="JSON 으로 출력")
    args = ap.parse_args()

    sql = args.query if args.query else read_text(args.file)
    result = run_sqltune(sql, execute=args.run, detail=args.detail)

    if args.json:
        print(json.dumps(result, ensure_ascii=False, indent=2, default=str))
        raise SystemExit(0 if result["safe"] else 1)

    if not result["safe"]:
        print("[거부] " + "; ".join(result["warnings"]))
        raise SystemExit(1)

    print(f"[진단] 문제 {result['finding_count']}건")
    for f in result.get("findings", []):
        print(f"  - {f['rule']:<18} {f['note']}")
        print(f"      근거: {f['evidence'][:90]}")

    if result.get("plan"):
        print("\n[실행계획]")
        print(format_plan(result["plan"]))

    idx = result.get("index") or {}
    if idx.get("ddl"):
        print(f"\n[인덱스 제안] {idx['note']}")
        print(f"  {idx['ddl']}")
        print("  ※ 실행하지 않았다. 검토 후 직접 수행할 것")
    elif idx.get("note"):
        print(f"\n[인덱스] {idx['note']}")

    if result.get("executed"):
        run = result.get("run") or {}
        print(f"\n[수행] {result['elapsed_sec']:.3f}초 (최소값, {SQLTUNE_RUNS}회 중)"
              f" / Buffers {result.get('buffers')}")
        if run.get("plan_text"):
            print(run["plan_text"][:2000])

    if result.get("proposal"):
        print(f"\n[LLM 개선안]\n{result['proposal']}")

    for w in result.get("warnings", []):
        print(f"\n[확인 필요] {w}")
