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
    monkeypatch.setattr(outlook, "MAIL_LOOKBACK_HOURS", 24)
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
    assert r["mail_count"] == 4


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
    """SQL 미설정은 '영향 없음'이 아니라 '확인 불가'다."""
    r = run_iferr(detail="full")
    for c in r["cases"]:
        assert c["db"]["status"] == "unknown"
        assert c["rule"] == "sql-not-configured"
        assert c["impact"] == "확인 불가"
    assert any("조회 SQL 이 설정되지 않았다" in w for w in r["warnings"])


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
        return await c.call_tool("check_interface_errors", {"hours": 24})

    text = _run(_with_client(call)).content[0].text
    assert "IF_ORD_SEND" in text
    assert "확인 불가" in text and "확인 필요" in text


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
        return await c.call_tool("list_error_mails", {"hours": 24})

    text = _run(_with_client(call)).content[0].text
    assert "IF_ORD_SEND" in text
    assert "오류 3통" in text
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
        return await c.call_tool("list_error_mails", {"hours": 24})

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
