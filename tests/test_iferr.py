"""iferr 에이전트 회귀 테스트.

메일은 samples/mail/ 의 .eml 로, DB 는 monkeypatch 로 대신한다.
Outlook 도 Oracle 도 없는 곳에서 전부 돌아야 한다.

실행:
    pytest -q
"""

from __future__ import annotations

import asyncio
import json
import sys
from datetime import datetime
from pathlib import Path
from typing import Any

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from mcp.client.client import Client  # noqa: E402

import config  # noqa: E402
from agents.iferr import agent as iferr  # noqa: E402
from agents.iferr import extract_keys, run_iferr  # noqa: E402
from core import outlook  # noqa: E402
from mcp_server.tools import build_server  # noqa: E402


@pytest.fixture(autouse=True)
def eml_backend(monkeypatch: Any) -> None:
    """모든 테스트는 파일 백엔드로 돈다. Outlook 이 없어도 통과해야 한다."""
    monkeypatch.setattr(outlook, "MAIL_BACKEND", "eml")
    monkeypatch.setattr(outlook, "MAIL_EML_DIR", "./samples/mail")
    # 샘플 메일은 파일에 박힌 날짜를 쓴다. 조회 범위를 24시간으로 두면
    # 하루만 지나도 샘플이 범위 밖으로 밀려 테스트가 깨진다.
    # 기간 해석 자체는 parse_period 로 따로 검증하므로 여기서는 넉넉히 잡는다.
    monkeypatch.setattr(outlook, "MAIL_LOOKBACK_HOURS", 24 * 3650)
    # 툴은 '3일'처럼 기간을 직접 넘긴다. 그때의 기준 시각을 샘플 날짜(2026-09-03)
    # 다음 날로 고정한다 — 실제 시계를 쓰면 날이 지날수록 0통이 되어 깨졌다.
    monkeypatch.setattr(outlook, "_now", lambda: datetime(2026, 9, 4, 9, 0))
    # 이 파일은 키워드·정규식 판정을 고정한다. LLM 판정은 test_iferr_llm.py 가
    # 가짜 LLM 으로 따로 본다 — 여기서 실제 Ollama 를 부르면 결과가 모델에 달린다.
    monkeypatch.setattr(iferr, "IFERR_MAIL_JUDGE", "keyword")
    iferr._query_cached.cache.clear()


# --------------------------------------------------------------------------
# 키 추출
# --------------------------------------------------------------------------


def test_extract_key_from_labeled_form():
    hits = extract_keys("  IF_ID : IF_ORD_SEND\n  오류내용 : ORA-01400")
    assert hits[0]["key"] == "IF_ORD_SEND"
    assert hits[0]["rule"] == "if-id-labeled"
    # 근거는 매칭된 실제 조각이어야 한다. 규칙명만으로는 재현이 안 된다.
    assert "IF_ORD_SEND" in hits[0]["evidence"]


def test_extract_key_from_korean_label():
    hits = extract_keys("인터페이스 ID: IF_STK_RECV\n건수: 128건")
    assert [h["key"] for h in hits] == ["IF_STK_RECV"]


def test_extract_key_deduplicates():
    """같은 키가 제목과 본문에 다 있어도 한 번만 나온다."""
    hits = extract_keys("IF_ID: IF_A_B\n...\nIF_ID = IF_A_B")
    assert [h["key"] for h in hits] == ["IF_A_B"]


def test_extract_returns_empty_when_no_key():
    assert extract_keys("연계 서버가 응답하지 않습니다.") == []


# --------------------------------------------------------------------------
# 메일 선별
# --------------------------------------------------------------------------


def test_only_error_mails_are_picked():
    """정상 처리 보고 메일(04)에도 IF_ID 가 있지만 대상이 아니다.

    제목 키워드로 거르지 않으면 정상 건까지 장애로 올라온다.
    """
    r = run_iferr(detail="full")
    keys = {c["key"] for c in r["cases"]}
    assert keys == {"IF_ORD_SEND", "IF_STK_RECV"}
    # 개수를 박아 두면 샘플 메일을 추가할 때마다 깨진다. 폴더를 기준으로 센다.
    assert r["mail_count"] == len(list(Path("samples/mail").glob("*.eml")))


def test_euckr_mail_body_is_read():
    """사내 메일 본문은 cp949/euc-kr 인 경우가 흔하다."""
    r = run_iferr(detail="full")
    stk = next(c for c in r["cases"] if c["key"] == "IF_STK_RECV")
    assert stk["mails"], "euc-kr 본문에서 키를 못 뽑았다"


def test_error_mail_without_key_is_flagged():
    """오류 메일인데 키를 못 뽑았으면 조용히 버리지 않는다."""
    r = run_iferr(detail="full")
    assert any("키를 뽑지 못한" in w for w in r["warnings"])


def test_sender_address_is_masked():
    """발신자 주소를 그대로 남기지 않는다(개인정보)."""
    r = run_iferr(detail="full")
    senders = [m["sender"] for c in r["cases"] for m in c["mails"]]
    assert senders and all("***" in s for s in senders)
    assert not any("if.monitor@" in s for s in senders)


def test_mail_unavailable_is_not_no_error(monkeypatch: Any):
    """메일을 못 읽은 것과 오류가 없는 것은 다르다.

    빈 결과로 넘기면 장애를 놓친다.
    """
    monkeypatch.setattr(outlook, "MAIL_EML_DIR", "./no/such/dir")
    r = run_iferr(detail="full")
    assert r["cases"] == []
    assert any("메일을 읽지 못했다" in w for w in r["warnings"])


# --------------------------------------------------------------------------
# DB 조회 — SQL 이 없을 때 / 있을 때
# --------------------------------------------------------------------------


def test_unconfigured_sql_is_unknown_not_ok():
    """마스터 테이블을 안 정했으면 '영향 없음'이 아니라 '확인 불가'다."""
    r = run_iferr(detail="full")
    for c in r["cases"]:
        assert c["db"]["status"] == "unknown"
        assert c["rule"] == "sql-not-configured"
        assert c["impact"] == "확인 불가"
    assert any("IFERR_MASTER_TABLE" in w for w in r["warnings"])


def test_unconfigured_db_is_unknown_not_ok(monkeypatch: Any):
    """SQL 은 있는데 접속 설정이 없으면 그것도 '확인 불가'다."""
    monkeypatch.setattr(
        iferr,
        "IFERR_SQL",
        {"header": "SELECT 1 FROM dual WHERE :if_key IS NOT NULL", "detail": "",
         "impact": ""},
    )
    r = run_iferr(detail="full")
    for c in r["cases"]:
        assert c["rule"] == "db-not-configured"
    assert any("Oracle 설정이 비어 있다" in w for w in r["warnings"])


def _fake_db(monkeypatch: Any, rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """조회 SQL 과 DB 를 가짜로 채운다. 호출된 바인드를 기록해 돌려준다."""
    calls: list[dict[str, Any]] = []

    monkeypatch.setitem(
        config.IFERR_SQL, "header",
        "SELECT if_key, status FROM if_hdr WHERE if_key = :if_key",
    )
    monkeypatch.setattr(iferr, "IFERR_SQL", config.IFERR_SQL)
    monkeypatch.setattr(iferr.oracle, "is_configured", lambda: True)

    def fake_query(sql: str, binds: dict[str, Any]) -> list[dict[str, Any]]:
        calls.append({"sql": sql, "binds": binds})
        return rows

    monkeypatch.setattr(iferr.oracle, "query", fake_query)
    return calls


def test_query_uses_bind_variable_only(monkeypatch: Any):
    """키는 반드시 바인드로 들어간다. SQL 문자열에 끼워 넣지 않는다."""
    calls = _fake_db(monkeypatch, [{"IF_KEY": "IF_ORD_SEND", "STATUS": "E"}])
    run_iferr(key="IF_ORD_SEND", detail="full")

    assert calls, "조회가 실행되지 않았다"
    assert calls[0]["binds"] == {"if_key": "IF_ORD_SEND"}
    assert "IF_ORD_SEND" not in calls[0]["sql"]


def test_rows_found_summarizes_status(monkeypatch: Any):
    """상태 컬럼이 있으면 값별로 집계한다(스키마를 몰라도 동작해야 한다)."""
    _fake_db(
        monkeypatch,
        [
            {"IF_KEY": "IF_ORD_SEND", "STATUS": "E"},
            {"IF_KEY": "IF_ORD_SEND", "STATUS": "E"},
            {"IF_KEY": "IF_ORD_SEND", "STATUS": "S"},
        ],
    )
    c = run_iferr(key="IF_ORD_SEND", detail="full")["cases"][0]
    assert c["db"]["status"] == "found" and c["rule"] == "rows-found"
    assert c["status_summary"] == {"E": 2, "S": 1}
    assert "header 3건" in c["impact"] and "E=2" in c["impact"]


def test_no_rows_is_missing_not_unknown(monkeypatch: Any):
    """행이 없는 것은 '없다'가 맞다 — '확인 불가'와 구분한다."""
    _fake_db(monkeypatch, [])
    c = run_iferr(key="IF_ORD_SEND", detail="full")["cases"][0]
    assert c["db"]["status"] == "missing" and c["rule"] == "no-rows"


def test_query_failure_is_unknown(monkeypatch: Any):
    """DB 오류를 '데이터 없음'으로 넘기지 않는다."""
    _fake_db(monkeypatch, [])

    def boom(sql: str, binds: dict[str, Any]):
        raise RuntimeError("ORA-12541")

    monkeypatch.setattr(iferr.oracle, "query", boom)
    r = run_iferr(key="IF_ORD_SEND", detail="full")
    c = r["cases"][0]
    assert c["db"]["status"] == "unknown" and c["rule"] == "query-failed"
    assert any("조회 실패" in w for w in r["warnings"])


def test_row_limit_is_applied(monkeypatch: Any):
    """수만 행을 그대로 들고 오면 챗봇이 멎는다."""
    _fake_db(monkeypatch, [{"IF_KEY": "X", "STATUS": "E"}] * (config.IFERR_MAX_ROWS + 50))
    c = run_iferr(key="IF_ORD_SEND", detail="full")["cases"][0]
    assert len(c["db"]["rows"]["header"]) == config.IFERR_MAX_ROWS


def test_same_key_queried_once(monkeypatch: Any):
    """같은 키를 여러 메일이 물고 와도 DB 왕복은 한 번이면 된다."""
    calls = _fake_db(monkeypatch, [{"IF_KEY": "X", "STATUS": "E"}])
    run_iferr(key="IF_ORD_SEND", detail="full")
    run_iferr(key="IF_ORD_SEND", detail="full")
    assert len(calls) == 1


# --------------------------------------------------------------------------
# detail
# --------------------------------------------------------------------------


def test_summary_omits_rows():
    """summary 는 LLM 컨텍스트에 들어간다. 조회 행이 들어가면 안 된다."""
    s = run_iferr(detail="summary")
    assert all("db" not in c and "mails" not in c for c in s["cases"])
    assert "case_count" in s


# --------------------------------------------------------------------------
# MCP 노출
# --------------------------------------------------------------------------


def _run(coro: Any) -> Any:
    return asyncio.run(coro)


async def _with_client(fn: Any) -> Any:
    async with Client(build_server()) as c:
        return await fn(c)


def test_mcp_tool_reports_unknown_clearly():
    """'확인 불가'가 '문제 없음'으로 읽히면 안 된다."""

    async def call(c: Client) -> Any:
        return await c.call_tool("check_interface_errors", {"period": "3일"})

    text = _run(_with_client(call)).content[0].text
    assert "IF_ORD_SEND" in text
    assert "확인 불가" in text and "확인 필요" in text
    # 어느 기간을 본 것인지 답에 반드시 드러나야 한다.
    assert "최근 3일(72시간)" in text


def test_mcp_tool_is_read_only():
    """메일 회신·삭제나 DML 을 하지 않는다는 표시가 있어야 한다.

    다른 MCP 호스트도 이 힌트로 확인 절차를 넣는다.
    """

    async def listed(c: Client) -> Any:
        return {t.name: t for t in (await c.list_tools()).tools}

    t = _run(_with_client(listed))["check_interface_errors"]
    assert t.annotations.read_only_hint is True
    assert t.annotations.destructive_hint is False


def test_mcp_resource_returns_full_detail(monkeypatch: Any):
    _fake_db(monkeypatch, [{"IF_KEY": "IF_ORD_SEND", "STATUS": "E"}])

    async def read(c: Client) -> Any:
        return await c.read_resource("iferr://detail/IF_ORD_SEND")

    data = json.loads(_run(_with_client(read)).contents[0].text)
    assert data["cases"][0]["key"] == "IF_ORD_SEND"
    assert data["cases"][0]["db"]["rows"]["header"]


# --------------------------------------------------------------------------
# 단계별 툴 (tier="step")
# --------------------------------------------------------------------------


def test_step_tool_list_error_mails_does_not_touch_db(monkeypatch: Any):
    """메일 목록 툴은 DB 를 건드리지 않는다."""

    def boom(*a: Any, **k: Any):
        raise AssertionError("DB 를 조회하면 안 된다")

    monkeypatch.setattr(iferr.oracle, "query", boom)

    async def call(c: Client) -> Any:
        return await c.call_tool("list_error_mails", {"period": "3일"})

    text = _run(_with_client(call)).content[0].text
    assert "IF_ORD_SEND" in text
    assert "오류" in text and "통)" in text
    # 발신자는 마스킹된 채로 나가야 한다.
    assert "***" in text and "if.monitor@" not in text


def test_step_tool_extract_keys_is_pure():
    """텍스트만 받는 툴이라 메일함도 DB 도 필요 없다."""

    async def call(c: Client) -> Any:
        return await c.call_tool(
            "extract_interface_keys", {"text": "IF_ID : IF_ORD_SEND 오류"}
        )

    text = _run(_with_client(call)).content[0].text
    assert "IF_ORD_SEND" in text and "if-id-labeled" in text


def test_step_tool_extract_keys_says_not_found():
    """못 찾은 것을 '키가 없다'로 단정하지 않는다 — 패턴이 다를 수 있다."""

    async def call(c: Client) -> Any:
        return await c.call_tool("extract_interface_keys", {"text": "응답 없음"})

    text = _run(_with_client(call)).content[0].text
    assert "찾지 못했다" in text and "IFERR_KEY_PATTERNS" in text


def test_step_tool_lookup_does_not_read_mail(monkeypatch: Any):
    """키를 아는 경우 사서함을 훑지 않는다."""

    def boom(*a: Any, **k: Any):
        raise AssertionError("메일을 읽으면 안 된다")

    monkeypatch.setattr(iferr, "read_mails", boom)
    _fake_db(monkeypatch, [{"IF_KEY": "IF_ORD_SEND", "STATUS": "E"}])

    async def call(c: Client) -> Any:
        return await c.call_tool("lookup_interface", {"key": "IF_ORD_SEND"})

    text = _run(_with_client(call)).content[0].text
    assert "IF_ORD_SEND" in text and "확인됨" in text


def test_combo_and_step_agree(monkeypatch: Any):
    """통합 툴과 단계별 툴이 같은 함수를 재사용하므로 결과가 어긋나면 안 된다."""
    _fake_db(monkeypatch, [{"IF_KEY": "IF_ORD_SEND", "STATUS": "E"}])
    combo = run_iferr(key="IF_ORD_SEND", detail="summary")["cases"][0]
    step = iferr.lookup_key("IF_ORD_SEND", detail="summary")["cases"][0]
    assert combo == step


def test_list_mails_failure_has_same_shape(monkeypatch: Any):
    """실패 경로도 성공 경로와 같은 키를 돌려줘야 한다.

    error_count 를 빠뜨렸더니 호출부가 KeyError 로 죽었고, 그 에러가
    진짜 원인(Outlook 연결 실패)을 가렸다. 실제로 겪은 사고다.
    """
    monkeypatch.setattr(outlook, "MAIL_EML_DIR", "./no/such/dir")

    ok = iferr.list_mails()          # 정상 경로(폴더가 없으니 실패 경로다)
    monkeypatch.setattr(outlook, "MAIL_EML_DIR", "./samples/mail")
    good = iferr.list_mails()

    assert set(ok) == set(good), "실패 경로와 성공 경로의 키가 다르다"
    assert ok["mail_count"] == 0 and ok["error_count"] == 0
    assert any("메일을 읽지 못했다" in w for w in ok["warnings"])


def test_step_tool_survives_mail_failure(monkeypatch: Any):
    """메일을 못 읽어도 툴이 예외로 죽지 않고 원인을 문자열로 돌려준다."""
    monkeypatch.setattr(outlook, "MAIL_EML_DIR", "./no/such/dir")

    async def call(c: Client) -> Any:
        return await c.call_tool("list_error_mails", {"period": "3일"})

    r = _run(_with_client(call))
    text = r.content[0].text
    assert "확인 필요" in text and "메일을 읽지 못했다" in text


def test_matched_keyword_is_recorded(monkeypatch: Any):
    """'왜 이 메일이 오류로 잡혔는지'가 결과에 남아야 한다."""
    r = iferr.list_mails()
    by_subject = {m["subject"]: m for m in r["mails"]}

    err = by_subject["재고 연계 ERROR 발생 (배치)"]
    assert err["is_error"] and err["matched"] == "ERROR"

    ok = by_subject["주간 연계 처리 결과 보고"]
    assert not ok["is_error"] and ok["matched"] == ""


def test_single_char_keyword_shows_why_it_matched(monkeypatch: Any):
    """설정을 잘못 써서 엉뚱한 메일이 걸려도 원인이 보여야 한다.

    ("오류") 처럼 쉼표를 빠뜨리면 '오' 한 글자로 비교하게 된다.
    config 가 정규화해 주지만, 직접 한 글자를 넣은 경우까지 막지는 않는다.
    그때는 matched 로 원인을 찾는다.
    """
    monkeypatch.setattr(iferr, "MAIL_SUBJECT_KEYWORDS", ("오",))
    r = iferr.list_mails()
    hits = [m for m in r["mails"] if m["is_error"]]
    assert all(m["matched"] == "오" for m in hits)


# --------------------------------------------------------------------------
# 제목 판정 모드
# --------------------------------------------------------------------------


def _subjects(marked_only: bool = True) -> set[str]:
    r = iferr.list_mails()
    return {m["subject"] for m in r["mails"] if m["is_error"] or not marked_only}


def test_startswith_mode_ignores_mentions_in_the_middle(monkeypatch: Any):
    """머리말로 시작하는 메일만 대상이다.

    "문의: (EAA) Alert Mail 설정 관련" 처럼 본문·제목 중간에 그 문구가
    들어간 메일은 알림이 아니다. contains 모드에서는 이게 걸린다.
    """
    monkeypatch.setattr(iferr, "MAIL_SUBJECT_MATCH", "startswith")
    monkeypatch.setattr(iferr, "MAIL_SUBJECT_KEYWORDS", ("(EAA) Alert Mail",))

    hits = _subjects()
    assert "(EAA) Alert Mail - 주문 연계 실패" in hits
    assert "문의: (EAA) Alert Mail 설정 관련" not in hits


def test_forwarded_copy_is_excluded_by_default(monkeypatch: Any):
    """startswith 는 '문자 그대로 그 문구로 시작'을 뜻한다.

    "RE: FW: (EAA) Alert Mail ..." 은 시스템이 보낸 원본이 아니라 사람이
    주고받은 사본이라 대개 중복이다. 기본값은 이를 제외한다.
    """
    monkeypatch.setattr(iferr, "MAIL_SUBJECT_MATCH", "startswith")
    monkeypatch.setattr(iferr, "MAIL_SUBJECT_KEYWORDS", ("(EAA) Alert Mail",))
    monkeypatch.setattr(iferr, "MAIL_SUBJECT_STRIP_PREFIXES", ())

    hits = _subjects()
    assert "(EAA) Alert Mail - 주문 연계 실패" in hits
    assert "RE: FW: (EAA) Alert Mail - 재고 연계 실패" not in hits


def test_forwarded_copy_can_be_included_by_config(monkeypatch: Any):
    """전달분까지 봐야 하면 머리말 목록을 설정한다."""
    monkeypatch.setattr(iferr, "MAIL_SUBJECT_MATCH", "startswith")
    monkeypatch.setattr(iferr, "MAIL_SUBJECT_KEYWORDS", ("(EAA) Alert Mail",))
    monkeypatch.setattr(iferr, "MAIL_SUBJECT_STRIP_PREFIXES", ("RE:", "FW:"))

    rows = {m["subject"]: m for m in iferr.list_mails()["mails"]}
    fwd = rows["RE: FW: (EAA) Alert Mail - 재고 연계 실패"]
    assert fwd["is_error"]
    # 원본이 아니라 사본임이 결과에 드러나야 한다.
    assert fwd["via_prefix"] is True
    assert rows["(EAA) Alert Mail - 주문 연계 실패"]["via_prefix"] is False


def test_unrelated_forwarded_mail_never_matches(monkeypatch: Any):
    """머리말 제거를 켜도 '키워드 없는 FW:' 메일이 걸려서는 안 된다."""
    monkeypatch.setattr(iferr, "MAIL_SUBJECT_MATCH", "startswith")
    monkeypatch.setattr(iferr, "MAIL_SUBJECT_KEYWORDS", ("(EAA) Alert Mail",))
    monkeypatch.setattr(iferr, "MAIL_SUBJECT_STRIP_PREFIXES", ("RE:", "FW:"))

    m = outlook.Mail(
        subject="FW: 전혀 다른 제목", body="", sender="a@b", received=None, source=""
    )
    assert iferr.match_info(m) == ("", False)


def test_normalize_subject_strips_repeated_prefixes(monkeypatch: Any):
    """머리말이 여러 번 붙는 경우가 있어 한 번만 떼면 부족하다."""
    monkeypatch.setattr(
        iferr, "MAIL_SUBJECT_STRIP_PREFIXES", ("RE:", "FW:", "회신:", "전달:")
    )
    assert iferr.normalize_subject("RE: FW: (EAA) Alert") == "(EAA) Alert"
    assert iferr.normalize_subject("  회신: 전달: 제목  ") == "제목"
    assert iferr.normalize_subject("정상 제목") == "정상 제목"


def test_no_stripping_by_default(monkeypatch: Any):
    """기본 설정에서는 제목을 그대로 쓴다."""
    monkeypatch.setattr(iferr, "MAIL_SUBJECT_STRIP_PREFIXES", ())
    assert iferr.normalize_subject("RE: 제목") == "RE: 제목"


def test_contains_mode_is_still_default(monkeypatch: Any):
    """기본 동작은 바뀌지 않는다."""
    monkeypatch.setattr(iferr, "MAIL_SUBJECT_MATCH", "contains")
    monkeypatch.setattr(iferr, "MAIL_SUBJECT_KEYWORDS", ("(EAA) Alert Mail",))
    assert "문의: (EAA) Alert Mail 설정 관련" in _subjects()


def test_match_is_case_insensitive(monkeypatch: Any):
    monkeypatch.setattr(iferr, "MAIL_SUBJECT_MATCH", "startswith")
    monkeypatch.setattr(iferr, "MAIL_SUBJECT_KEYWORDS", ("(eaa) ALERT mail",))
    assert "(EAA) Alert Mail - 주문 연계 실패" in _subjects()


def test_regex_mode(monkeypatch: Any):
    monkeypatch.setattr(iferr, "MAIL_SUBJECT_MATCH", "regex")
    monkeypatch.setattr(iferr, "MAIL_SUBJECT_KEYWORDS", (r"^\(EAA\)\s*Alert",))
    hits = _subjects()
    assert "(EAA) Alert Mail - 주문 연계 실패" in hits
    assert "문의: (EAA) Alert Mail 설정 관련" not in hits


def test_check_subject_rule_catches_bad_config(monkeypatch: Any):
    """설정이 조용히 잘못되면 '오류 메일 없음'으로 보인다. 그게 제일 나쁘다."""
    monkeypatch.setattr(iferr, "MAIL_SUBJECT_MATCH", "starts_with")   # 오타
    assert any("MAIL_SUBJECT_MATCH" in p for p in iferr.check_subject_rule())

    monkeypatch.setattr(iferr, "MAIL_SUBJECT_MATCH", "contains")
    monkeypatch.setattr(iferr, "MAIL_SUBJECT_KEYWORDS", ())
    assert any("비어 있어" in p for p in iferr.check_subject_rule())

    monkeypatch.setattr(iferr, "MAIL_SUBJECT_KEYWORDS", ("오", "류"))
    assert any("한 글자" in p for p in iferr.check_subject_rule())

    monkeypatch.setattr(iferr, "MAIL_SUBJECT_MATCH", "regex")
    monkeypatch.setattr(iferr, "MAIL_SUBJECT_KEYWORDS", ("(EAA",))    # 안 닫힌 괄호
    assert any("정규식" in p for p in iferr.check_subject_rule())


def test_bad_regex_does_not_crash(monkeypatch: Any):
    """잘못된 정규식이 있어도 예외로 죽지 않는다(점검에서 알려 준다)."""
    monkeypatch.setattr(iferr, "MAIL_SUBJECT_MATCH", "regex")
    monkeypatch.setattr(iferr, "MAIL_SUBJECT_KEYWORDS", ("(EAA", r"^\(EAA\)"))
    assert "(EAA) Alert Mail - 주문 연계 실패" in _subjects()


# --------------------------------------------------------------------------
# 접두어 기반 키 추출 (IFERR_KEY_PREFIXES)
# --------------------------------------------------------------------------


def _with_prefix(monkeypatch: Any, prefix: str = "ABCIF") -> None:
    """접두어 패턴만 켠 상태를 만든다."""
    import re as _re

    pattern = (
        f"prefix-{prefix.lower()}",
        rf"(?<![A-Za-z0-9_])({_re.escape(prefix)}[0-9]+)(?![A-Za-z0-9_])",
    )
    monkeypatch.setattr(iferr, "_COMPILED", [(pattern[0], _re.compile(pattern[1]))])


def test_prefix_pattern_extracts_id(monkeypatch: Any):
    _with_prefix(monkeypatch)
    hits = extract_keys("(EAA) Alert Mail - ABCIF0001234 전송 실패")
    assert [h["key"] for h in hits] == ["ABCIF0001234"]
    assert hits[0]["rule"] == "prefix-abcif"


def test_prefix_pattern_does_not_truncate(monkeypatch: Any):
    """ABCIF0001234_TMP 에서 숫자를 하나 뱉어 '잘린 키'를 만들면 안 된다.

    뒤쪽 경계에서 숫자를 빼면 정규식이 되돌아가며 ABCIF000123 을 만든다.
    잘린 키로 DB 를 조회하면 없는 행을 찾거나 엉뚱한 행을 집는다 —
    못 찾는 것보다 나쁘다.
    """
    _with_prefix(monkeypatch)
    assert extract_keys("대상: ABCIF0001234_TMP 임시테이블") == []


def test_prefix_pattern_respects_boundaries(monkeypatch: Any):
    _with_prefix(monkeypatch)
    assert extract_keys("xABCIF123") == []          # 앞에 글자가 붙으면 아니다
    assert [h["key"] for h in extract_keys("[ABCIF777] 오류")] == ["ABCIF777"]
    assert [h["key"] for h in extract_keys("ABCIF777.")] == ["ABCIF777"]


def test_prefix_pattern_needs_digits(monkeypatch: Any):
    _with_prefix(monkeypatch)
    assert extract_keys("ABCIF 인터페이스 전반") == []


def test_prefix_id_is_found_in_real_mail(monkeypatch: Any):
    """샘플 메일(08)에서 실제로 뽑히는지."""
    _with_prefix(monkeypatch)
    monkeypatch.setattr(iferr, "MAIL_SUBJECT_MATCH", "startswith")
    monkeypatch.setattr(iferr, "MAIL_SUBJECT_KEYWORDS", ("(EAA) Alert Mail",))

    rows = {m["subject"]: m for m in iferr.list_mails()["mails"]}
    mail = rows["(EAA) Alert Mail - ABCIF0001234 전송 실패"]
    assert mail["is_error"]
    # 본문의 _TMP 테이블명은 키가 아니다. 중복 없이 하나만 나와야 한다.
    assert mail["keys"] == ["ABCIF0001234"]


# --------------------------------------------------------------------------
# DB 진단 (스키마 탐색)
# --------------------------------------------------------------------------


def test_check_connection_without_config(monkeypatch: Any):
    """설정이 없으면 예외가 아니라 이유를 돌려준다(진단 화면은 계속 진행)."""
    from core import oracle

    monkeypatch.setattr(oracle, "ORACLE_DSN", None)
    ok, msg = oracle.check_connection()
    assert ok is False and "config_local.py" in msg


def test_identifier_validation_blocks_injection():
    """테이블·컬럼 이름은 바인드로 못 넘겨 문자열에 들어간다.

    그래서 데이터 딕셔너리에서 온 이름만 쓰고, 그것도 다시 검증한다.
    """
    from core import oracle

    assert oracle._quote_ident("if_hdr") == '"IF_HDR"'
    assert oracle._quote_ident("IF_HDR$1") == '"IF_HDR$1"'
    for bad in ("1TABLE", "IF HDR", 'IF"HDR', "DROP TABLE X", "", "a" * 31):
        with pytest.raises(ValueError):
            oracle._quote_ident(bad)


def test_find_value_uses_bind_for_the_value(monkeypatch: Any):
    """값은 반드시 바인드로 들어간다. 이름만 문자열에 들어간다."""
    from core import oracle

    calls: list[dict[str, Any]] = []

    def fake_query(sql: str, binds: Any = None, timeout: Any = None):
        calls.append({"sql": sql, "binds": binds})
        if "all_tab_columns" in sql:
            return [
                {"OWNER": "ERP", "TABLE_NAME": "IF_HDR", "COLUMN_NAME": "IF_KEY"}
            ]
        return [{"CNT": 3}]

    monkeypatch.setattr(oracle, "query", fake_query)
    hits = oracle.find_value("EAIIF0001234", name_like="IF", schema="ERP")

    assert hits == [{"table": "IF_HDR", "column": "IF_KEY", "count": 3}]
    count_sql = calls[-1]
    assert count_sql["binds"] == {"v": "EAIIF0001234"}
    assert "EAIIF0001234" not in count_sql["sql"]
    assert '"ERP"."IF_HDR"' in count_sql["sql"]


def test_find_value_reports_truncated_candidates(monkeypatch: Any):
    """후보를 잘랐으면 그 사실이 결과에 남아야 한다.

    조용히 자르면 '없다'로 읽히는데, 안 본 컬럼에 있을 수 있다.
    """
    from core import oracle

    def fake_query(sql: str, binds: Any = None, timeout: Any = None):
        if "all_tab_columns" in sql:
            return [
                {"OWNER": "ERP", "TABLE_NAME": f"T{i}", "COLUMN_NAME": "IF_KEY"}
                for i in range(10)
            ]
        return [{"CNT": 0}]

    monkeypatch.setattr(oracle, "query", fake_query)
    hits = oracle.find_value("X", schema="ERP", max_tables=3)
    notes = [h for h in hits if h.get("error")]
    assert notes and "후보 10개 중 3개만" in notes[0]["error"]


def test_find_value_reports_unreadable_columns(monkeypatch: Any):
    """권한 없는 테이블을 조용히 넘기면 '없다'로 오해한다."""
    from core import oracle

    def fake_query(sql: str, binds: Any = None, timeout: Any = None):
        if "all_tab_columns" in sql:
            return [{"OWNER": "ERP", "TABLE_NAME": "T", "COLUMN_NAME": "IF_KEY"}]
        raise RuntimeError("ORA-00942: table or view does not exist")

    monkeypatch.setattr(oracle, "query", fake_query)
    hits = oracle.find_value("X", schema="ERP")
    assert hits[0]["count"] is None and "ORA-00942" in hits[0]["error"]


def test_dpy3010_triggers_thick_mode_retry(monkeypatch: Any):
    """DPY-3010 은 '이 서버 버전은 thin 모드 미지원'이다.

    안내만 하고 끝내면 사용자가 설정을 고치고 다시 실행해야 한다.
    Oracle Client 가 있으면 그 자리에서 전환해 재시도한다.
    """
    from core import oracle

    events: list[str] = []

    class FakeDriver:
        def init_oracle_client(self, lib_dir: Any = None) -> None:
            events.append("init_thick")

        def connect(self, **kw: Any) -> Any:
            events.append("connect")
            if events.count("connect") == 1:
                raise RuntimeError("DPY-3010: connections to this database server ...")

            class Conn:
                call_timeout = 0

                def cursor(self):
                    raise AssertionError("이 테스트에서는 쿼리까지 가지 않는다")

                def close(self):
                    pass

            return Conn()

    monkeypatch.setattr(oracle, "_client_mode", "thin")
    monkeypatch.setattr(oracle, "_import_driver", lambda: FakeDriver())
    monkeypatch.setattr(oracle, "ORACLE_DSN", "host/db")
    monkeypatch.setattr(oracle, "ORACLE_USER", "u")
    monkeypatch.setattr(oracle, "ORACLE_PASSWORD", "p")

    with oracle.get_conn():
        pass

    assert events == ["connect", "init_thick", "connect"]
    assert oracle.client_mode() == "thick"
    # 모드는 프로세스 전역이라 되돌려 두지 않으면 뒤 테스트에 샌다.
    monkeypatch.setattr(oracle, "_client_mode", "thin")


def test_thick_mode_failure_explains_how_to_fix(monkeypatch: Any):
    """클라이언트가 없으면 무엇을 해야 하는지 알려 준다."""
    from core import oracle

    class FakeDriver:
        def init_oracle_client(self, lib_dir: Any = None) -> None:
            raise RuntimeError("DPI-1047: Cannot locate a 64-bit Oracle Client library")

        def connect(self, **kw: Any) -> Any:
            raise RuntimeError("DPY-3010: not supported")

    monkeypatch.setattr(oracle, "_client_mode", "thin")
    monkeypatch.setattr(oracle, "_import_driver", lambda: FakeDriver())
    monkeypatch.setattr(oracle, "ORACLE_DSN", "host/db")
    monkeypatch.setattr(oracle, "ORACLE_USER", "u")
    monkeypatch.setattr(oracle, "ORACLE_PASSWORD", "p")

    with pytest.raises(oracle.OracleUnavailable) as ei:
        with oracle.get_conn():
            pass
    msg = str(ei.value)
    assert "ORACLE_CLIENT_LIB_DIR" in msg and "32/64비트" in msg


# --------------------------------------------------------------------------
# IF_MST 기반 영향 판정
# --------------------------------------------------------------------------


def _fake_master(monkeypatch: Any, rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """IF_MST 조회를 흉내 낸다."""
    calls: list[dict[str, Any]] = []
    monkeypatch.setattr(iferr.oracle, "is_configured", lambda: True)
    # 마스터 테이블 이름은 설정에서 온다. 테스트는 그 설정을 흉내 낸다.
    monkeypatch.setattr(
        iferr,
        "IFERR_SQL",
        {
            "header": config.build_master_sql(
                "IF_MST",
                {
                    "id": "IFID", "src_sys": "SRCSYS", "tar_sys": "TARSYS",
                    "src_table": "SRCTNAME", "tar_table": "TARTNAME",
                },
            ),
            "detail": "",
            "impact": "",
        },
    )
    monkeypatch.setattr(
        iferr,
        "IFERR_MASTER_FIELDS",
        {
            "id": "IFID", "src_sys": "SRCSYS", "tar_sys": "TARSYS",
            "src_table": "SRCTNAME", "tar_table": "TARTNAME",
        },
    )

    def fake_query(sql: str, binds: Any = None, timeout: Any = None):
        calls.append({"sql": sql, "binds": binds})
        return rows

    monkeypatch.setattr(iferr.oracle, "query", fake_query)
    return calls


def test_master_row_becomes_impact_text(monkeypatch: Any):
    """IF_MST 한 행이 '무엇이 어디로 가는가'로 요약되어야 한다.

    인터페이스가 실패했다는 것은 타겟 테이블에 데이터가 들어가지 않았다는
    뜻이다. 그 경로가 곧 영향 범위다.
    """
    _fake_master(
        monkeypatch,
        [
            {
                "IFID": "ABCIF0001234",
                "SRCSYS": "SAP",
                "TARSYS": "ERP",
                "SRCTNAME": "ZORDER",
                "TARTNAME": "IF_ORDER_TMP",
            }
        ],
    )
    c = run_iferr(key="ABCIF0001234", detail="full")["cases"][0]

    assert c["db"]["status"] == "found" and c["rule"] == "rows-found"
    assert "[SAP] ZORDER → [ERP] IF_ORDER_TMP" in c["impact"]
    assert c["flows"][0]["tar_table"] == "IF_ORDER_TMP"


def test_master_lookup_uses_bind(monkeypatch: Any):
    """IFID 는 반드시 바인드로 들어간다."""
    calls = _fake_master(monkeypatch, [{"IFID": "ABCIF0001234"}])
    run_iferr(key="ABCIF0001234", detail="full")
    assert calls[0]["binds"] == {"if_key": "ABCIF0001234"}
    assert "ABCIF0001234" not in calls[0]["sql"]
    assert "IF_MST" in calls[0]["sql"] and "IFID = :if_key" in calls[0]["sql"]


def test_master_field_names_are_configurable(monkeypatch: Any):
    """사이트마다 컬럼 이름이 다를 수 있다."""
    _fake_master(
        monkeypatch,
        [
            {
                "INTERFACE_ID": "X1",
                "FROM_SYS": "MES",
                "TO_SYS": "ERP",
                "FROM_TAB": "T1",
                "TO_TAB": "T2",
            }
        ],
    )
    monkeypatch.setattr(
        iferr,
        "IFERR_MASTER_FIELDS",
        {
            "id": "INTERFACE_ID",
            "src_sys": "FROM_SYS",
            "tar_sys": "TO_SYS",
            "src_table": "FROM_TAB",
            "tar_table": "TO_TAB",
        },
    )
    c = run_iferr(key="X1", detail="full")["cases"][0]
    assert "[MES] T1 → [ERP] T2" in c["impact"]


def test_summary_carries_flows(monkeypatch: Any):
    """경로는 LLM 컨텍스트에 들어가야 할 핵심 정보다."""
    _fake_master(
        monkeypatch,
        [{"IFID": "X1", "SRCSYS": "SAP", "TARSYS": "ERP",
          "SRCTNAME": "A", "TARTNAME": "B"}],
    )
    s = run_iferr(key="X1", detail="summary")["cases"][0]
    assert s["flows"][0]["src_sys"] == "SAP"
    assert "[SAP] A → [ERP] B" in s["impact"]


def test_schema_prefix_is_validated(monkeypatch: Any):
    """{schema} 치환에도 식별자 검증이 걸려야 한다."""
    from core import oracle

    monkeypatch.setattr(oracle, "ORACLE_SCHEMA", "ERP")
    assert oracle.render_sql("SELECT * FROM {schema}IF_MST") == (
        'SELECT * FROM "ERP".IF_MST'
    )

    monkeypatch.setattr(oracle, "ORACLE_SCHEMA", "ERP; DROP TABLE X")
    with pytest.raises(ValueError):
        oracle.render_sql("SELECT * FROM {schema}IF_MST")

    monkeypatch.setattr(oracle, "ORACLE_SCHEMA", None)
    assert oracle.render_sql("SELECT * FROM {schema}IF_MST") == (
        "SELECT * FROM IF_MST"
    )


# --------------------------------------------------------------------------
# 스케줄 (하루 여러 번 도는 인터페이스)
# --------------------------------------------------------------------------


def test_schedule_text_groups_by_day():
    """같은 일자의 시각은 한 줄로 묶는다."""
    assert iferr._schedule_text([("*", "8", "30"), ("*", "12", "0")]) == (
        "매일 08:30, 12:00"
    )
    assert iferr._schedule_text([("5", "3", "15")]) == "5일 03:15"
    assert iferr._schedule_text([("*", "8", "30"), ("5", "3", "15")]) == (
        "매일 08:30 / 5일 03:15"
    )


def test_schedule_text_dedups_and_sorts():
    """같은 시각이 중복으로 들어오기도 한다. 순서도 고정한다."""
    assert iferr._schedule_text(
        [("*", "18", "0"), ("*", "8", "30"), ("*", "18", "0")]
    ) == "매일 08:30, 18:00"


def test_schedule_text_keeps_non_numeric():
    """시/분에 '*' 가 들어가도 예외 없이 원문을 보여준다."""
    assert iferr._schedule_text([("*", "*", "*")]) == "매일 *:*"
    assert iferr._schedule_text([]) == ""


def _master_fields_with_schedule() -> dict[str, str]:
    return {
        "id": "IFID", "src_sys": "SRCSYS", "tar_sys": "TARSYS",
        "src_table": "SRCTNAME", "tar_table": "TARTNAME",
        "sch_day": "SCH_DAY", "sch_h": "SCH_H", "sch_m": "SCH_M",
    }


def test_multiple_schedule_rows_group_into_one_interface(monkeypatch: Any):
    """하루 여러 번 도는 인터페이스는 IFID 로 묶여 한 건이어야 한다.

    묶지 않으면 같은 인터페이스가 여러 건으로 보여 '몇 개가 깨진 거지'를
    셀 수 없다.
    """
    rows = [
        {"IFID": "X1", "SRCSYS": "SAP", "TARSYS": "ERP", "SRCTNAME": "ZORDER",
         "TARTNAME": "IF_ORD", "SCH_DAY": "*", "SCH_H": h, "SCH_M": m}
        for h, m in (("8", "30"), ("12", "0"), ("18", "0"))
    ]
    _fake_master(monkeypatch, rows)
    monkeypatch.setattr(iferr, "IFERR_MASTER_FIELDS", _master_fields_with_schedule())

    c = run_iferr(key="X1", detail="full")["cases"][0]
    assert len(c["flows"]) == 1                    # 3행 → 1건
    assert c["flows"][0]["rows"] == 3              # 원 행 수는 남긴다
    assert c["flows"][0]["schedule"] == "매일 08:30, 12:00, 18:00"
    assert "[SAP] ZORDER → [ERP] IF_ORD (매일 08:30, 12:00, 18:00)" in c["impact"]


def test_different_ifids_stay_separate(monkeypatch: Any):
    """다른 IFID 는 묶이면 안 된다."""
    rows = [
        {"IFID": "X1", "SRCSYS": "SAP", "TARSYS": "ERP", "SRCTNAME": "A",
         "TARTNAME": "B", "SCH_DAY": "*", "SCH_H": "8", "SCH_M": "0"},
        {"IFID": "X2", "SRCSYS": "MES", "TARSYS": "ERP", "SRCTNAME": "C",
         "TARTNAME": "D", "SCH_DAY": "5", "SCH_H": "3", "SCH_M": "0"},
    ]
    _fake_master(monkeypatch, rows)
    monkeypatch.setattr(iferr, "IFERR_MASTER_FIELDS", _master_fields_with_schedule())

    flows = run_iferr(key="X1", detail="full")["cases"][0]["flows"]
    assert [f["id"] for f in flows] == ["X1", "X2"]
    assert flows[1]["schedule"] == "5일 03:00"


def test_schedule_reaches_summary(monkeypatch: Any):
    """스케줄은 LLM 이 답할 때 필요한 정보라 summary 에도 있어야 한다."""
    _fake_master(
        monkeypatch,
        [{"IFID": "X1", "SRCSYS": "SAP", "TARSYS": "ERP", "SRCTNAME": "A",
          "TARTNAME": "B", "SCH_DAY": "*", "SCH_H": "9", "SCH_M": "5"}],
    )
    monkeypatch.setattr(iferr, "IFERR_MASTER_FIELDS", _master_fields_with_schedule())

    s = run_iferr(key="X1", detail="summary")["cases"][0]
    assert s["flows"][0]["schedule"] == "매일 09:05"
    assert "매일 09:05" in s["impact"]


# --------------------------------------------------------------------------
# --doctor (한 번에 점검)
# --------------------------------------------------------------------------


def test_doctor_reports_every_step(monkeypatch: Any):
    """앞 단계가 실패해도 뒤 단계를 건너뛰지 않는다. 문제가 둘일 수 있다."""
    monkeypatch.setattr(iferr, "MAIL_SUBJECT_MATCH", "startswith")
    monkeypatch.setattr(iferr, "MAIL_SUBJECT_KEYWORDS", ("(EAA) Alert Mail",))

    steps = {name: (ok, msg) for name, ok, msg in iferr.doctor()}
    assert set(steps) == {
        "제목 판정 설정", "메일 읽기", "키 추출", "DB 접속", "마스터 조회"
    }
    # DB 는 설정이 없으니 실패지만, 그 앞 단계들은 정상으로 나와야 한다.
    assert steps["메일 읽기"][0] is True
    assert steps["DB 접속"][0] is False


def test_doctor_flags_mail_failure_with_next_step(monkeypatch: Any):
    """실패에는 다음에 뭘 해야 하는지가 붙어야 한다."""
    monkeypatch.setattr(outlook, "MAIL_EML_DIR", "./no/such/dir")
    steps = {name: (ok, msg) for name, ok, msg in iferr.doctor()}
    ok, msg = steps["메일 읽기"]
    assert ok is False and "--folders" in msg


def test_doctor_flags_missing_keys(monkeypatch: Any):
    """오류 메일은 있는데 키를 못 뽑으면 그 사실이 드러나야 한다."""
    monkeypatch.setattr(iferr, "MAIL_SUBJECT_MATCH", "contains")
    monkeypatch.setattr(iferr, "MAIL_SUBJECT_KEYWORDS", ("응답 없음",))
    monkeypatch.setattr(iferr, "_COMPILED", [])      # 어떤 패턴도 없다

    steps = {name: (ok, msg) for name, ok, msg in iferr.doctor()}
    ok, msg = steps["키 추출"]
    assert ok is False and "IFERR_KEY_PREFIXES" in msg


def test_doctor_runs_a_real_lookup(monkeypatch: Any):
    """설정이 다 되어 있으면 실제 키로 조회까지 해 본다."""
    _fake_master(
        monkeypatch,
        [{"IFID": "ABCIF0001234", "SRCSYS": "SAP", "TARSYS": "ERP",
          "SRCTNAME": "A", "TARTNAME": "B", "SCH_DAY": "*", "SCH_H": "8",
          "SCH_M": "30"}],
    )
    monkeypatch.setattr(iferr, "IFERR_MASTER_FIELDS", _master_fields_with_schedule())
    monkeypatch.setattr(iferr, "MAIL_SUBJECT_MATCH", "startswith")
    monkeypatch.setattr(iferr, "MAIL_SUBJECT_KEYWORDS", ("(EAA) Alert Mail",))
    _with_prefix(monkeypatch)
    monkeypatch.setattr(
        iferr.oracle, "check_connection", lambda timeout=None: (True, "접속 OK")
    )

    steps = {name: (ok, msg) for name, ok, msg in iferr.doctor()}
    ok, msg = steps["마스터 조회"]
    assert ok is True
    assert "ABCIF0001234" in msg and "매일 08:30" in msg


# --------------------------------------------------------------------------
# 기간 해석 (소형 모델이 "3일"을 3으로 넣던 문제)
# --------------------------------------------------------------------------


def test_parse_period_days_and_hours():
    """'3일'은 72시간이다. 모델에 계산을 맡기면 3을 넣는다 — 실제로 그랬다."""
    assert iferr.parse_period("3일", 24)[0] == 72
    assert iferr.parse_period("최근 3일", 24)[0] == 72
    assert iferr.parse_period("72시간", 24)[0] == 72
    assert iferr.parse_period("2주", 24)[0] == 336
    assert iferr.parse_period("3 days", 24)[0] == 72


def test_parse_period_bare_number_is_hours():
    """단위 없는 숫자는 예전 인자와 같은 뜻(시간)으로 본다."""
    assert iferr.parse_period("3", 24)[0] == 3


def test_parse_period_empty_uses_default():
    hours, label, warn = iferr.parse_period("", 24)
    assert hours == 24 and not warn and "24시간" in label


def test_parse_period_unknown_warns_instead_of_silent_default():
    """못 알아들으면 조용히 기본값을 쓰지 않는다.

    '3일이라 했는데 왜 하루치지'를 알 수 있어야 한다.
    """
    hours, _, warn = iferr.parse_period("내일모레쯤", 24)
    assert hours == 24
    assert "이해하지 못해" in warn and "확인 필요" in warn


def test_tool_reports_the_period_it_used():
    """답에 기간이 드러나야 한다.

    MCP 는 모르는 인자를 조용히 버린다. 모델이 hours=72 처럼 없는 인자를
    넣으면 기본값으로 돌게 되는데, 그때도 사용자가 알아챌 수 있어야 한다.
    """

    async def call(c: Client) -> Any:
        return await c.call_tool("check_interface_errors", {"hours": 72})

    text = _run(_with_client(call)).content[0].text
    assert "최근 24시간" in text     # 무시된 인자 대신 기본값을 썼음이 드러난다


def test_tool_warns_on_unparsed_period():
    async def call(c: Client) -> Any:
        return await c.call_tool("check_interface_errors", {"period": "언젠가"})

    text = _run(_with_client(call)).content[0].text
    assert "이해하지 못해" in text


# --------------------------------------------------------------------------
# IFID → 참조 테이블 (find_interface_tables)
# --------------------------------------------------------------------------


def _master_row(ifid: str, src: str, tar: str, h: str = "8") -> dict[str, Any]:
    return {"IFID": ifid, "SRCSYS": "SAP", "TARSYS": "ERP", "SRCTNAME": src,
            "TARTNAME": tar, "SCH_DAY": "*", "SCH_H": h, "SCH_M": "30"}


def test_interface_tables_lists_source_and_target(monkeypatch: Any):
    _fake_master(monkeypatch, [_master_row("X1", "ZORDER", "IF_ORD"),
                               _master_row("X1", "ZORDER", "IF_ORD", h="12")])
    monkeypatch.setattr(iferr, "IFERR_MASTER_FIELDS", _master_fields_with_schedule())
    r = iferr.interface_tables("X1")
    assert r["status"] == "found"
    # 스케줄 행이 여러 개여도 테이블은 한 번씩만.
    assert [(t["role"], t["system"], t["table"]) for t in r["tables"]] == [
        ("source", "SAP", "ZORDER"), ("target", "ERP", "IF_ORD"),
    ]


def test_interface_tables_does_not_read_mail_or_call_llm(monkeypatch: Any):
    """마스터 한 번이면 끝나는 질문이다. 메일도 요약용 LLM 도 부르지 않는다."""
    _fake_master(monkeypatch, [_master_row("X1", "ZORDER", "IF_ORD")])

    def boom(*a: Any, **k: Any) -> Any:
        raise AssertionError("부르면 안 된다")

    monkeypatch.setattr(iferr, "read_mails", boom)
    monkeypatch.setattr(iferr, "explain", boom)
    assert iferr.interface_tables("X1")["tables"]


def test_interface_tables_unknown_is_not_empty(monkeypatch: Any):
    """DB 설정이 없으면 '테이블 없음'이 아니라 '확인 불가'다."""
    r = iferr.interface_tables("X1")
    assert r["status"] == "unknown" and r["tables"] == []
    assert r["warnings"]


def test_interface_tables_missing_id(monkeypatch: Any):
    _fake_master(monkeypatch, [])
    assert iferr.interface_tables("NOPE")["status"] == "missing"


def test_interface_tables_rows_without_table_names_warn(monkeypatch: Any):
    _fake_master(monkeypatch, [_master_row("X1", "", "")])
    r = iferr.interface_tables("X1")
    assert r["status"] == "found" and r["tables"] == []
    assert any("src_table / tar_table" in w for w in r["warnings"])


def test_mcp_find_interface_tables(monkeypatch: Any):
    _fake_master(monkeypatch, [_master_row("X1", "ZORDER", "IF_ORD")])

    async def call(c: Client) -> Any:
        tools = {t.name: t for t in (await c.list_tools()).tools}
        return tools, await c.call_tool("find_interface_tables", {"key": "X1"})

    tools, result = _run(_with_client(call))
    assert tools["find_interface_tables"].annotations.read_only_hint is True
    text = result.content[0].text
    assert "출발: [SAP] ZORDER" in text and "도착: [ERP] IF_ORD" in text
    # 다음 질문(영향도)으로 이어지게 도착 테이블을 짚어 준다.
    assert "IF_ORD 테이블 영향도 조사해줘" in text


def test_mcp_find_interface_tables_says_unknown():
    async def call(c: Client) -> Any:
        return await c.call_tool("find_interface_tables", {"key": "X1"})

    text = _run(_with_client(call)).content[0].text
    assert "확인 불가" in text and "없다는 뜻이 아니다" in text


# --------------------------------------------------------------------------
# 원인 컬럼 추출 — 메일의 오류 내용에서 '어느 테이블의 어느 컬럼'
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "text, want",
    [
        ('ORA-12899: value too large for column "ERP"."IF_ORDER_TMP"."ORD_NO" '
         "(actual: 25, maximum: 20)",
         ("size", "ERP", "IF_ORDER_TMP", "ORD_NO", "25", "20")),
        # 한글 Oracle 메시지
        ('ORA-12899: "ERP"."IF_ORDER_TMP"."ORD_NO" 열에 대한 값이 너무 큼(실제: 25, 최대값: 20)',
         ("size", "ERP", "IF_ORDER_TMP", "ORD_NO", "25", "20")),
        ('ORA-01400 cannot insert NULL into ("ERP"."ORD_HDR"."CUST_CD")',
         ("null", "ERP", "ORD_HDR", "CUST_CD", "", "")),
        # 스키마 없이 테이블.컬럼
        ('ORA-12899: value too large for column "IF_ORDER_TMP"."ORD_NO" (actual: 9, maximum: 8)',
         ("size", "", "IF_ORDER_TMP", "ORD_NO", "9", "8")),
    ],
)
def test_extract_issues_from_oracle_messages(text: str, want: tuple[str, ...]):
    i = iferr.extract_issues(text)[0]
    assert (i["kind"], i["schema"], i["table"], i["column"], i["actual"], i["max"]) == want


def test_issue_without_column_name_is_kept_as_unknown_column():
    """ORA-01438 은 컬럼 이름이 없다. '원인 없음'이 아니라 '컬럼 미상'으로 남긴다."""
    i = iferr.extract_issues("ORA-01438: value larger than specified precision")[0]
    assert i["kind"] == "precision" and i["column"] == ""
    assert "컬럼 미상" in iferr.issue_text(i)


def test_issue_text():
    i = iferr.extract_issues('ORA-12899: value too large for column "E"."T"."C" (actual: 25, maximum: 20)')[0]
    assert iferr.issue_text(i) == "T.C 길이 초과(실제 25 > 최대 20)"


def test_llm_columns_must_be_in_the_mail():
    """LLM 이 낸 컬럼은 메일에 적혀 있을 때만 쓴다 — 지어낸 컬럼으로 영향도를 조사하면 안 된다."""
    text = "주문번호(ORD_NO) 길이가 넘어 실패했습니다"
    got = iferr.verify_llm_columns(text, ["ORD_NO", "CUST_NM", "IF_ORDER_TMP.ORD_NO"])
    assert [(g["table"], g["column"]) for g in got] == [("", "ORD_NO")]   # 테이블은 메일에 없다


HUMAN_MAIL = (
    "안녕하세요.\n"
    "주문번호(ORD_NO) 체계가 20자리 → 25자리로 바뀌어 적재가 실패했습니다.\n"
    "고객코드 CUST_CD 가 비어 있는 건이 있습니다.\n"
    "금액 TOTAL_AMT 형식이 맞지 않습니다.\n"
)


def test_llm_column_gets_the_problem_from_its_own_line():
    """컬럼만 집고 '컬럼 오류' 로 끝나면 무슨 문제였는지 모른다. 그 줄의 말로 가른다."""
    got = {g["column"]: g for g in iferr.verify_llm_columns(
        HUMAN_MAIL, ["ORD_NO", "CUST_CD", "TOTAL_AMT"], "아무 요약")}
    assert iferr.issue_text(got["ORD_NO"]) == "ORD_NO 길이 초과(실제 25 > 최대 20)"
    # 다음 줄의 '비어 있는' 이 위 줄 컬럼에 붙지 않는다.
    assert iferr.issue_text(got["CUST_CD"]) == "CUST_CD NULL 불가"
    # 컬럼이 여럿이면 메일 하나짜리 LLM 요약을 어느 컬럼에도 붙이지 않는다.
    assert iferr.issue_text(got["TOTAL_AMT"]) == "TOTAL_AMT 컬럼 오류"
    assert got["TOTAL_AMT"]["detail"] == "금액 TOTAL_AMT 형식이 맞지 않습니다."


def test_llm_problem_is_used_when_rules_cannot_tell():
    i = iferr.verify_llm_columns(HUMAN_MAIL, ["TOTAL_AMT"], "금액 형식 오류")[0]
    assert iferr.issue_text(i) == "TOTAL_AMT 금액 형식 오류"


def test_llm_column_mixed_with_korean_name_is_kept():
    """qwen 이 '주문번호 (ORD_NO)' 로 냈다(실측). 버리지 않고 이름 조각을 쓴다."""
    i = iferr.verify_llm_columns(HUMAN_MAIL, ["주문번호 (ORD_NO)"], "길이 초과")[0]
    assert i["column"] == "ORD_NO" and i["kind"] == "size"


def test_table_written_before_the_column_is_not_an_interface_key():
    """'IF_ORDER_TMP 의 TOTAL_AMT' — 컬럼만 낸 LLM 답에서도 테이블을 잡아 IFID 에서 뺀다(실측)."""
    i = iferr.verify_llm_columns("IF_ORDER_TMP 의 TOTAL_AMT 에 소수점이 들어왔다", ["TOTAL_AMT"])[0]
    assert i["table"] == "IF_ORDER_TMP"
    mail = outlook.Mail(
        subject="[연계오류] 주문 금액 수신 실패",
        body="인터페이스 IF_AMT_RECV 처리 중 오류입니다.\n"
             "IF_ORDER_TMP 의 TOTAL_AMT 에 소수점이 들어와 변환하지 못했습니다.\n",
        sender="a@b.c", received=None, source="amt.eml")
    import agents.iferr.agent as mod
    verdict = {"is_error": True, "interface_ids": ["IF_AMT_RECV"], "cause_columns": ["TOTAL_AMT"],
               "cause_problem": "소수점 포함 변환 불가", "reason": "r", "note": ""}
    orig = (mod.llm_judge_enabled, mod.llm_judge_mail)
    mod.llm_judge_enabled, mod.llm_judge_mail = (lambda: True), (lambda m: verdict)
    try:
        j = iferr.judge_mails([mail])[0][0]
    finally:
        mod.llm_judge_enabled, mod.llm_judge_mail = orig
    assert [k["key"] for k in j["keys"]] == ["IF_AMT_RECV"]
    assert iferr.issue_text(j["issues"][0]) == "IF_ORDER_TMP.TOTAL_AMT 소수점 포함 변환 불가"


def test_oracle_issue_keeps_the_mail_line():
    i = iferr.extract_issues('IF_ID : X\n오류내용 : ORA-12899: value too large for column '
                             '"E"."T"."C" (actual: 25, maximum: 20)\n다음 줄')[0]
    assert i["detail"].startswith("오류내용 : ORA-12899") and "다음 줄" not in i["detail"]


def test_table_name_in_oracle_message_is_not_an_interface_key():
    """IF_ 로 시작하는 인터페이스 테이블이 if-prefix 규칙에 키로 잡혔다(실측)."""
    mail = outlook.Mail(
        subject="[연계오류] 주문 수신 실패",
        body='IF_ID : IF_ORD_RECV\n오류내용 : ORA-12899: value too large for column '
             '"ERP"."IF_ORDER_TMP"."ORD_NO" (actual: 25, maximum: 20)',
        sender="a@b.c", received=None, source="x.eml")
    j = iferr.judge_mails([mail])[0][0]
    assert [k["key"] for k in j["keys"]] == ["IF_ORD_RECV"]
    assert j["issues"][0]["column"] == "ORD_NO"


def test_case_carries_issues_from_its_mails(tmp_path: Path, monkeypatch: Any):
    from email.message import EmailMessage

    msg = EmailMessage()
    msg["Subject"], msg["Date"] = "[연계오류] 주문 수신 실패", "Thu, 03 Sep 2026 13:00:00 -0000"
    msg.set_content('IF_ID : IF_ORD_RECV\nORA-12899: value too large for column '
                    '"ERP"."IF_ORDER_TMP"."ORD_NO" (actual: 25, maximum: 20)\n')
    (tmp_path / "a.eml").write_bytes(bytes(msg))
    monkeypatch.setattr(outlook, "MAIL_EML_DIR", str(tmp_path))
    c = run_iferr(detail="summary")["cases"][0]
    assert c["key"] == "IF_ORD_RECV"
    assert [(i["table"], i["column"]) for i in c["issues"]] == [("IF_ORDER_TMP", "ORD_NO")]


# --------------------------------------------------------------------------
# 파일 마스터 — DB 가 없는 데모·오프라인
# --------------------------------------------------------------------------

DEMO_MASTER = str(Path(__file__).resolve().parents[1] / "samples" / "demo" / "if_master.json")


def test_master_file_answers_without_db(monkeypatch: Any):
    monkeypatch.setattr(iferr, "IFERR_MASTER_FILE", DEMO_MASTER)
    monkeypatch.setattr(iferr.oracle, "query", lambda *a, **k: (_ for _ in ()).throw(
        AssertionError("DB 를 보면 안 된다")))
    c = run_iferr(key="if_ord_recv", detail="full")["cases"][0]
    assert c["db"]["status"] == "found" and c["db"]["source"] == "file"
    assert c["flows"][0]["tar_table"] == "IF_ORDER_TMP"
    assert c["flows"][0]["schedule"] == "매일 08:30, 12:00"
    assert run_iferr(key="NOPE", detail="full")["cases"][0]["db"]["status"] == "missing"


def test_master_file_reverse_lookup(monkeypatch: Any):
    monkeypatch.setattr(iferr, "IFERR_MASTER_FILE", DEMO_MASTER)
    r = iferr.table_interfaces("if_order_tmp")
    assert r["status"] == "found"
    assert [(i["id"], i["roles"]) for i in r["interfaces"]] == [("IF_ORD_RECV", ["target"])]


def test_broken_master_file_is_unknown_not_missing(tmp_path: Path, monkeypatch: Any):
    bad = tmp_path / "m.json"
    bad.write_text("{not json", encoding="utf-8")
    monkeypatch.setattr(iferr, "IFERR_MASTER_FILE", str(bad))
    r = run_iferr(key="X1", detail="full")
    assert r["cases"][0]["db"]["status"] == "unknown"
    assert any("마스터 파일" in w for w in r["warnings"])


# --------------------------------------------------------------------------
# 테이블 → 관련 인터페이스 (find_table_interfaces)
# --------------------------------------------------------------------------


def _fake_table_master(monkeypatch: Any, rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """IF_MST 역방향 조회를 흉내 낸다. SQL 은 설정과 같은 방식으로 만든다."""
    calls: list[dict[str, Any]] = []
    fields = _master_fields_with_schedule()
    monkeypatch.setattr(iferr.oracle, "is_configured", lambda: True)
    monkeypatch.setattr(iferr, "IFERR_MASTER_FIELDS", fields)
    monkeypatch.setattr(iferr, "IFERR_TABLE_SQL", config.build_master_table_sql("IF_MST", fields))

    def fake_query(sql: str, binds: Any = None, timeout: Any = None):
        calls.append({"sql": sql, "binds": binds})
        return rows

    monkeypatch.setattr(iferr.oracle, "query", fake_query)
    return calls


def test_table_sql_binds_name_and_avoids_like():
    """값은 바인드로만. LIKE 는 '_' 가 와일드카드라 TB_ORDER 가 TBXORDER 를 잡는다."""
    sql = config.build_master_table_sql("IF_MST", _master_fields_with_schedule())
    assert "FROM {schema}IF_MST" in sql
    assert "UPPER(SRCTNAME) = :tname" in sql and "UPPER(TARTNAME) = :tname" in sql
    assert ":dotname" in sql and "LIKE" not in sql.upper()


def test_table_sql_needs_table_columns():
    """출발·도착 테이블 컬럼이 없으면 거꾸로 찾을 방법이 없다 — 빈 SQL."""
    assert config.build_master_table_sql("IF_MST", {"id": "IFID", "src_sys": "SRCSYS"}) == ""


def test_table_sql_rejects_bad_identifier():
    with pytest.raises(ValueError):
        config.build_master_table_sql("IF_MST; DROP", _master_fields_with_schedule())


def test_table_interfaces_marks_role_and_groups_schedules(monkeypatch: Any):
    calls = _fake_table_master(monkeypatch, [
        _master_row("X2", "IF_ORD", "ZORDER_BAK"),
        _master_row("X1", "ZORDER", "IF_ORD"),
        _master_row("X1", "ZORDER", "IF_ORD", h="12"),
    ])
    r = iferr.table_interfaces("if_ord")
    assert calls[0]["binds"] == {"tname": "IF_ORD", "dotname": ".IF_ORD"}
    assert r["status"] == "found" and r["decided_by"] == "rule"
    # 데이터가 들어오는 쪽(도착)이 먼저, 스케줄 행은 한 건으로 묶인다.
    assert [(i["id"], i["roles"]) for i in r["interfaces"]] == [
        ("X1", ["target"]), ("X2", ["source"]),
    ]
    assert r["interfaces"][0]["schedule"] == "매일 08:30, 12:30"


def test_table_interfaces_matches_schema_qualified_master_value(monkeypatch: Any):
    """마스터 값이 'EMP.IF_ORD' 여도, 사용자가 '[ERP] EMP.IF_ORD' 로 물어도 같은 테이블이다."""
    calls = _fake_table_master(monkeypatch, [_master_row("X1", "ZORDER", "EMP.IF_ORD")])
    r = iferr.table_interfaces("[ERP] EMP.IF_ORD")
    assert calls[0]["binds"]["tname"] == "IF_ORD"
    assert r["status"] == "found" and r["qualifier"] == "EMP"
    assert r["interfaces"][0]["roles"] == ["target"]


def test_table_interfaces_drops_rows_for_other_tables(monkeypatch: Any):
    """직접 쓴 SQL 이 넓게 잡아도 이름이 다른 테이블은 답에 섞지 않는다."""
    _fake_table_master(monkeypatch, [_master_row("X1", "ZORDER", "IF_ORD"),
                                     _master_row("X9", "A", "IF_ORD_TMP")])
    r = iferr.table_interfaces("IF_ORD")
    assert [i["id"] for i in r["interfaces"]] == ["X1"]
    assert any("뺐다" in w for w in r["warnings"])


def test_table_interfaces_only_mismatched_rows_is_unknown(monkeypatch: Any):
    _fake_table_master(monkeypatch, [_master_row("X9", "A", "IF_ORD_TMP")])
    r = iferr.table_interfaces("IF_ORD")
    assert r["status"] == "unknown" and r["rule"] == "name-mismatch"


def test_table_interfaces_none_is_missing(monkeypatch: Any):
    _fake_table_master(monkeypatch, [])
    r = iferr.table_interfaces("IF_ORD")
    assert r["status"] == "missing" and r["rule"] == "no-rows"


def test_table_interfaces_unknown_without_config(monkeypatch: Any):
    """SQL·DB 설정이 없으면 '인터페이스 없음'이 아니라 '확인 불가'다."""
    monkeypatch.setattr(iferr, "IFERR_TABLE_SQL", "")
    r = iferr.table_interfaces("IF_ORD")
    assert r["status"] == "unknown" and r["rule"] == "sql-not-configured" and r["warnings"]

    monkeypatch.setattr(iferr, "IFERR_TABLE_SQL", "SELECT 1 FROM DUAL")
    monkeypatch.setattr(iferr.oracle, "is_configured", lambda: False)
    assert iferr.table_interfaces("IF_ORD")["rule"] == "db-not-configured"


def test_table_interfaces_query_failure_is_unknown(monkeypatch: Any):
    _fake_table_master(monkeypatch, [])

    def boom(*a: Any, **k: Any):
        raise RuntimeError("ORA-00942")

    monkeypatch.setattr(iferr.oracle, "query", boom)
    r = iferr.table_interfaces("IF_ORD")
    assert r["status"] == "unknown" and r["rule"] == "query-failed"
    assert any("ORA-00942" in w for w in r["warnings"])


def test_table_interfaces_rejects_sentence_without_query(monkeypatch: Any):
    calls = _fake_table_master(monkeypatch, [])
    assert iferr.table_interfaces("")["rule"] == "no-table"
    assert iferr.table_interfaces("주문 테이블")["rule"] == "bad-name"
    assert not calls


def test_table_interfaces_does_not_read_mail_or_call_llm(monkeypatch: Any):
    _fake_table_master(monkeypatch, [_master_row("X1", "ZORDER", "IF_ORD")])

    def boom(*a: Any, **k: Any) -> Any:
        raise AssertionError("부르면 안 된다")

    monkeypatch.setattr(iferr, "read_mails", boom)
    monkeypatch.setattr(iferr, "explain", boom)
    assert iferr.table_interfaces("IF_ORD")["interfaces"]


def test_mcp_find_table_interfaces(monkeypatch: Any):
    _fake_table_master(monkeypatch, [_master_row("X2", "IF_ORD", "ZORDER_BAK"),
                                     _master_row("X1", "ZORDER", "IF_ORD")])

    async def call(c: Client) -> Any:
        tools = {t.name: t for t in (await c.list_tools()).tools}
        return tools, await c.call_tool("find_table_interfaces", {"table": "IF_ORD"})

    tools, result = _run(_with_client(call))
    assert tools["find_table_interfaces"].annotations.read_only_hint is True
    text = result.content[0].text
    assert "인터페이스 2개 (들어옴 1, 나감 1" in text
    assert "- X1 [들어옴]: [SAP] ZORDER → [ERP] IF_ORD (매일 08:30)" in text
    assert "- X2 [나감]:" in text
    # 다음 질문(오류 확인)으로 이어지게 데이터를 넣는 인터페이스를 짚어 준다.
    assert "X1 이 인터페이스 뭐가 문제야?" in text


def test_mcp_find_table_interfaces_says_unknown(monkeypatch: Any):
    monkeypatch.setattr(iferr, "IFERR_TABLE_SQL", "")

    async def call(c: Client) -> Any:
        return await c.call_tool("find_table_interfaces", {"table": "IF_ORD"})

    text = _run(_with_client(call)).content[0].text
    assert "확인 불가" in text and "없다는 뜻이 아니다" in text
