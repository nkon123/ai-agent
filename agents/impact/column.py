"""컬럼 영향도 — 테이블의 컬럼 하나가 소스 어디에서 실제로 쓰이는가.

테이블 영향도(agent.py)가 '어느 프로그램이 이 테이블을 쓰나'라면, 이쪽은 한
단계 더 들어가 '그 프로그램에서 이 컬럼을 쓰나'다. 사람이 하던 순서 그대로다.

    테이블이 나오는 파일 → 그 파일에서 컬럼이 나온 줄 전부
    → 규칙으로 1차 분류(주석·문자열·같은 문장에 테이블·다른 코드)
    → 문장 단위로 LLM 이 '이 테이블의 컬럼으로 쓰였는가'를 판정 → 그리드

찾은 줄은 판정과 상관없이 전부 남긴다. 어느 프로그램에서 이름이 나왔는지는
사람이 다 봐야 하고, 실제 영향 여부만 판정으로 가른다.

판정(verdict)은 세 값이다 — '아니다'와 '모른다'를 섞지 않는다.
    yes      영향 있음   그 테이블의 컬럼으로 쓰였다
    no       영향 없음   주석이거나, 다른 테이블의 같은 이름 컬럼·변수다
    unknown  확인 필요   LLM 이 판단 못 함 / LLM 미확인 / LLM 실패

단독 실행:
    python agents/impact/column.py IF_ORDER_TMP TOTAL_AMT --root SAMPLE
    USE_LLM=false python agents/impact/column.py IF_ORDER_TMP TOTAL_AMT
"""

from __future__ import annotations

import re
import sys
from pathlib import Path
from typing import Any, Literal

_ROOT = Path(__file__).resolve().parents[2]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from agents.usage.agent import LANG_BY_SUFFIX, scan_files  # noqa: E402
from config import (  # noqa: E402
    COLUMN_IMPACT_MAX_HITS,
    COLUMN_IMPACT_MAX_LLM,
    SOURCE_ROOTS,
    USE_LLM,
)
from core.cache import TTLCache  # noqa: E402
from core.progress import notify  # noqa: E402
from core.sqlstmt import bare_table_name, classify, masks_strings, statement_at  # noqa: E402
from core.text import ident_pattern, read_text_with_encoding, strip_comments  # noqa: E402

Detail = Literal["full", "summary"]

VERDICT_LABEL = {"yes": "영향 있음", "no": "영향 없음", "unknown": "확인 필요"}
USAGE_LABEL = {
    "select": "조회", "where": "조건", "insert": "입력", "update": "수정",
    "join": "조인", "order_group": "정렬·그룹", "other": "기타", "none": "-",
}
# 규칙 1차 분류. 그리드의 '위치' 칸에 그대로 나간다.
PLACE_LABEL = {
    "comment": "주석", "string": "문자열", "same-stmt": "테이블과 같은 문장",
    "other-code": "다른 코드",
}


def split_names(table: str, column: str) -> tuple[str, str, str]:
    """(테이블, 컬럼, 떼어 낸 앞부분). 'IF_ORDER_TMP.TOTAL_AMT' 한 덩어리도 받는다.

    모델이 컬럼 자리에 '테이블.컬럼' 을 통째로 넣는 일이 흔하다. 컬럼은 마지막
    조각, 그 앞은 테이블이 비었을 때만 테이블로 쓴다. 테이블의 스키마·시스템은
    테이블 영향도와 같은 bare_table_name 으로 뗀다.
    """
    col = (column or "").strip()
    tab = (table or "").strip()
    if "." in col:
        head, col = col.rsplit(".", 1)
        if not tab:
            tab = head
    tab, qualifier = bare_table_name(tab)
    return tab.upper(), col.strip().strip('"').upper(), qualifier


# --------------------------------------------------------------------------
# 1단계 — 찾기와 규칙 분류
# --------------------------------------------------------------------------


def _place(nocomment_line: str, masked_line: str, pat: re.Pattern[str]) -> str:
    """이 줄의 컬럼 이름이 어디에 있는가: 주석 / 문자열 / 코드.

    세 사본을 비교한다 — 원문에만 있으면 주석, 주석만 지운 사본에는 있는데
    문자열까지 지운 사본에 없으면 문자열 안이다. java/js 는 SQL 이 문자열
    안에 있으므로 문자열을 지우지 않는다(masked == nocomment).
    """
    if pat.search(masked_line):
        return "code"
    if pat.search(nocomment_line):
        return "string"
    return "comment"


def find_hits(table: str, column: str, root_path: str) -> dict[str, Any]:
    """테이블이 나오는 파일에서 컬럼이 나온 줄을 전부 찾아 규칙으로 분류한다."""
    files = scan_files(root_path)
    tab_pat = ident_pattern(table, re.IGNORECASE)
    # SQL 식별자는 대소문자를 가리지 않는다(테이블 영향도와 같다).
    col_pat = ident_pattern(column, re.IGNORECASE)
    hits: list[dict[str, Any]] = []
    unreadable: list[str] = []
    table_files = 0
    truncated = False

    for path in files:
        try:
            text, _enc = read_text_with_encoding(path)
        except OSError as e:
            unreadable.append(f"{path}: {e}")
            continue
        lang = LANG_BY_SUFFIX.get(Path(path).suffix.lower(), "c")
        try:
            nocomment = strip_comments(text, lang)
            masked = strip_comments(text, lang, mask_strings=masks_strings(lang))
        except ValueError:
            nocomment = masked = text
        # 주석에만 테이블이 나오는 파일은 그 테이블을 쓰는 프로그램이 아니다.
        if not tab_pat.search(masked):
            continue
        table_files += 1
        if not col_pat.search(text):
            continue

        raw_lines = text.splitlines()
        nc_lines = nocomment.splitlines()
        mk_lines = masked.splitlines()
        for no, raw in enumerate(raw_lines, start=1):
            if not col_pat.search(raw):
                continue
            if len(hits) >= COLUMN_IMPACT_MAX_HITS:
                truncated = True
                break
            nc = nc_lines[no - 1] if no - 1 < len(nc_lines) else ""
            mk = mk_lines[no - 1] if no - 1 < len(mk_lines) else ""
            where = _place(nc, mk, col_pat)
            hit: dict[str, Any] = {
                "file": path, "program": Path(path).name, "lang": lang, "line": no,
                "code": raw.strip()[:300], "start_line": no, "end_line": no,
                "stmt_kind": "", "table_role": "", "sql": "",
            }
            if where == "comment":
                hit.update(place="comment", rule_verdict="no", rule="comment-only")
            else:
                stmt = statement_at(text, no, lang)
                try:
                    body = strip_comments(stmt.sql, lang, mask_strings=masks_strings(lang))
                except ValueError:
                    body = stmt.sql
                same = bool(tab_pat.search(body))
                hit.update(start_line=stmt.start_line, end_line=stmt.end_line, sql=stmt.sql,
                           complete=stmt.complete)
                if where == "string":
                    # Pro*C·PL/SQL 의 문자열 안 — 로그 문구일 수도, 동적 SQL 일 수도 있다.
                    hit.update(place="string", rule_verdict="unknown", rule="in-string")
                elif same:
                    info = classify(stmt.sql, table, lang)
                    hit.update(place="same-stmt", stmt_kind=info.kind, table_role=info.role,
                               rule_verdict="yes" if stmt.complete else "unknown",
                               rule="same-statement" if stmt.complete else "partial-statement")
                else:
                    # 테이블을 쓰는 파일이지만 이 문장에는 테이블이 없다 — 다른 테이블의
                    # 같은 이름 컬럼이거나 변수일 가능성이 크다. 단정하지 않는다.
                    hit.update(place="other-code", rule_verdict="unknown", rule="other-statement")
            hits.append(hit)
        if truncated:
            break

    return {"files_scanned": len(files), "table_files": table_files, "hits": hits,
            "unreadable": unreadable, "truncated": truncated}


# --------------------------------------------------------------------------
# 2단계 — LLM 판정 (문장 단위)
# --------------------------------------------------------------------------


def _numbered(h: dict[str, Any]) -> str:
    """문장에 줄 번호를 붙이고 판정할 줄에 '>>' 를 단다.

    같은 문장 안에서도 줄마다 답이 다르다 — INSERT INTO ORD_HDR (.., TOTAL_AMT)
    는 ORD_HDR 의 컬럼이고 바로 아래 SELECT .. TOTAL_AMT FROM IF_ORDER_TMP 는
    IF_ORDER_TMP 의 컬럼이다. 문장째 한 번에 물으면 두 줄이 같은 답을 받는다(실측).
    """
    lines = (h["sql"] or h["code"]).splitlines() or [h["code"]]
    out = []
    for i, text in enumerate(lines, start=h["start_line"]):
        mark = ">>" if i == h["line"] else "  "
        out.append(f"{mark}{i:5d}| {text}")
    return "\n".join(out)[:3500]


def _prompt(table: str, column: str, h: dict[str, Any]) -> str:
    where = {
        "same-stmt": f"이 문장에는 테이블 {table} 가 함께 나온다.",
        "other-code": f"이 문장에는 테이블 {table} 가 나오지 않는다.",
        "string": "판정할 줄의 컬럼 이름은 문자열 리터럴 안에 있다.",
    }.get(h["place"], "")
    # 규칙은 실측 오판에서 나왔다(qwen3.5:4b). 'INTO :amt' 를 보고 변수라며 no,
    # Java 문자열 속 SQL 을 '문자열일 뿐'이라며 no 로 답했다. yes/no 를 바로 물으면
    # 근거에는 'ORD_HDR 의 컬럼'이라 쓰고 답은 yes 로 냈다 — 그래서 '어느 테이블의
    # 컬럼인가'를 묻고 비교는 코드가 한다(Verdict 스키마 순서도 근거가 먼저다).
    return (
        f"'>>' 가 붙은 줄에 나온 {column} 이 무엇인지 판단해라.\n"
        "kind:\n"
        "- column: SQL 안의 테이블 컬럼. owner_table 에 그 컬럼이 속한 테이블 이름을 적는다.\n"
        "  'SELECT 컬럼 INTO :변수' 에서 :변수 는 값을 받는 자리일 뿐이고 컬럼은 그대로 컬럼이다.\n"
        "  Java·JS 문자열 안의 SQL 도 SQL 이다.\n"
        "  'INSERT INTO X (.., 컬럼)' 의 괄호 안 목록은 X 의 컬럼이다.\n"
        "  'UPDATE X SET 컬럼 = ..' 은 X 의 컬럼이다.\n"
        "  별칭이 붙어 있으면(B.컬럼) FROM/JOIN 에서 그 별칭이 어느 테이블인지 찾아 적는다.\n"
        "- variable: SQL 이 아닌 프로그램 변수(C 변수 선언·대입 등).\n"
        "- text: 로그·메시지 문구 같은 글자.\n"
        "- unknown: 근거가 부족하다. 추측하지 마라.\n"
        "usage(column 일 때): select(조회 목록), where(조건), insert(입력 값), update(SET), "
        "join(조인 조건), order_group(ORDER/GROUP BY), other. 아니면 none.\n\n"
        f"파일: {h['program']} ({h['lang']}). {where}\n\n{_numbered(h)}"
    )


def verdict_of(kind: str, owner: str, table: str) -> str:
    """LLM 의 답(무엇인가, 어느 테이블인가)을 판정으로 바꾼다. 비교는 코드가 한다."""
    if kind in ("variable", "text"):
        return "no"
    if kind != "column":
        return "unknown"
    name = bare_table_name(owner or "")[0].strip().upper()
    if not name:
        return "unknown"        # 컬럼이라면서 테이블을 못 댔다
    return "yes" if name == table.upper() else "no"


def judge(table: str, column: str, hits: list[dict[str, Any]]) -> list[str]:
    """규칙으로 끝나지 않은 줄을 문장 단위로 묶어 LLM 이 판정한다. 경고 목록을 돌려준다.

    LLM 판정이 있으면 그것이 최종이다(규칙 추정은 그리드에 함께 남긴다).
    같은 이름의 컬럼이 여러 테이블에 흔해 규칙만으로는 가를 수 없기 때문이다.
    """
    for h in hits:
        h.update(verdict=h["rule_verdict"], usage="none", decided_by="rule",
                 reason={"comment-only": "주석 안에만 나온다",
                         "same-statement": "테이블과 같은 SQL 문장에 나온다(LLM 미확인)",
                         "partial-statement": "문장 끝을 찾지 못한 조각이다",
                         "in-string": "문자열 안에 있다(LLM 미확인)",
                         "other-statement": "테이블이 없는 문장이다(LLM 미확인)"}[h["rule"]])
    if not USE_LLM:
        return []

    # 테이블과 같은 문장이 먼저다 — 상한에 걸려도 영향이 큰 쪽은 판정된다.
    order = sorted((h for h in hits if h["place"] != "comment"),
                   key=lambda h: ({"same-stmt": 0, "string": 1}.get(h["place"], 2),
                                  h["file"], h["line"]))
    targets = order[:COLUMN_IMPACT_MAX_LLM]
    warnings: list[str] = []
    if len(order) > len(targets):
        warnings.append(f"LLM 판정 상한({COLUMN_IMPACT_MAX_LLM}줄)을 넘은 "
                        f"{len(order) - len(targets)}줄은 규칙 추정만 있다 — 확인 필요 "
                        "(COLUMN_IMPACT_MAX_LLM)")
    if not targets:
        return warnings

    from pydantic import BaseModel, Field

    from core.llm import invoke_structured

    class Verdict(BaseModel):
        # 순서가 중요하다 — 소형 모델은 앞 필드부터 채운다. 근거를 먼저 쓰게 한다.
        reason: str = Field(description="한 문장 근거")
        kind: Literal["column", "variable", "text", "unknown"] = Field(
            description="'>>' 줄의 이름이 무엇인가")
        owner_table: str = Field(description="column 이면 그 컬럼이 속한 테이블 이름, 아니면 빈 값")
        usage: Literal["select", "where", "insert", "update", "join", "order_group",
                       "other", "none"] = Field(description="쓰인 자리")

    failures = 0
    for idx, h in enumerate(targets, start=1):
        notify(f"컬럼 판정 중 {idx}/{len(targets)} ({h['program']}:{h['line']})")
        try:
            v, note = invoke_structured(Verdict, _prompt(table, column, h))
        except Exception as e:
            failures += 1
            h.update(verdict="unknown", decided_by="llm-failed",
                     reason=f"LLM 판정 실패: {type(e).__name__}")
            if failures >= 2 and failures == idx:
                # 처음부터 연달아 실패하면 연결 문제다. 남은 수십 건을 타임아웃까지 기다리지 않는다.
                warnings.append(f"LLM 판정 실패 — 확인 필요: {type(e).__name__}: {e}")
                for rest in targets[idx:]:
                    rest.update(verdict="unknown", decided_by="llm-failed",
                                reason="LLM 연결 실패로 판정하지 못했다")
                return warnings
            continue
        if note and note not in warnings:
            warnings.append(note)
        verdict = verdict_of(v.kind, v.owner_table, table)
        h.update(verdict=verdict, usage=v.usage if verdict == "yes" else "none",
                 decided_by="llm", reason=v.reason.strip()[:200],
                 llm_kind=v.kind, llm_owner=v.owner_table.strip(),
                 conflict=h["rule_verdict"] != "unknown" and h["rule_verdict"] != verdict)
    if failures:
        warnings.append(f"LLM 판정 실패 {failures}문장 — 확인 필요")
    return warnings


# --------------------------------------------------------------------------
# 진입점
# --------------------------------------------------------------------------


def _resolve_root(label: str) -> tuple[str, str, str]:
    """(루트 이름, 경로, 경고). 비우거나 '-' 면 첫 번째 루트."""
    label = (label or "").strip()
    if label in ("", "-"):
        if not SOURCE_ROOTS:
            return "", "", "설정된 소스 루트가 없다 — 확인 필요 (SOURCE_ROOTS)"
        name, path = next(iter(SOURCE_ROOTS.items()))
        return name, path, ""
    if label not in SOURCE_ROOTS:
        return label, "", (f"'{label}' 은 설정에 없는 소스 루트다 — 확인 필요 "
                           f"(설정된 루트: {', '.join(SOURCE_ROOTS)})")
    return label, SOURCE_ROOTS[label], ""


# 툴 답(요약)과 그리드(리소스)가 같은 결과를 쓰게 한다. 챗봇 답이 나온 뒤
# [표로 보기] 를 누르면 리소스가 같은 조사를 다시 부르는데, LLM 판정을 다시
# 돌리면 수십 초를 또 기다린다.
_RESULTS = TTLCache(ttl=1800, maxsize=16)


def _analyze(table: str, column: str, root: str = "") -> dict[str, Any]:
    """판정까지 끝낸 전체 결과. LLM 이 실패한 결과는 캐시하지 않는다 —
    Ollama 가 잠깐 죽었던 결과가 30분 동안 '확인 필요'로 남으면 안 된다."""
    key = split_names(table, column)[:2] + ((root or "").strip() or "-",)
    hit = _RESULTS.get(key)
    if hit is not None:
        return hit
    r = _analyze_uncached(table, column, root)
    if not any(h["decided_by"] == "llm-failed" for h in r["hits"]):
        _RESULTS.set(key, r)
    return r


def _analyze_uncached(table: str, column: str, root: str = "") -> dict[str, Any]:
    tab, col, qualifier = split_names(table, column)
    root_label, root_path, root_warn = _resolve_root(root)
    out: dict[str, Any] = {
        "table": tab, "column": col, "qualifier": qualifier, "root": root_label,
        "files_scanned": 0, "table_files": 0, "hits": [], "warnings": [],
        "rule": "", "decided_by": "fallback",
    }
    if not tab or not col:
        return {**out, "rule": "empty-name", "warnings": ["테이블과 컬럼 이름이 모두 필요하다"]}
    if root_warn:
        return {**out, "rule": "unknown-root", "warnings": [root_warn]}

    notify(f"{root_label} 소스에서 {tab} 을(를) 쓰는 파일 찾는 중")
    found = find_hits(tab, col, root_path)
    hits = found["hits"]
    warnings: list[str] = []
    if found["unreadable"]:
        warnings.append(f"읽지 못한 파일 {len(found['unreadable'])}건 — 확인 필요")
    if found["truncated"]:
        warnings.append(f"찾은 줄이 {COLUMN_IMPACT_MAX_HITS}줄을 넘어 앞부분만 담았다 — "
                        "확인 필요 (COLUMN_IMPACT_MAX_HITS)")
    if hits:
        notify(f"{tab}.{col} — {len(hits)}줄 찾음, 판정 시작")
        warnings += judge(tab, col, hits)

    rank = {"yes": 0, "unknown": 1, "no": 2}
    hits.sort(key=lambda h: (rank[h["verdict"]], h["file"], h["line"]))
    if not found["files_scanned"]:
        rule = "no-files"
        warnings.append(f"'{root_path}' 아래에 스캔할 소스가 없다 — 확인 필요")
    elif not found["table_files"]:
        rule = "no-table-hit"
    elif not hits:
        rule = "no-column-hit"
    else:
        rule = "hits-found"
    return {**out, "files_scanned": found["files_scanned"], "table_files": found["table_files"],
            "hits": hits, "warnings": warnings, "rule": rule,
            "decided_by": "llm" if any(h["decided_by"] == "llm" for h in hits) else "rule"}


def counts_of(hits: list[dict[str, Any]]) -> dict[str, int]:
    return {k: sum(1 for h in hits if h["verdict"] == k) for k in ("yes", "unknown", "no")}


def run_column_impact(table: str, column: str, root: str = "",
                      detail: Detail = "full") -> dict[str, Any]:
    """진입점.

        full    : 찾은 줄 전부와 문장 전문 (리소스·그리드용)
        summary : 판정별 건수와 영향 있는 곳 몇 줄 (챗봇 툴 답용)
    """
    r = _analyze(table, column, root)
    hits = r["hits"]
    base = {k: r[k] for k in ("table", "column", "qualifier", "root", "files_scanned",
                              "table_files", "rule", "decided_by", "warnings")}
    base["counts"] = counts_of(hits)
    base["programs"] = sorted({h["program"] for h in hits})
    if detail == "summary":
        return {
            **base,
            "hit_count": len(hits),
            "places": [
                f"{h['program']}:{h['line']} {USAGE_LABEL[h['usage']]} — {h['code'][:80]}"
                for h in hits if h["verdict"] == "yes"
            ][:10],
            "unknown_places": [f"{h['program']}:{h['line']}" for h in hits
                               if h["verdict"] == "unknown"][:5],
        }
    return {**base, "hits": hits}


GRID_COLUMNS = [
    ("verdict", "판정"), ("program", "프로그램"), ("line", "줄"), ("usage", "용도"),
    ("place", "위치"), ("stmt", "문장"), ("code", "코드"), ("reason", "근거"),
    ("decided_by", "판정 주체"), ("rule_verdict", "규칙 추정"), ("file", "경로"),
]


def column_grid(table: str, column: str, root: str = "") -> dict[str, Any]:
    """화면 그리드용. 표 머리와 행을 함께 준다 — 화면은 이 툴을 몰라도 그린다."""
    r = run_column_impact(table, column, root, detail="full")
    rows = [
        {
            "verdict": VERDICT_LABEL[h["verdict"]],
            "program": h["program"],
            "line": h["line"],
            "usage": USAGE_LABEL[h["usage"]],
            "place": PLACE_LABEL[h["place"]],
            "stmt": (f"{h['stmt_kind'] or '-'} {h['start_line']}~{h['end_line']}"
                     if h["place"] != "comment" else "-"),
            "code": h["code"],
            "reason": h["reason"],
            "decided_by": {"llm": "LLM", "rule": "규칙", "llm-failed": "LLM 실패"}[h["decided_by"]],
            "rule_verdict": VERDICT_LABEL[h["rule_verdict"]]
            + (" ≠ LLM" if h.get("conflict") else ""),
            "file": h["file"],
        }
        for h in r["hits"]
    ]
    c = r["counts"]
    return {
        "title": f"{r['table']}.{r['column']} 컬럼 영향도",
        "subtitle": (f"{r['root']} · 테이블을 쓰는 파일 {r['table_files']}개 · 찾은 줄 {len(rows)}개 "
                     f"(영향 있음 {c['yes']} / 확인 필요 {c['unknown']} / 영향 없음 {c['no']})"),
        "grid": {"columns": [{"key": k, "label": v} for k, v in GRID_COLUMNS], "rows": rows,
                 # 화면은 이 칸의 값으로 걸러 보기 버튼을 만들고 색을 입힌다.
                 "filter_key": "verdict",
                 "tones": {VERDICT_LABEL["yes"]: "bad", VERDICT_LABEL["unknown"]: "warn",
                           VERDICT_LABEL["no"]: "muted"}},
        "warnings": r["warnings"],
    }


if __name__ == "__main__":
    import argparse

    ap = argparse.ArgumentParser(description="컬럼 영향도 조사")
    ap.add_argument("table")
    ap.add_argument("column")
    ap.add_argument("--root", default="", help=f"소스 루트 ({', '.join(SOURCE_ROOTS)})")
    args = ap.parse_args()

    g = column_grid(args.table, args.column, args.root)
    print(g["title"], "—", g["subtitle"])
    for row in g["grid"]["rows"]:
        print(f"  [{row['verdict']}] {row['program']}:{row['line']}  {row['usage']}  "
              f"{row['place']}  {row['code'][:60]}")
        print(f"      └ {row['decided_by']}: {row['reason']}")
    for w in g["warnings"]:
        print(f"  [확인 필요] {w}")
