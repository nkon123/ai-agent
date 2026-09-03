"""iferr — 인터페이스 오류 메일 → 키 추출 → DB 조회 → 영향 확인.

타 시스템에서 데이터를 받다 오류가 나면 인터페이스 키가 적힌 메일이 온다.
그 키로 테이블을 확인해 '어떤 데이터가 어떻게 영향을 받는지'를 정리한다.

    메일 수집 → 키 추출 → DB 조회 → 영향 판정 → (선택) LLM 요약

단독 실행:
    python agents/iferr/agent.py                       # 최근 24시간
    python agents/iferr/agent.py --hours 72
    python agents/iferr/agent.py --key IF_ORD_SEND     # 키를 직접 지정
    MAIL_BACKEND=eml python agents/iferr/agent.py      # 파일로 테스트

설계 원칙 (여기서 특히 중요하다):
    - 메일은 읽기만 한다. 회신·삭제·이동을 하지 않는다.
    - DB 는 SELECT 만. 바인드 변수만 쓴다. DML/DDL 은 만들지도 실행하지도
      않는다 — 조치가 필요하면 사람이 판단한다.
    - '오류가 없다'와 '확인하지 못했다'를 절대 같은 값으로 쓰지 않는다.
      메일을 못 읽었는데 '오류 없음'으로 보이면 장애를 놓친다.
    - 발신자 주소는 마스킹해서 남긴다.
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
    IFERR_KEY_PATTERNS,
    IFERR_MAX_ROWS,
    IFERR_SQL,
    IFERR_STATUS_COLUMNS,
    MAIL_SUBJECT_KEYWORDS,
    MAIL_SUBJECT_MATCH,
    MAIL_SUBJECT_STRIP_PREFIXES,
    USE_LLM,
)
from core import oracle  # noqa: E402
from core.cache import cached  # noqa: E402
from core.outlook import Mail, MailUnavailable, read_mails  # noqa: E402

Detail = Literal["full", "summary", "minimal"]

# 정규식은 매번 컴파일하지 않는다. 메일 수백 통 × 패턴 수만큼 돈다.
_COMPILED: list[tuple[str, re.Pattern[str]]] = [
    (name, re.compile(pat)) for name, pat in IFERR_KEY_PATTERNS
]


class IfErrState(TypedDict, total=False):
    hours: int
    only_key: str
    mails: list[Mail]
    mail_count: int
    cases: list[dict[str, Any]]
    decided_by: str
    rule: str
    warnings: list[str]


# --------------------------------------------------------------------------
# 키 추출 — 순수 함수라 테스트하기 쉽다
# --------------------------------------------------------------------------


def extract_keys(text: str) -> list[dict[str, str]]:
    """텍스트에서 인터페이스 키를 뽑는다. [{key, rule, evidence}]

    같은 키가 여러 번 나와도 한 번만 돌려준다(순서 유지).
    evidence 에는 매칭된 실제 조각을 남긴다 — 규칙명만으로는 왜 그 키가
    뽑혔는지 나중에 알 수 없다.
    """
    found: list[dict[str, str]] = []
    seen: set[str] = set()
    for rule, pattern in _COMPILED:
        for m in pattern.finditer(text or ""):
            key = (m.group(1) if m.groups() else m.group(0)).strip()
            if not key or key.upper() in seen:
                continue
            seen.add(key.upper())
            # 근거는 매칭 주변을 그대로. 앞뒤를 조금 붙여 문맥을 남긴다.
            start, end = max(0, m.start() - 20), min(len(text), m.end() + 20)
            found.append(
                {
                    "key": key,
                    "rule": rule,
                    "evidence": " ".join(text[start:end].split()),
                }
            )
    return found


def normalize_subject(subject: str) -> str:
    """비교용으로 제목을 다듬는다. 회신·전달 머리말을 반복 제거한다.

    "RE: FW: (EAA) Alert Mail ..." 처럼 여러 번 붙는 경우가 있어
    한 번만 떼면 부족하다.

    기본 설정(MAIL_SUBJECT_STRIP_PREFIXES 가 비어 있음)에서는 아무것도
    떼지 않는다 — startswith 는 '문자 그대로 그 문구로 시작'을 뜻해야 한다.
    """
    s = (subject or "").strip()
    changed = True
    while changed:
        changed = False
        for p in MAIL_SUBJECT_STRIP_PREFIXES:
            p = p.strip()
            if p and s.upper().startswith(p.upper()):
                s = s[len(p) :].lstrip()
                changed = True
    return s


def match_info(mail: Mail) -> tuple[str, bool]:
    """(걸린 키워드, 머리말을 떼고서야 걸렸는가).

    두 번째 값을 남기는 이유: 머리말 제거를 켜 두면 "RE: FW: ..." 사본도
    걸리는데, 결과만 보면 원본 알림과 구별되지 않는다. 어느 쪽인지
    화면에 보여야 '왜 두 건으로 잡혔지'를 바로 안다.
    """
    raw = (mail.subject or "").strip()
    hit = _match_against(raw)
    if hit:
        return hit, False
    if MAIL_SUBJECT_STRIP_PREFIXES:
        stripped = normalize_subject(raw)
        if stripped != raw:
            hit = _match_against(stripped)
            if hit:
                return hit, True
    return "", False


def _match_against(subject: str) -> str:
    """제목 문자열 하나를 키워드들과 비교한다. 대소문자는 무시한다."""
    upper = subject.upper()
    mode = (MAIL_SUBJECT_MATCH or "contains").strip().lower()
    for k in MAIL_SUBJECT_KEYWORDS:
        k = k.strip()
        if not k:
            continue
        if mode == "startswith":
            if upper.startswith(k.upper()):
                return k
        elif mode == "regex":
            try:
                if re.search(k, subject, re.IGNORECASE):
                    return k
            except re.error:
                # 잘못된 정규식을 조용히 넘기면 '오류 메일 없음'으로 보인다.
                # 여기서는 건너뛰되 check_subject_rule() 이 따로 알려 준다.
                continue
        elif upper.find(k.upper()) >= 0:
            return k
    return ""


def matched_keyword(mail: Mail) -> str:
    """제목에 걸린 키워드를 돌려준다. 안 걸리면 빈 문자열.

    걸린 키워드를 남기는 이유: '왜 이 메일이 오류로 잡혔지'를 결과만
    보고는 알 수 없다. 판정에는 반드시 근거가 따라와야 한다(규칙 4-6).
    설정을 잘못 써서 엉뚱한 메일이 걸리는 경우가 실제로 있었다.

    비교 방식은 config.MAIL_SUBJECT_MATCH 로 정한다.
    대소문자는 어느 모드에서나 무시한다.
    """
    return match_info(mail)[0]


def check_subject_rule() -> list[str]:
    """제목 판정 설정이 쓸 만한지 점검한다. 문제 목록을 돌려준다.

    설정이 조용히 잘못되어 있으면 '오류 메일이 하나도 없다'로 보인다.
    그건 장애를 놓치는 가장 흔한 경로다.
    """
    problems: list[str] = []
    mode = (MAIL_SUBJECT_MATCH or "").strip().lower()
    if mode not in ("contains", "startswith", "regex"):
        problems.append(
            f"MAIL_SUBJECT_MATCH 값이 이상하다: {MAIL_SUBJECT_MATCH!r} "
            "(contains | startswith | regex)"
        )
    if not [k for k in MAIL_SUBJECT_KEYWORDS if k.strip()]:
        problems.append("MAIL_SUBJECT_KEYWORDS 가 비어 있어 어떤 메일도 대상이 아니다")
    for k in MAIL_SUBJECT_KEYWORDS:
        if len(k.strip()) == 1:
            problems.append(
                f"키워드 '{k}' 가 한 글자다 — 쉼표를 빠뜨렸을 수 있다 "
                '(("오류",) 처럼 쉼표를 붙일 것)'
            )
        if mode == "regex":
            try:
                re.compile(k)
            except re.error as e:
                problems.append(f"정규식이 잘못됐다: {k!r} — {e}")
    return problems


def is_error_mail(mail: Mail) -> bool:
    """제목 키워드로 오류 메일을 고른다. 판정 기준을 한곳에 둔다."""
    return bool(matched_keyword(mail))


# --------------------------------------------------------------------------
# 노드 1 — 메일 수집
# --------------------------------------------------------------------------


def collect(state: IfErrState) -> IfErrState:
    """메일을 읽는다. 못 읽으면 그 사실을 결과에 남긴다.

    빈 목록을 돌려주고 끝내면 '오류 메일 없음'으로 읽힌다. 메일함에
    접근하지 못한 것과 오류가 없는 것은 완전히 다른 상황이다.
    """
    if state.get("only_key"):
        # 키를 직접 준 경우 메일을 읽지 않는다. 이미 아는 키를 확인만 할 때
        # 사서함을 훑을 이유가 없다.
        return {"mails": [], "mail_count": 0, "warnings": []}

    try:
        mails = read_mails(since_hours=state.get("hours"))
    except MailUnavailable as e:
        return {
            "mails": [],
            "mail_count": 0,
            "decided_by": "fallback",
            "rule": "mail-unavailable",
            "warnings": [f"메일을 읽지 못했다 — 확인 필요: {e}"],
        }
    return {"mails": mails, "mail_count": len(mails), "warnings": []}


# --------------------------------------------------------------------------
# 노드 2 — 키 추출
# --------------------------------------------------------------------------


def find_keys(state: IfErrState) -> IfErrState:
    """오류 메일에서 인터페이스 키를 모은다. 키 하나당 case 하나."""
    if state.get("rule") == "mail-unavailable":
        return {}

    if state.get("only_key"):
        return {
            "cases": [
                {
                    "key": state["only_key"],
                    "found_by": "argument",
                    "evidence": "사용자가 직접 지정",
                    "mails": [],
                }
            ]
        }

    cases: dict[str, dict[str, Any]] = {}
    unmatched: list[str] = []

    for mail in state.get("mails") or []:
        if not is_error_mail(mail):
            continue
        # 제목과 본문 양쪽에서 찾는다. 본문 표에 키가 있는 형식이 흔하다.
        hits = extract_keys(f"{mail.subject}\n{mail.body}")
        if not hits:
            # 오류 메일인데 키를 못 뽑았다 — 조용히 버리면 장애를 놓친다.
            unmatched.append(mail.subject[:80])
            continue
        for h in hits:
            case = cases.setdefault(
                h["key"].upper(),
                {
                    "key": h["key"],
                    "found_by": h["rule"],
                    "evidence": h["evidence"],
                    "mails": [],
                },
            )
            case["mails"].append(
                {
                    "subject": mail.subject,
                    "received": mail.received.isoformat() if mail.received else "",
                    # 발신자 주소를 그대로 남기지 않는다.
                    "sender": mail.sender_masked,
                }
            )

    warnings = list(state.get("warnings") or [])
    if unmatched:
        warnings.append(
            f"키를 뽑지 못한 오류 메일 {len(unmatched)}건 — 확인 필요 "
            f"(예: {unmatched[0]})"
        )
    return {"cases": list(cases.values()), "warnings": warnings}


# --------------------------------------------------------------------------
# 노드 3 — DB 조회
# --------------------------------------------------------------------------


@cached(ttl=60, maxsize=128, key=lambda name, sql, key: (name, key))
def _query_cached(name: str, sql: str, key: str) -> list[dict[str, Any]]:
    """같은 키를 여러 메일이 물고 오는 경우가 흔해 짧게 캐시한다.

    캐시 키에서 sql 본문을 빼는 이유: 비싼 것은 DB 왕복이고, sql 은
    설정에서 오는 고정 문자열이라 (name, key) 로 충분하다. TTL 을 60초로
    짧게 둔 것은 장애 대응 중에 상태가 바뀌기 때문이다 — 오래된 값을
    보여 주면 '이미 재처리됐는데 실패로 보이는' 사고가 난다.
    """
    rows = oracle.query(sql, {"if_key": key})
    return rows[:IFERR_MAX_ROWS]


def lookup(state: IfErrState) -> IfErrState:
    """키별로 설정된 SQL 을 돌린다.

    SQL 이 설정되지 않았거나 DB 설정이 없으면 '확인 불가'로 남긴다.
    '영향 없음'으로 처리하지 않는다.
    """
    cases = state.get("cases") or []
    if not cases:
        return {}

    configured = {k: v for k, v in IFERR_SQL.items() if v.strip()}
    warnings = list(state.get("warnings") or [])

    if not configured:
        for c in cases:
            c["db"] = {"status": "unknown", "rule": "sql-not-configured", "rows": {}}
        warnings.append(
            "조회 SQL 이 설정되지 않았다 — 확인 필요 "
            "(config.IFERR_SQL 에 :if_key 를 쓰는 SELECT 를 넣을 것)"
        )
        return {"cases": cases, "warnings": warnings}

    if not oracle.is_configured():
        for c in cases:
            c["db"] = {"status": "unknown", "rule": "db-not-configured", "rows": {}}
        warnings.append("Oracle 설정이 비어 있다 — 확인 필요 (config.ORACLE_DSN)")
        return {"cases": cases, "warnings": warnings}

    for c in cases:
        rows: dict[str, list[dict[str, Any]]] = {}
        errors: list[str] = []
        for name, sql in configured.items():
            try:
                rows[name] = _query_cached(name, sql, c["key"])
            except Exception as e:
                # DB 오류를 조용히 넘기면 '데이터 없음'과 구분되지 않는다.
                errors.append(f"{name}: {type(e).__name__}: {e}")
        if errors:
            c["db"] = {"status": "unknown", "rule": "query-failed",
                       "rows": rows, "errors": errors}
            warnings.append(f"[{c['key']}] 조회 실패 — 확인 필요: {errors[0]}")
        elif any(rows.values()):
            c["db"] = {"status": "found", "rule": "rows-found", "rows": rows}
        else:
            # 진짜로 행이 없는 경우. 이건 '없다'가 맞다.
            c["db"] = {"status": "missing", "rule": "no-rows", "rows": rows}
    return {"cases": cases, "warnings": warnings}


# --------------------------------------------------------------------------
# 노드 4 — 영향 판정
# --------------------------------------------------------------------------


def assess(state: IfErrState) -> IfErrState:
    """조회 결과로 영향을 정리한다. 규칙 기반이며 근거를 함께 남긴다.

    스키마를 모르는 상태에서도 동작해야 하므로, 상태 컬럼 후보
    (config.IFERR_STATUS_COLUMNS)가 있으면 값별로 집계하고 없으면
    행 수만 센다. 스키마가 확정되면 여기를 구체화한다.
    """
    for c in state.get("cases") or []:
        db = c.get("db") or {}
        rows: dict[str, list[dict[str, Any]]] = db.get("rows") or {}

        if db.get("status") == "unknown":
            c["impact"] = "확인 불가"
            c["decided_by"] = "fallback"
            c["rule"] = db.get("rule", "unknown")
            continue

        if db.get("status") == "missing":
            c["impact"] = "해당 키의 데이터가 테이블에 없다"
            c["decided_by"] = "rule"
            c["rule"] = "no-rows"
            continue

        counts = {name: len(r) for name, r in rows.items() if r}
        status_summary = _summarize_status(rows)
        parts = [f"{name} {n}건" for name, n in counts.items()]
        if status_summary:
            parts.append("상태 " + ", ".join(f"{k}={v}" for k, v in status_summary.items()))
        c["impact"] = " / ".join(parts)
        c["decided_by"] = "rule"
        c["rule"] = "rows-found"
        c["counts"] = counts
        c["status_summary"] = status_summary
    return {"cases": state.get("cases") or []}


def _summarize_status(rows: dict[str, list[dict[str, Any]]]) -> dict[str, int]:
    """상태 컬럼 후보를 찾아 값별로 센다. 없으면 빈 dict."""
    wanted = {c.strip().upper() for c in IFERR_STATUS_COLUMNS if c.strip()}
    summary: dict[str, int] = {}
    for row_list in rows.values():
        for row in row_list:
            for col, val in row.items():
                if col.upper() in wanted and val is not None:
                    summary[str(val)] = summary.get(str(val), 0) + 1
    return summary


# --------------------------------------------------------------------------
# 노드 5 — LLM 요약 (선택)
# --------------------------------------------------------------------------


def explain(state: IfErrState) -> IfErrState:
    """USE_LLM 이 False 면 아무것도 하지 않는다.

    판정은 규칙이 끝냈다. LLM 은 사람이 읽기 좋게 묶어 주기만 한다.
    판정을 LLM 에 맡기면 같은 상황에 다른 답이 나오고 근거도 남지 않는다.
    """
    cases = state.get("cases") or []
    if not USE_LLM or not cases:
        return {}

    warnings = list(state.get("warnings") or [])
    try:
        from core.llm import get_llm

        lines = "\n".join(
            f"- {c['key']}: {c.get('impact','')} (메일 {len(c.get('mails') or [])}건)"
            for c in cases[:10]
        )
        prompt = (
            "아래는 인터페이스 오류 확인 결과다. 무엇이 문제이고 무엇을 먼저 "
            "봐야 하는지 두 문장 이내로 요약해라. 목록에 없는 내용은 말하지 마라. "
            "'확인 불가'는 '문제 없음'이 아니다.\n" + lines
        )
        return {"comment": str(get_llm().invoke(prompt).content).strip(),
                "warnings": warnings}
    except Exception as e:
        warnings.append(f"LLM 호출 실패 — 확인 필요: {type(e).__name__}: {e}")
        return {"comment": "", "warnings": warnings}


@cached(ttl=3600, maxsize=1, key=lambda: "graph")
def _graph():
    g = StateGraph(IfErrState)
    for name, fn in (
        ("collect", collect), ("find_keys", find_keys),
        ("lookup", lookup), ("assess", assess), ("explain", explain),
    ):
        g.add_node(name, fn)
    g.add_edge(START, "collect")
    g.add_edge("collect", "find_keys")
    g.add_edge("find_keys", "lookup")
    g.add_edge("lookup", "assess")
    g.add_edge("assess", "explain")
    g.add_edge("explain", END)
    return g.compile()


def run_iferr(
    hours: int | None = None, key: str = "", detail: Detail = "full"
) -> dict[str, Any]:
    """진입점.

        full    : 조회된 행까지 전부 (화면용, MCP 리소스로 나간다)
        summary : 키별 한 줄 (LLM 컨텍스트에 들어간다)
        minimal : 키 목록과 상태만

    full 을 챗봇 툴에서 돌려주면 조회 행이 매 턴 컨텍스트를 먹는다.
    """
    state: IfErrState = _graph().invoke(
        {"hours": hours, "only_key": key.strip()}
    )
    cases = state.get("cases") or []
    warnings = state.get("warnings") or []

    if detail == "minimal":
        return {
            "mail_count": state.get("mail_count", 0),
            "cases": [
                {"key": c["key"], "status": (c.get("db") or {}).get("status", "unknown")}
                for c in cases
            ],
            "warnings": warnings,
        }

    if detail == "summary":
        return {
            "mail_count": state.get("mail_count", 0),
            "case_count": len(cases),
            "cases": [
                {
                    "key": c["key"],
                    "status": (c.get("db") or {}).get("status", "unknown"),
                    "impact": c.get("impact", ""),
                    "rule": c.get("rule", ""),
                    "mail_count": len(c.get("mails") or []),
                }
                for c in cases[:10]
            ],
            "more_cases": max(0, len(cases) - 10),
            "comment": state.get("comment") or "",
            "warnings": warnings,
        }

    return {
        "mail_count": state.get("mail_count", 0),
        "case_count": len(cases),
        "cases": cases,
        "comment": state.get("comment") or "",
        "warnings": warnings,
    }


# --------------------------------------------------------------------------
# 단계별 진입점 — MCP 에서 기능별 툴로 노출한다
#
# run_iferr() 가 전체를 한 번에 도는 통합 경로이고, 아래 둘은 그 중간
# 단계만 따로 부르는 경로다. 같은 함수를 재사용하므로 통합 실행과
# 단계별 실행의 결과가 어긋나지 않는다.
# --------------------------------------------------------------------------


def list_mails(hours: int | None = None, detail: Detail = "full") -> dict[str, Any]:
    """메일만 읽는다. DB 는 건드리지 않는다.

    '메일이 오긴 왔는지' 부터 확인할 때 쓴다. 발신자는 마스킹된다.
    """
    state = collect({"hours": hours})
    if state.get("rule") == "mail-unavailable":
        # 실패 경로도 성공 경로와 '같은 모양'이어야 한다.
        # 키를 빠뜨리면 호출부가 KeyError 로 죽고, 그 에러가 진짜 원인
        # (Outlook 연결 실패·폴더 못 찾음)을 가려 버린다. 실제로 겪었다.
        return {
            "mail_count": 0,
            "error_count": 0,
            "mails": [],
            "warnings": state.get("warnings") or [],
        }

    rows: list[dict[str, Any]] = []
    for m in state.get("mails") or []:
        hit, via_prefix = match_info(m)
        keys = extract_keys(f"{m.subject}\n{m.body}") if hit else []
        rows.append(
            {
                "subject": m.subject,
                "received": m.received.isoformat() if m.received else "",
                "sender": m.sender_masked,
                "is_error": bool(hit),
                # 어떤 키워드에 걸렸는지 남긴다. 오탐 원인을 여기서 찾는다.
                "matched": hit,
                # 머리말(RE:/FW:)을 떼고서야 걸린 사본인가
                "via_prefix": via_prefix,
                "keys": [k["key"] for k in keys],
            }
        )

    if detail != "full":
        # 본문은 애초에 담지 않았지만, 요약에서는 제목도 잘라 컨텍스트를 아낀다.
        rows = [{**r, "subject": r["subject"][:60]} for r in rows[:20]]

    return {
        "mail_count": len(state.get("mails") or []),
        "error_count": sum(1 for r in rows if r["is_error"]),
        "mails": rows,
        "warnings": state.get("warnings") or [],
    }


def lookup_key(key: str, detail: Detail = "full") -> dict[str, Any]:
    """메일을 읽지 않고 키 하나만 DB 에서 확인한다.

    이미 키를 아는 경우(사람이 메일을 보고 왔거나, 다른 툴이 뽑아 준 경우)
    사서함을 훑을 이유가 없다.
    """
    return run_iferr(key=key, detail=detail)


if __name__ == "__main__":
    import argparse
    import json

    ap = argparse.ArgumentParser(description="인터페이스 오류 메일 확인")
    ap.add_argument("--hours", type=int, default=None, help="몇 시간 전까지 볼지")
    ap.add_argument("--key", default="", help="메일을 읽지 않고 이 키만 확인")
    ap.add_argument("--detail", choices=["full", "summary", "minimal"], default="full")
    # 아래 셋은 진단용이다. DB 를 건드리지 않는다.
    ap.add_argument("--folders", action="store_true",
                    help="Outlook 폴더 목록 (MAIL_FOLDER 에 넣을 값 찾기)")
    ap.add_argument("--mails", action="store_true",
                    help="메일 목록만 본다. DB 조회를 하지 않는다")
    ap.add_argument("--dump", type=int, metavar="N", default=0,
                    help="최근 오류 메일 N통의 본문을 out/ 에 저장(정규식 확인용)")
    args = ap.parse_args()

    # ---- 진단 모드 ---------------------------------------------------------
    if args.folders:
        from core.outlook import list_folders

        try:
            for path in list_folders():
                print(path)
        except Exception as e:
            print(f"[실패] {e}")
            raise SystemExit(1)
        raise SystemExit(0)

    if args.mails or args.dump:
        r = list_mails(hours=args.hours)

        # 메일을 못 읽었으면 표를 그리기 전에 원인부터 보여 준다.
        # 빈 표를 먼저 그리면 '오류 메일이 없구나'로 읽힌다.
        if not r["mails"] and r["warnings"]:
            print("[메일을 읽지 못했다]")
            for w in r["warnings"]:
                print(f"  {w}")
            print(
                "\n확인 순서:\n"
                "  1) python agents/iferr/agent.py --folders   # 폴더 이름 확인\n"
                "  2) config_local.py 의 MAIL_FOLDER 를 그 경로로\n"
                "  3) Outlook 이 실행 중인지, pip install pywin32 했는지"
            )
            raise SystemExit(1)

        print(f"메일 {r['mail_count']}통 (오류로 분류 {r['error_count']}통)")
        print(
            f"오류 판정: [{MAIL_SUBJECT_MATCH}] "
            f"{', '.join(MAIL_SUBJECT_KEYWORDS) or '(없음)'}"
        )
        print(
            "  머리말 제거: "
            + (", ".join(MAIL_SUBJECT_STRIP_PREFIXES) or "안 함 (RE:/FW: 사본 제외)")
        )
        for problem in check_subject_rule():
            print(f"  [설정 확인] {problem}")
        print()
        print(f"{'오류':<5}{'수신시각':<18}{'걸린키워드':<16}{'키':<22}제목")
        print("─" * 100)
        for m in r["mails"]:
            mark = "  O  " if m["is_error"] else "  .  "
            keys = ", ".join(m["keys"]) or ("-" if not m["is_error"] else "못찾음")
            # 걸린 키워드를 함께 보여 준다. 엉뚱한 메일이 O 로 찍히면
            # 여기를 보고 바로 원인을 안다(예: 한 글자짜리 '오').
            # 컬럼 폭을 넘는 값은 잘라 정렬을 유지한다.
            # "(EAA) Alert Mail" 처럼 긴 키워드에서 표가 밀린다.
            hit = m["matched"] or "-"
            if m.get("via_prefix"):
                hit += "(전달)"      # 원본이 아니라 사본임을 표시
            hit = hit if len(hit) <= 14 else hit[:13] + "…"
            print(
                f"{mark}{m['received'][:16]:<18}{hit:<16}"
                f"{keys:<22}{m['subject'][:40]}"
            )
        for w in r["warnings"]:
            print(f"\n[확인 필요] {w}")

        if args.dump:
            # 본문에는 개인정보가 있을 수 있다. out/ 은 .gitignore 에 있어
            # 커밋되지 않지만, 외부로 보낼 때는 반드시 내용을 확인할 것.
            from core.outlook import MailUnavailable, read_mails

            out_dir = _ROOT / "out"
            out_dir.mkdir(exist_ok=True)
            saved = 0
            try:
                mails = read_mails(since_hours=args.hours)
            except MailUnavailable as e:
                print(f"[메일을 읽지 못했다] {e}")
                raise SystemExit(1)
            for mail in mails:
                if saved >= args.dump or not is_error_mail(mail):
                    continue
                path = out_dir / f"mail_{saved + 1}.txt"
                path.write_text(
                    f"제목: {mail.subject}\n발신: {mail.sender_masked}\n"
                    f"수신: {mail.received}\n{'─' * 60}\n{mail.body}",
                    encoding="utf-8",
                )
                print(f"저장: {path}")
                saved += 1
            print(
                "\n본문을 보고 config_local.py 의 IFERR_KEY_PATTERNS 를 맞출 것. "
                "(개인정보가 있을 수 있으니 외부 공유 전 확인)"
            )
        raise SystemExit(0)

    # ---- 통합 실행 ---------------------------------------------------------
    result = run_iferr(hours=args.hours, key=args.key, detail=args.detail)
    print(json.dumps(result, ensure_ascii=False, indent=2, default=str))

    # 규칙별 집계. 무엇이 어떤 근거로 판정됐는지 콘솔에도 남긴다.
    counts: dict[str, int] = {}
    for c in result.get("cases", []):
        k = f"{c.get('decided_by', '-')}/{c.get('rule', '-')}"
        counts[k] = counts.get(k, 0) + 1
    print("\n[규칙별 집계]")
    for k, v in sorted(counts.items()):
        print(f"  {k:<28} {v:>4}건")
    if result.get("warnings"):
        print("\n[확인 필요]")
        for w in result["warnings"]:
            print(f"  - {w}")
