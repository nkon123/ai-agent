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
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any, Literal, TypedDict

_ROOT = Path(__file__).resolve().parents[2]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from langgraph.graph import END, START, StateGraph  # noqa: E402

from config import (  # noqa: E402
    SQLTUNE_CANDIDATES,
    SQLTUNE_COMPARE,
    SQLTUNE_COMPARE_COUNT,
    SQLTUNE_EXECUTE,
    SQLTUNE_INTERLEAVE_RUNS,
    SQLTUNE_PARALLEL_COUNT,
    SQLTUNE_PARALLEL_EXPLAIN,
    SQLTUNE_PARALLEL_MAX,
    SQLTUNE_MAX_ROWS,
    SQLTUNE_RULES_FILE,
    SQLTUNE_RUNS,
    SQLTUNE_TIMEOUT_SEC,
    USE_LLM,
)
from core import oracle  # noqa: E402
from core.cache import cached  # noqa: E402
from core.progress import notify  # noqa: E402
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


def relevant_rules(rules_text: str, names: list[str]) -> str:
    """기준 문서에서 이번에 걸린 규칙의 '절 전체'를 추린다.

    문서 전체를 넣으면 프롬프트가 커지고, 추론 모델은 사고 토큰까지 더해
    컨텍스트를 넘긴다(본문이 비어 나온다). 걸린 규칙만 보내면 충분하다 —
    LLM 은 우리가 찾은 문제를 고치는 역할이지 문서를 통독할 이유가 없다.

    줄 단위가 아니라 절(### 제목 ~ 다음 제목) 단위로 잘라내는 이유:
    고친 예시와 주의사항이 제목 아래 여러 줄에 걸쳐 있다. 규칙 이름이
    있는 줄만 뽑으면 정작 '어떻게 고치라'는 부분이 빠진다.

    문서 앞머리(버전 차이표)와 8항(비교 기준)은 항상 넣는다. 어느 규칙에
    걸렸든 11g 에서 쓸 수 없는 문법을 내놓거나 건수가 달라지는 변경을
    하면 안 되기 때문이다.
    """
    lines = rules_text.splitlines()

    # 절 경계: '### ' 로 시작하는 제목
    sections: list[tuple[str, list[str]]] = []
    head: list[str] = []
    title, body = "", []
    for line in lines:
        if line.startswith("### "):
            if title:
                sections.append((title, body))
            title, body = line, [line]
        elif title:
            body.append(line)
        else:
            head.append(line)
    if title:
        sections.append((title, body))

    wanted = [n for n in names if n]
    keep: list[str] = []
    for sec_title, sec_body in sections:
        if any(n in sec_title for n in wanted):
            keep.extend(sec_body)

    # 앞머리(버전 차이)와 비교 기준은 규칙과 무관하게 늘 필요하다.
    always = [ln for ln in head if ln.strip()]
    tail_at = next(
        (i for i, ln in enumerate(lines) if ln.startswith("## 8.")), len(lines)
    )
    always += lines[tail_at:]

    text = "\n".join(always + [""] + keep).strip()
    # 걸린 규칙이 문서에 없으면(이름을 바꾼 경우) 앞부분이라도 준다.
    return text if len(text) > 200 else rules_text[:1500]


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
    binds: dict[str, Any]
    execute: bool
    compare: bool
    compare_count: bool
    parallel_explain: bool
    parallel_count: bool
    interleave: bool
    candidates: list[dict[str, Any]]
    comparison: list[dict[str, Any]]
    best: str
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
    notify("쿼리 정적 진단 중")
    return {"findings": check_text_rules(state["sql"])}


def plan_check(state: TuneState) -> TuneState:
    """실행계획을 받아 규칙으로 본다. 쿼리를 실행하지 않는다."""
    if not state.get("safe"):
        return {}

    warnings = list(state.get("warnings") or [])
    if not oracle.is_configured():
        warnings.append("Oracle 설정이 비어 있어 실행계획을 못 봤다 — 확인 필요")
        return {"plan": [], "warnings": warnings}

    notify("실행계획 확인 중")
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


def missing_binds(sql: str, binds: dict[str, Any] | None) -> list[str]:
    """값이 없는 바인드 이름. 실행 계열을 건너뛸지 판단하는 데 쓴다."""
    given = {k.lstrip(":") for k in (binds or {})}
    return [n for n in oracle.bind_names(sql) if n not in given]


def run_check(state: TuneState) -> TuneState:
    """실제로 수행해 시간과 실제 계획을 받는다. 기본은 하지 않는다.

    실행은 곧 운영 DB 부하다. 사람이 --run 으로 켤 때만 돈다.
    """
    if not state.get("safe") or not state.get("execute"):
        return {}

    # 후보 비교를 할 거면 여기서 재지 않는다. compare 가 원본까지 같은
    # 조건으로 재기 때문에, 여기서 또 재면 느린 원본을 쓸데없이 한 번 더
    # 실행하는 셈이다(교차 반복 순서도 흐트러진다).
    if state.get("compare") and state.get("candidates"):
        return {}

    warnings = list(state.get("warnings") or [])
    if not oracle.is_configured():
        warnings.append("Oracle 설정이 비어 있어 수행 비교를 못 했다 — 확인 필요")
        return {"warnings": warnings}

    # 바인드 값이 없으면 실행하지 않는다. NULL 로 채워 돌리면 조건이 맞지
    # 않아 0건이 나오는데, 그걸 '빠르다'로 읽으면 잘못된 결론이 된다.
    lack = missing_binds(state["sql"], state.get("binds"))
    if lack:
        warnings.append(
            f"바인드 값이 없어 수행하지 않았다 — 확인 필요 ({', '.join(':' + b for b in lack)}). "
            "--bind 이름=값 으로 넘길 것"
        )
        return {"warnings": warnings}

    try:
        result = oracle.execute_with_stats(
            strip_trailing_semicolon(state["sql"]),
            state.get("binds") or {},
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
    """LLM 에게 기준 문서를 주고 개선 후보를 받는다.

    LLM 이 규칙 판정을 뒤집지 않는다. 규칙이 찾은 문제를 근거로 고친
    쿼리를 '후보'로 낼 뿐이고, 좋아졌는지는 플랜과 건수가 정한다.

    구조화 출력(json_schema)을 쓰는 이유: 답변 문장에서 SQL 을 뽑아내려면
    파싱이 필요하고, 소형 모델은 그 형식을 자주 어긴다. 스키마를 강제하면
    문법이 어긋난 JSON 이 나올 수 없다.
    """
    if not state.get("safe") or not USE_LLM or not state.get("compare"):
        return {}

    warnings = list(state.get("warnings") or [])
    findings = state.get("findings") or []
    try:
        from pydantic import BaseModel, Field

        from core.llm import invoke_structured

        class Candidate(BaseModel):
            sql: str = Field(description="고친 SELECT 쿼리 전문")
            reason: str = Field(description="무엇을 왜 고쳤는지 한두 문장")
            rules: list[str] = Field(
                default_factory=list, description="근거로 삼은 규칙명"
            )

        class Candidates(BaseModel):
            candidates: list[Candidate] = Field(description="개선 후보 목록")

        notify("튜닝 기준으로 개선 후보 만드는 중 (수십 초 걸린다)")
        # 걸린 규칙만 보낸다. 문서 전체(3KB+)를 넣으면 추론 토큰까지 더해
        # 컨텍스트를 넘겨 본문이 비어서 나온다.
        rules = relevant_rules(
            load_rules(SQLTUNE_RULES_FILE), [f["rule"] for f in findings]
        )
        issues = "\n".join(
            f"- {f['rule']}: {f['note']} (근거: {f['evidence']})" for f in findings
        ) or "- (규칙이 찾은 문제 없음)"
        prompt = (
            "너는 오라클 SQL 튜닝을 돕는다. 아래 '튜닝 기준'에 있는 항목만 근거로 삼아라.\n"
            "기준에 없는 내용을 지어내지 마라.\n"
            "**결과 건수와 결과 값이 달라지는 변경은 절대 하지 마라.** 건수가 달라지면\n"
            "튜닝이 아니라 버그다. SELECT 문만 만들어라.\n"
            f"후보는 최대 {SQLTUNE_CANDIDATES}개까지.\n\n"
            f"[튜닝 기준]\n{rules[:3000]}\n\n"
            f"[규칙이 찾은 문제]\n{issues}\n\n"
            f"[원본 쿼리]\n{state['sql'][:3000]}\n"
        )
        # 생성 과제라 사고 과정을 끈다. 켜 두면 사고에만 수백 초를 쓰고
        # 본문이 비어 나오는 일이 잦다(실측 392초·534초 실패 → 20초 성공).
        # 판정 과제인 impact 는 반대로 켜 둔다 — 거기서는 정확도가 크게 오른다.
        out, note = invoke_structured(Candidates, prompt, reasoning=False)
        if note:
            warnings.append(note)
        cands: list[dict[str, Any]] = []
        for i, c in enumerate(out.candidates[:SQLTUNE_CANDIDATES], start=1):
            cands.append(
                {
                    "name": f"후보{i}",
                    "sql": c.sql.strip(),
                    "reason": c.reason.strip(),
                    "based_on": list(c.rules or []),
                }
            )
        if not cands:
            warnings.append("LLM 이 개선 후보를 내지 못했다 — 확인 필요")
        return {"candidates": cands, "warnings": warnings}
    except Exception as e:
        warnings.append(f"개선 후보 생성 실패 — 확인 필요: {type(e).__name__}: {e}")
        return {"warnings": warnings}


def _run_maybe_parallel(fn: Any, items: list[Any], parallel: bool) -> list[Any]:
    """items 에 fn 을 적용한다. parallel 이면 스레드로 동시에.

    커넥션은 호출마다 새로 열리므로(core.oracle.get_conn) 스레드끼리
    공유하는 상태가 없다. 다만 세션 수가 늘어나므로 상한을 둔다.
    순서는 입력 순서를 유지한다 — 결과를 후보와 짝지어야 한다.
    """
    if not parallel or len(items) <= 1:
        return [fn(x) for x in items]
    with ThreadPoolExecutor(
        max_workers=min(SQLTUNE_PARALLEL_MAX, len(items))
    ) as pool:
        return list(pool.map(fn, items))


def _measure_plan(sql: str) -> dict[str, Any]:
    """실행계획을 받는다. 쿼리를 실행하지 않아 부하가 거의 없다.

    계획 행을 그대로 들고 간다. Cost 숫자 하나만 남기면 "왜 이 후보가
    나은가" 를 볼 수 없다 — 어느 접근 경로가 바뀌었는지가 핵심이다.
    """
    out: dict[str, Any] = {"cost": None, "plan_rows": 0, "plan": [], "errors": []}
    try:
        plan = oracle.explain_plan(strip_trailing_semicolon(sql))
        out["plan"] = plan
        out["plan_rows"] = len(plan)
        # 루트(ID 0)의 Cost 가 옵티마이저 추정 비용이다.
        for row in plan:
            if row.get("ID") == 0:
                out["cost"] = row.get("COST")
                break
    except Exception as e:
        out["errors"].append(f"explain: {type(e).__name__}: {e}")
    return out


def _measure_count(sql: str, binds: dict[str, Any] | None = None) -> dict[str, Any]:
    """건수만 센다. 경합이 있어도 값은 바뀌지 않는다(결정적)."""
    try:
        return {
            "rows": oracle.count_rows(
                strip_trailing_semicolon(sql), binds or {},
                timeout=SQLTUNE_TIMEOUT_SEC,
            ),
            "errors": [],
        }
    except Exception as e:
        return {"rows": None, "errors": [f"count: {type(e).__name__}: {e}"]}


def _measure_run_once(sql: str, binds: dict[str, Any] | None = None) -> dict[str, Any]:
    """한 번 수행하고 시간과 실제 계획을 받는다.

    이 함수는 절대 병렬로 부르지 않는다. 동시에 돌리면 서로 CPU·I/O·
    버퍼캐시를 뺏어 재려던 수치가 오염된다.
    """
    try:
        r = oracle.execute_with_stats(
            strip_trailing_semicolon(sql),
            binds or {},
            max_rows=SQLTUNE_MAX_ROWS,
            timeout=SQLTUNE_TIMEOUT_SEC,
            runs=1,
        )
        return {
            "elapsed_sec": r["elapsed_sec"],
            "buffers": r["buffers"],
            "truncated": r.get("truncated"),
            "errors": [],
        }
    except Exception as e:
        return {"errors": [f"run: {type(e).__name__}: {e}"]}


def _time_entries(
    entries: list[dict[str, Any]], interleave: bool, binds: dict[str, Any] | None = None
) -> None:
    """수행시간을 잰다. entries 를 제자리에서 갱신한다.

    interleave=True 면 A,B,A,B 순으로 돈다. False 면 A,A,B,B 다.
    연속으로 돌리면 뒤에 측정한 쪽이 앞 쿼리가 데워 놓은 캐시 덕을 본다.
    교차하면 그 편향이 줄어든다.

    각 후보의 값은 여러 회 중 **최소값**을 쓴다. 첫 회에는 하드파싱과
    캐시 적재가 섞이기 때문이다.
    """
    runs = max(1, SQLTUNE_RUNS)
    order = (
        [(r, e) for r in range(runs) for e in entries]
        if interleave
        else [(r, e) for e in entries for r in range(runs)]
    )
    for _round, entry in order:
        if entry.get("rejected"):
            continue
        got = _measure_run_once(entry["sql"], binds)
        entry.setdefault("errors", []).extend(got.get("errors") or [])
        if got.get("elapsed_sec") is None:
            continue
        prev = entry.get("elapsed_sec")
        if prev is None or got["elapsed_sec"] < prev:
            entry["elapsed_sec"] = got["elapsed_sec"]
        # Buffers 는 실행마다 거의 같다. 마지막 값을 쓴다.
        entry["buffers"] = got.get("buffers")
        entry["truncated"] = got.get("truncated")


def _score(entry: dict[str, Any]) -> tuple[float, float, float]:
    """비교 순서: Buffers → 수행시간 → Cost (기준 문서 8항).

    Buffers 를 먼저 보는 이유: 수행 시간은 캐시·부하에 흔들려 같은 쿼리도
    실행마다 다르지만 논리적 읽기는 재현된다. Cost 는 추정치라 마지막이다.
    """
    big = float("inf")
    return (
        float(entry.get("buffers") if entry.get("buffers") is not None else big),
        float(entry.get("elapsed_sec") if entry.get("elapsed_sec") is not None else big),
        float(entry.get("cost") if entry.get("cost") is not None else big),
    )


def compare(state: TuneState) -> TuneState:
    """원본과 후보들을 같은 기준으로 재고 순위를 매긴다.

    3단계로 나눈다. 단계마다 병렬 여부가 다르다.

        1) EXPLAIN  — 병렬 가능. 실행이 아니라 파싱이라 부하가 거의 없다
        2) COUNT    — 병렬 가능하지만 기본은 순차. 건수는 결정적이라
                      경합이 있어도 값이 안 바뀌지만 순간 부하가 커진다
        3) 수행시간  — **항상 순차**. 동시에 돌리면 서로 자원을 뺏어
                      재려던 수치가 오염된다. 대신 교차 반복으로 캐시 편향을 줄인다

    후보는 원본과 똑같은 안전 게이트를 통과해야 한다. LLM 이 만든 SQL 도
    사용자 입력과 다를 바 없다.
    """
    if not state.get("safe") or not state.get("compare"):
        return {}

    warnings = list(state.get("warnings") or [])
    if not oracle.is_configured():
        warnings.append("Oracle 설정이 비어 있어 후보를 비교하지 못했다 — 확인 필요")
        return {"warnings": warnings}

    binds = state.get("binds") or {}
    want_count = bool(state.get("compare_count"))
    want_run = bool(state.get("execute"))
    # 실행 계열은 바인드 값이 있어야 한다. 없으면 계획 비교까지만 한다.
    lack = missing_binds(state["sql"], binds)
    if lack and (want_count or want_run):
        warnings.append(
            f"바인드 값이 없어 건수·수행 비교를 건너뛰었다 — 확인 필요 "
            f"({', '.join(':' + b for b in lack)}). --bind 이름=값 으로 넘길 것"
        )
        want_count = want_run = False
    par_explain = bool(state.get("parallel_explain", SQLTUNE_PARALLEL_EXPLAIN))
    par_count = bool(state.get("parallel_count", SQLTUNE_PARALLEL_COUNT))
    interleave = bool(state.get("interleave", SQLTUNE_INTERLEAVE_RUNS))

    entries: list[dict[str, Any]] = [
        {"name": "원본", "sql": state["sql"], "reason": "", "based_on": [], "errors": []}
    ]
    for cand in state.get("candidates") or []:
        ok, why = is_safe_select(cand["sql"])
        entry = {**cand, "errors": []}
        if not ok:
            entry["rejected"] = f"안전 검사 탈락: {why}"
            warnings.append(f"{cand['name']} 안전 검사 탈락 — {why}")
        entries.append(entry)

    live = [e for e in entries if not e.get("rejected")]

    # ---- 1단계: 실행계획 -------------------------------------------------
    notify(f"후보 {len(live)}개 실행계획 비교 중")
    for entry, got in zip(
        live, _run_maybe_parallel(_measure_plan, [e["sql"] for e in live], par_explain)
    ):
        entry.update({k: v for k, v in got.items() if k != "errors"})
        entry["errors"].extend(got["errors"])

    # ---- 2단계: 건수 -----------------------------------------------------
    if want_count:
        notify(f"결과 건수 확인 중 ({len(live)}개)")
        for entry, got in zip(
            live,
            _run_maybe_parallel(
                lambda q: _measure_count(q, binds), [e["sql"] for e in live], par_count
            ),
        ):
            entry["rows"] = got["rows"]
            entry["errors"].extend(got["errors"])

        baseline_rows = entries[0].get("rows")
        for entry in live[1:]:
            if baseline_rows is None:
                continue
            if entry.get("rows") is None:
                entry["rejected"] = "건수를 세지 못했다"
            elif entry["rows"] != baseline_rows:
                # 건수가 달라지면 튜닝이 아니라 버그다. 순위에 넣지 않는다.
                entry["rejected"] = (
                    f"결과 건수가 다르다 (원본 {baseline_rows}건 → "
                    f"{entry['rows']}건) — 튜닝이 아니라 버그다"
                )
                warnings.append(f"{entry['name']} {entry['rejected']}")
        if par_count:
            warnings.append(
                f"건수를 병렬로 셌다(동시 {min(SQLTUNE_PARALLEL_MAX, len(live))}개) "
                "— 순간 부하가 그만큼 컸다"
            )

    # ---- 3단계: 수행시간 -------------------------------------------------
    if want_run:
        notify(f"수행시간 측정 중 ({len(live)}개 × {SQLTUNE_RUNS}회, 교차 반복)")
        _time_entries([e for e in live if not e.get("rejected")], interleave, binds)
        if not interleave and len(live) > 1:
            warnings.append(
                "수행시간을 교차 반복 없이 쟀다 — 뒤에 측정한 쪽이 앞 쿼리가 "
                "데워 놓은 캐시 덕을 봤을 수 있다"
            )

    ranked = [e for e in entries if not e.get("rejected") and not e.get("errors")]
    ranked.sort(key=_score)
    best = ranked[0]["name"] if ranked else ""

    if not want_count and state.get("candidates"):
        # 건수를 안 봤으면 '같은 결과'라는 보장이 없다. 그 사실을 남긴다.
        warnings.append(
            "결과 건수를 비교하지 않았다 — 후보가 같은 결과를 내는지 확인할 것 "
            "(--count 로 켤 수 있다)"
        )
    return {"comparison": entries, "best": best, "warnings": warnings}


@cached(ttl=3600, maxsize=1, key=lambda: "graph")
def _graph():
    g = StateGraph(TuneState)
    for name, fn in (
        ("guard", guard), ("static_check", static_check), ("plan_check", plan_check),
        ("index_check", index_check), ("run_check", run_check),
        ("propose", propose), ("compare", compare),
    ):
        g.add_node(name, fn)
    g.add_edge(START, "guard")
    g.add_edge("guard", "static_check")
    g.add_edge("static_check", "plan_check")
    g.add_edge("plan_check", "index_check")
    # propose(후보 생성)를 run_check 앞에 둔다. 그래야 run_check 가
    # '비교를 할 것인지'를 알고 중복 측정을 건너뛸 수 있다.
    g.add_edge("index_check", "propose")
    g.add_edge("propose", "run_check")
    g.add_edge("run_check", "compare")
    g.add_edge("compare", END)
    return g.compile()


def run_sqltune(
    sql: str,
    binds: dict[str, Any] | None = None,
    execute: bool | None = None,
    compare_candidates: bool | None = None,
    compare_count: bool | None = None,
    parallel_explain: bool | None = None,
    parallel_count: bool | None = None,
    interleave: bool | None = None,
    detail: Detail = "full",
) -> dict[str, Any]:
    """진입점.

        full    : 플랜 행과 SQL 전문 포함 (화면용, MCP 리소스로 나간다)
        summary : 문제 목록과 인덱스 제안 (LLM 컨텍스트에 들어간다)
        minimal : 건수만

    binds              : 바인드 값 {"if_key": "EAIIF0001234"}.
                         실행계획은 값 없이도 되지만 건수·수행 비교에는 필요하다.
    execute            : 실제 수행 여부 (None 이면 config.SQLTUNE_EXECUTE)
    compare_candidates : 개선 후보를 만들어 비교 (None 이면 config.SQLTUNE_COMPARE)
    compare_count      : 결과 건수까지 비교 (None 이면 config.SQLTUNE_COMPARE_COUNT).
                         건수 비교는 원본·후보를 각각 COUNT(*)로 감싸 실행한다.
    parallel_explain   : 실행계획을 병렬로 (기본 켜짐, 부하 거의 없음)
    parallel_count     : 건수를 병렬로 (기본 꺼짐, 순간 부하가 후보 수만큼)
    interleave         : 수행시간을 교차 반복(A,B,A,B)으로 (기본 켜짐)

    수행시간 측정은 어떤 설정에서도 병렬로 하지 않는다 — 동시에 돌리면
    서로 자원을 뺏어 재려던 수치가 오염된다.
    """
    want_count = SQLTUNE_COMPARE_COUNT if compare_count is None else bool(compare_count)
    state: TuneState = _graph().invoke(
        {
            "sql": sql,
            "binds": binds or {},
            "execute": SQLTUNE_EXECUTE if execute is None else bool(execute),
            "compare": SQLTUNE_COMPARE if compare_candidates is None
            else bool(compare_candidates),
            "compare_count": want_count,
            "parallel_explain": SQLTUNE_PARALLEL_EXPLAIN
            if parallel_explain is None else bool(parallel_explain),
            "parallel_count": SQLTUNE_PARALLEL_COUNT
            if parallel_count is None else bool(parallel_count),
            "interleave": SQLTUNE_INTERLEAVE_RUNS
            if interleave is None else bool(interleave),
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
        "best": state.get("best", ""),
        # 계획 행은 summary 에 넣지 않는다 — 매 턴 컨텍스트를 먹는다.
        # 화면·CLI 는 detail="full" 의 comparison 에서 가져간다.
        "compared": [
            {
                "name": c["name"],
                "cost": c.get("cost"),
                "rows": c.get("rows"),
                "elapsed_sec": c.get("elapsed_sec"),
                "buffers": c.get("buffers"),
                "rejected": c.get("rejected", ""),
                "reason": c.get("reason", ""),
            }
            for c in (state.get("comparison") or [])
        ],
    }
    if detail == "summary":
        return summary

    return {
        **summary,
        "sql": sql,
        "plan": state.get("plan") or [],
        "existing_indexes": index.get("existing") or [],
        "run": state.get("run") or {},
        "candidates": state.get("candidates") or [],
        "comparison": state.get("comparison") or [],
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
    ap.add_argument("--compare", action="store_true",
                    help="LLM 개선 후보를 만들어 원본과 플랜을 비교")
    ap.add_argument("--count", action="store_true",
                    help="결과 건수까지 비교 (원본·후보를 COUNT(*)로 감싸 실행)")
    ap.add_argument("--no-parallel-explain", action="store_true",
                    help="실행계획을 순차로 받는다 (기본은 병렬)")
    ap.add_argument("--parallel-count", action="store_true",
                    help="건수를 병렬로 센다 (빠르지만 순간 부하가 후보 수만큼 커진다)")
    ap.add_argument("--bind", action="append", default=[], metavar="이름=값",
                    help="바인드 값 (여러 번 쓸 수 있다). 예: --bind if_key=EAIIF0001234")
    ap.add_argument("--no-interleave", action="store_true",
                    help="수행시간을 교차 반복하지 않는다 (기본은 A,B,A,B 교차)")
    ap.add_argument("--detail", choices=["full", "summary", "minimal"], default="full")
    ap.add_argument("--json", action="store_true", help="JSON 으로 출력")
    args = ap.parse_args()

    sql = args.query if args.query else read_text(args.file)
    binds: dict[str, Any] = {}
    for item in args.bind:
        if "=" not in item:
            print(f"[거부] --bind 는 이름=값 형식이다: {item}")
            raise SystemExit(2)
        k, v = item.split("=", 1)
        binds[k.strip().lstrip(":")] = v
    result = run_sqltune(
        sql,
        binds=binds,
        execute=args.run,
        # --count 는 후보 비교의 일부다. 따로 켜면 비교도 함께 켠다.
        compare_candidates=args.compare or args.count,
        compare_count=args.count,
        parallel_explain=not args.no_parallel_explain,
        parallel_count=args.parallel_count,
        interleave=not args.no_interleave,
        detail=args.detail,
    )

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
        print("\n[실행계획] 원본")
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

    comparison = result.get("comparison") or []
    if comparison:
        print("\n[후보 비교]")
        print(f"  {'후보':<8}{'Cost':>8}{'건수':>10}{'시간(초)':>10}{'Buffers':>10}  비고")
        print("  " + "─" * 76)
        for c in comparison:
            note = c.get("rejected") or c.get("reason", "")
            elapsed = c.get("elapsed_sec")
            elapsed_s = f"{elapsed:.3f}" if elapsed is not None else "-"
            rows = c.get("rows")
            print(
                f"  {c['name']:<8}{str(c.get('cost') or '-'):>8}"
                f"{str(rows if rows is not None else '-'):>10}"
                f"{elapsed_s:>10}{str(c.get('buffers') or '-'):>10}  {note[:40]}"
            )
        if result.get("best"):
            print(f"\n  → 가장 나은 것: {result['best']}")
        for c in comparison:
            if c.get("sql") and c["name"] != "원본" and not c.get("rejected"):
                print(f"\n  [{c['name']}] {c.get('reason','')}")
                print("  " + c["sql"].replace("\n", "\n  "))
                if c.get("plan"):
                    # 후보의 계획도 보여 준다. 어느 접근 경로가 달라졌는지가
                    # Cost 숫자보다 중요하다.
                    print("  " + format_plan(c["plan"]).replace("\n", "\n  "))

    for w in result.get("warnings", []):
        print(f"\n[확인 필요] {w}")
