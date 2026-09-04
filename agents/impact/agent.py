"""impact — 테이블 영향도 조사.

인터페이스가 실패하면 그 테이블이 소스 어디에서 쓰이는지 보고 영향 범위를
가늠하게 된다. 사람이 하던 순서를 그대로 따른다.

    테이블 이름 → 소스에서 찾기 → 그 줄이 속한 SQL 문장을 통째로 잘라내기
    → 읽기/쓰기 판정 → (선택) LLM 이 실제 사용인지 확인

단독 실행:
    python agents/impact/agent.py IF_ORDER_TMP --root SAMPLE
    python agents/impact/agent.py IF_ORDER_TMP --detail summary
    USE_LLM=false python agents/impact/agent.py IF_ORDER_TMP   # 규칙만

왜 문장 단위인가:
    테이블 이름이 나온 줄 하나만 보면 읽는지 쓰는지 알 수 없다.
    INSERT 한 줄, FROM 절 한 줄만 보고 판단하면 반드시 틀린다.
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

from agents.usage.agent import LANG_BY_SUFFIX, scan_files  # noqa: E402
from config import IMPACT_MAX_STATEMENTS, SOURCE_ROOTS, USE_LLM  # noqa: E402
from core.cache import cached  # noqa: E402
from core.progress import notify  # noqa: E402
from core.sqlstmt import classify, masks_strings, statement_at  # noqa: E402
from core.text import ident_pattern, read_text_with_encoding, strip_comments  # noqa: E402

Detail = Literal["full", "summary", "minimal"]


class ImpactState(TypedDict, total=False):
    table: str
    root_label: str
    root_path: str
    files_scanned: int
    statements: list[dict[str, Any]]
    unreadable: list[str]
    decided_by: str
    rule: str
    warnings: list[str]


# --------------------------------------------------------------------------
# 노드 1 — 루트 결정
# --------------------------------------------------------------------------


def resolve_root(state: ImpactState) -> ImpactState:
    label = (state.get("root_label") or "").strip()
    if label:
        if label not in SOURCE_ROOTS:
            return {
                "root_path": "",
                "decided_by": "fallback",
                "rule": "unknown-root",
                "warnings": [
                    f"'{label}' 은 설정에 없는 소스 루트다 — 확인 필요 "
                    f"(설정된 루트: {', '.join(SOURCE_ROOTS)})"
                ],
            }
        return {"root_label": label, "root_path": SOURCE_ROOTS[label]}
    first = next(iter(SOURCE_ROOTS.items()))
    return {"root_label": first[0], "root_path": first[1]}


# --------------------------------------------------------------------------
# 노드 2 — 문장 수집
# --------------------------------------------------------------------------


def collect(state: ImpactState) -> ImpactState:
    """테이블 이름이 나온 줄을 찾아 그 줄이 속한 SQL 문장을 잘라낸다."""
    if not state.get("root_path"):
        return {}

    table = (state.get("table") or "").strip()
    if not table:
        return {
            "statements": [],
            "decided_by": "fallback",
            "rule": "empty-table",
            "warnings": ["테이블 이름이 비어 있다 — 확인 필요"],
        }

    notify(f"{state['root_label']} 소스에서 {table} 찾는 중")
    files = scan_files(state["root_path"])
    # SQL 식별자는 대소문자를 가리지 않는다. FROM if_order_tmp 와
    # FROM IF_ORDER_TMP 는 같은 테이블이다. 대소문자를 따지면 소스마다
    # 표기가 달라 통째로 놓친다.
    pattern = ident_pattern(table, re.IGNORECASE)
    statements: list[dict[str, Any]] = []
    unreadable: list[str] = []
    seen: set[tuple[str, int]] = set()

    for path in files:
        try:
            text, _enc = read_text_with_encoding(path)
        except OSError as e:
            unreadable.append(f"{path}: {e}")
            continue

        lang = LANG_BY_SUFFIX.get(Path(path).suffix.lower(), "c")
        # java/js 는 SQL 이 문자열 안에 있어 문자열을 지우면 안 된다.
        body = strip_comments(text, lang, mask_strings=masks_strings(lang))

        for no, line in enumerate(body.splitlines(), start=1):
            if not pattern.search(line):
                continue
            stmt = statement_at(text, no, lang)
            # 한 문장에 테이블이 두 번 나오면 문장도 두 번 잡힌다. 시작 줄로 묶는다.
            key = (path, stmt.start_line)
            if key in seen:
                continue
            seen.add(key)

            info = classify(stmt.sql, table, lang)
            statements.append(
                {
                    "file": path,
                    "line": stmt.hit_line,
                    "start_line": stmt.start_line,
                    "end_line": stmt.end_line,
                    "complete": stmt.complete,
                    "kind": info.kind,
                    "role": info.role,
                    "decided_by": "rule",
                    "rule": info.rule,
                    "evidence": info.evidence,
                    "sql": stmt.sql,
                }
            )

    warnings = [f"읽지 못한 파일 {len(unreadable)}건 — 확인 필요"] if unreadable else []

    if not files:
        return {
            "files_scanned": 0, "statements": [], "unreadable": unreadable,
            "decided_by": "fallback", "rule": "no-files",
            "warnings": warnings
            + [f"'{state['root_path']}' 아래에 스캔할 소스가 없다 — 확인 필요"],
        }

    # 문장이 잘리지 않았는지(세미콜론을 못 찾음) 알려 준다. 잘린 조각으로
    # 판단하면 엉뚱한 결론이 난다.
    partial = [s for s in statements if not s["complete"]]
    if partial:
        warnings.append(
            f"문장 끝(세미콜론)을 찾지 못한 조각 {len(partial)}건 — 확인 필요"
        )

    return {
        "files_scanned": len(files),
        "statements": statements,
        "unreadable": unreadable,
        "decided_by": "rule",
        "rule": "statements-found" if statements else "no-hit",
        "warnings": warnings,
    }


# --------------------------------------------------------------------------
# 노드 3 — LLM 확인 (선택)
# --------------------------------------------------------------------------


def verify(state: ImpactState) -> ImpactState:
    """LLM 에게 '이 문장이 정말 그 테이블을 쓰는가'를 확인시킨다.

    규칙이 이미 판정했고, LLM 은 그 판정을 뒤집지 않는다. 다만 규칙과
    다르게 보면 '확인 필요'로 표시한다 — 조용히 한쪽을 고르면 오판을
    영영 못 찾는다.

    비용 때문에 앞쪽 몇 건만 본다. 로컬 모델은 문장 하나에 수 초가 걸린다.
    """
    statements = state.get("statements") or []
    if not USE_LLM or not statements:
        return {}
    notify(f"문장 {min(len(statements), IMPACT_MAX_STATEMENTS)}건 LLM 확인 시작")

    warnings = list(state.get("warnings") or [])
    table = state.get("table", "")
    checked = 0
    try:
        from pydantic import BaseModel, Field

        from core.llm import invoke_structured

        class Verdict(BaseModel):
            uses_table: bool = Field(description="이 SQL이 해당 테이블을 실제로 사용하는가")
            role: Literal["read", "write", "unknown"] = Field(description="읽기/쓰기")
            note: str = Field(description="한 문장 근거")

        targets = statements[:IMPACT_MAX_STATEMENTS]
        for idx, s in enumerate(targets, start=1):
            # 판정 하나에 수십 초가 걸린다. 몇 번째인지 알려야 기다릴 수 있다.
            notify(f"SQL 문장 판정 중 {idx}/{len(targets)} "
                    f"({Path(s['file']).name}:{s.get('start_line')})")
            prompt = (
                f"아래 SQL 문장이 테이블 '{table}' 을 실제로 사용하는지 판단해라.\n"
                "주석이나 다른 테이블 이름에 스쳐 나온 것이면 사용이 아니다.\n"
                "읽기(SELECT/FROM/JOIN)인지 쓰기(INSERT/UPDATE/DELETE/MERGE)인지도 말해라.\n\n"
                f"{s['sql'][:2000]}"
            )
            v, note = invoke_structured(Verdict, prompt)
            if note and note not in warnings:
                warnings.append(note)
            s["llm"] = {"uses_table": v.uses_table, "role": v.role, "note": v.note}
            checked += 1
            # 규칙과 어긋나면 그대로 남긴다. 어느 쪽이 맞는지는 사람이 본다.
            if not v.uses_table or (
                s["role"] != "unknown" and v.role != s["role"]
            ):
                s["decided_by"] = "rule+llm"
                s["conflict"] = True
                warnings.append(
                    f"{Path(s['file']).name}:{s['start_line']} 규칙은 "
                    f"{s['role']}, LLM 은 "
                    f"{'사용 아님' if not v.uses_table else v.role} — 확인 필요"
                )
        return {"statements": statements, "warnings": warnings}
    except Exception as e:
        warnings.append(
            f"LLM 확인 실패({checked}/{len(statements)}건 확인) — 확인 필요: "
            f"{type(e).__name__}: {e}"
        )
        return {"statements": statements, "warnings": warnings}


@cached(ttl=3600, maxsize=1, key=lambda: "graph")
def _graph():
    g = StateGraph(ImpactState)
    g.add_node("resolve_root", resolve_root)
    g.add_node("collect", collect)
    g.add_node("verify", verify)
    g.add_edge(START, "resolve_root")
    g.add_edge("resolve_root", "collect")
    g.add_edge("collect", "verify")
    g.add_edge("verify", END)
    return g.compile()


def run_impact(
    table: str, root: str = "", detail: Detail = "full"
) -> dict[str, Any]:
    """진입점.

        full    : SQL 문장 전문 포함 (화면용, MCP 리소스로 나간다)
        summary : 건수와 파일 목록 (LLM 컨텍스트에 들어간다)
        minimal : 읽기/쓰기 건수만

    full 을 챗봇 툴에서 돌려주면 SQL 수십 개가 매 턴 컨텍스트를 먹는다.
    """
    state: ImpactState = _graph().invoke({"table": table, "root_label": root})
    stmts = state.get("statements") or []

    writes = [s for s in stmts if s["role"] == "write"]
    reads = [s for s in stmts if s["role"] == "read"]
    unknown = [s for s in stmts if s["role"] == "unknown"]
    base = {
        "table": table,
        "write_count": len(writes),
        "read_count": len(reads),
        "unknown_count": len(unknown),
        "decided_by": state.get("decided_by"),
        "rule": state.get("rule"),
    }

    if detail == "minimal":
        return base

    files = sorted({s["file"] for s in stmts})
    if detail == "summary":
        return {
            **base,
            "root": state.get("root_label"),
            "files_scanned": state.get("files_scanned", 0),
            "files": [Path(f).name for f in files[:5]],
            "more_files": max(0, len(files) - 5),
            # 어디서 쓰는지 한 줄씩. SQL 전문은 리소스로 따로 가져간다.
            "places": [
                f"{Path(s['file']).name}:{s['start_line']} {s['kind']}/{s['role']}"
                for s in (writes + reads + unknown)[:10]
            ],
            "warnings": state.get("warnings") or [],
        }

    return {
        **base,
        "root": state.get("root_label"),
        "root_path": state.get("root_path"),
        "files_scanned": state.get("files_scanned", 0),
        "statements": stmts,
        "unreadable": state.get("unreadable") or [],
        "warnings": state.get("warnings") or [],
    }


if __name__ == "__main__":
    import argparse
    import json

    ap = argparse.ArgumentParser(description="테이블 영향도 조사")
    ap.add_argument("table", help="조사할 테이블 이름")
    ap.add_argument("--root", default="", help=f"소스 루트 ({', '.join(SOURCE_ROOTS)})")
    ap.add_argument("--detail", choices=["full", "summary", "minimal"], default="full")
    ap.add_argument("--sql", action="store_true", help="SQL 문장을 그대로 출력")
    args = ap.parse_args()

    result = run_impact(args.table, root=args.root, detail=args.detail)

    if args.sql and result.get("statements"):
        for s in result["statements"]:
            print(f"── {s['file']}:{s['start_line']}~{s['end_line']} "
                  f"[{s['kind']}/{s['role']}] 규칙={s['rule']}")
            print(s["sql"])
            if s.get("llm"):
                print(f"   LLM: {s['llm']}")
            print()
    else:
        print(json.dumps(result, ensure_ascii=False, indent=2, default=str))

    print(f"[영향] 쓰기 {result['write_count']}건 / 읽기 {result['read_count']}건"
          f" / 판정불가 {result['unknown_count']}건")
    for w in result.get("warnings", []):
        print(f"  [확인 필요] {w}")
