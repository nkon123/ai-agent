"""incident — "오늘 인터페이스 오류 확인해줘" 한마디로 메일 → IFID → 테이블 → 원인 컬럼 → 영향도.

메일은 시험마다 tmp 에 만들고, 마스터는 samples/demo/if_master.json(가짜 값),
소스는 samples/colimpact·samples/src 를 쓴다. LLM 은 끈다(규칙 경로를 고정한다).

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
from urllib.parse import quote

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from mcp.client.client import Client  # noqa: E402

from agents.iferr import agent as iferr  # noqa: E402
from agents.impact import column  # noqa: E402
from agents.incident import agent as incident  # noqa: E402
from agents.usage import agent as usage_agent  # noqa: E402
from core import outlook  # noqa: E402
from mcp_server.tools import build_server  # noqa: E402

REPO = Path(__file__).resolve().parents[1]

def eml(subject: str, date: str, body: str) -> bytes:
    """제목에 한글이 있으면 헤더 인코딩이 필요하다 — 손으로 쓰면 제목이 깨져 오류로 안 잡힌다."""
    from email.message import EmailMessage

    msg = EmailMessage()
    msg["Subject"], msg["From"], msg["Date"] = subject, "if.monitor@partner.example", date
    msg.set_content(body)
    return bytes(msg)


SIZE_MAIL = eml(
    "[연계오류] 주문 수신 인터페이스 처리 실패", "Wed, 14 Oct 2026 08:00:00 -0000",
    "IF_ID : IF_ORD_RECV\n"
    'ORA-12899: value too large for column "ERP"."IF_ORDER_TMP"."ORD_NO" (actual: 25, maximum: 20)\n')
TIMEOUT_MAIL = eml(
    "[연계오류] 재고 수신 응답 없음", "Wed, 14 Oct 2026 07:00:00 -0000",
    "인터페이스 ID: IF_STK_RECV\n오류: timeout\n")


@pytest.fixture(autouse=True)
def demo(tmp_path: Path, monkeypatch: Any) -> Path:
    mail = tmp_path / "mail"
    mail.mkdir()
    (mail / "01.eml").write_bytes(SIZE_MAIL)
    (mail / "02.eml").write_bytes(TIMEOUT_MAIL)
    monkeypatch.setattr(outlook, "MAIL_BACKEND", "eml")
    monkeypatch.setattr(outlook, "MAIL_EML_DIR", str(mail))
    monkeypatch.setattr(outlook, "_now", lambda: datetime(2026, 10, 14, 12, 0))
    monkeypatch.setattr(iferr, "IFERR_MASTER_FILE", str(REPO / "samples/demo/if_master.json"))
    monkeypatch.setattr(iferr, "IFERR_MAIL_JUDGE", "keyword")
    monkeypatch.setattr(iferr, "USE_LLM", False)
    monkeypatch.setattr(column, "USE_LLM", False)
    monkeypatch.setattr(usage_agent, "SOURCE_ROOTS", {
        "ERP": str(REPO / "samples/colimpact"), "SAMPLE": str(REPO / "samples/src")})
    column._RESULTS.clear()
    incident._LAST.clear()
    return mail


def test_one_question_reaches_column_impact():
    """메일 → IFID → 마스터 테이블 → 원인 컬럼 → 그 컬럼을 쓰는 줄까지 한 번에."""
    r = incident.run_incident("3일")
    keys = [c["key"] for c in r["cases"]]
    # 원인 컬럼까지 밝혀진 케이스가 먼저다.
    assert keys == ["IF_ORD_RECV", "IF_STK_RECV"]
    c = r["cases"][0]
    assert c["master_source"] == "file" and c["flows"][0]["tar_table"] == "IF_ORDER_TMP"
    imp = c["impacts"][0]
    assert (imp["table"], imp["column"], imp["table_from"]) == ("IF_ORDER_TMP", "ORD_NO", "오류 메시지")
    assert imp["issue_text"] == "IF_ORDER_TMP.ORD_NO 길이 초과(실제 25 > 최대 20)"
    ci = imp["result"]
    assert ci["root"] == "ERP, SAMPLE" and ci["hit_count"] > 0 and ci["counts"]["yes"] > 0
    # 원인 컬럼이 없는 케이스는 영향도를 조사하지 않는다(지어내지 않는다).
    assert r["cases"][1]["impacts"] == []


def test_table_comes_from_master_when_message_has_no_table():
    c = {"key": "X", "flows": [{"tar_table": "EMP.IF_ORDER_TMP"}],
         "issues": [{"kind": "other", "rule": "llm", "schema": "", "table": "", "column": "ORD_NO",
                     "actual": "", "max": "", "evidence": ""}]}
    targets, warnings = incident.targets_of(c)
    assert [(t["table"], t["column"], t["table_from"]) for t in targets] == [
        ("IF_ORDER_TMP", "ORD_NO", "마스터 도착 테이블")]
    assert not warnings


def test_unknown_table_is_reported_not_guessed():
    c = {"key": "X", "flows": [{"tar_table": "A"}, {"tar_table": "B"}],
         "issues": [{"kind": "other", "rule": "llm", "schema": "", "table": "", "column": "C",
                     "actual": "", "max": "", "evidence": ""}]}
    targets, warnings = incident.targets_of(c)
    assert targets == [] and any("도착 테이블이 여럿" in w for w in warnings)


def test_impact_cap_is_reported(monkeypatch: Any):
    monkeypatch.setattr(incident, "INCIDENT_MAX_IMPACTS", 0)
    r = incident.run_incident("3일")
    assert r["cases"][0]["impacts"] == []
    assert any("상한" in w for w in r["warnings"])


def test_grid_reuses_the_last_investigation(monkeypatch: Any):
    incident.run_incident("3일")
    monkeypatch.setattr(incident, "run_iferr", lambda **k: (_ for _ in ()).throw(
        AssertionError("다시 조사하면 안 된다")))
    g = incident.incident_grid("3일", "-")
    keys = [c["key"] for c in g["grid"]["columns"]]
    assert keys[:4] == ["ifid", "cause", "mail_detail", "verdict"]
    assert g["grid"]["rows"] and all(row["ifid"] == "IF_ORD_RECV" for row in g["grid"]["rows"])
    assert "원인 컬럼 1개" in g["subtitle"]
    # 무슨 문제였는지가 표 머리와 행 모두에 보인다.
    assert g["subtitle"].startswith("원인: IF_ORD_RECV IF_ORDER_TMP.ORD_NO 길이 초과(실제 25 > 최대 20)")
    row = g["grid"]["rows"][0]
    assert row["cause"] == "IF_ORDER_TMP.ORD_NO 길이 초과(실제 25 > 최대 20)"
    assert row["mail_detail"].startswith("ORA-12899: value too large")
    assert g["grid"]["source"]["word_key"] == "src_word" and row["src_word"] == "ORD_NO"


def _run(coro: Any) -> Any:
    return asyncio.run(coro)


async def _with_client(fn: Any) -> Any:
    async with Client(build_server()) as c:
        return await fn(c)


def test_check_interface_errors_tells_the_whole_story():
    async def call(c: Client) -> Any:
        tools = {t.name: t for t in (await c.list_tools()).tools}
        res = await c.call_tool("check_interface_errors", {"period": "3일"})
        grid = await c.read_resource("iferr://impact/3%EC%9D%BC/-/-")
        return tools, res, grid

    tools, res, grid = _run(_with_client(call))
    meta = tools["check_interface_errors"].meta
    assert meta["view"] == "grid" and meta["detail_uri"] == "iferr://impact/{period}/{key}/{subject}"
    text = res.content[0].text
    assert "최근 3일(72시간) 메일 2통에서 인터페이스 오류 2건" in text
    assert "마스터: 파일" in text
    assert "- 원인: **IF_ORDER_TMP.ORD_NO 길이 초과(실제 25 > 최대 20)** — 테이블은 오류 메시지에서" in text
    assert "- 메일 오류 내용: `ORA-12899: value too large" in text
    assert "- 영향도: 영향 있음 **" in text and "IF_ORDER_TMP.ORD_NO 이 나온 줄" in text
    assert "테이블만 쓰는 문장" in text and "[표로 보기]" in text
    # 화면 마크다운이 그대로 그릴 수 있는 모양: 케이스마다 굵은 머리 줄, 영향 있는 곳은 하위 목록.
    # 들여쓴 '원인:'·'· 위치' 줄은 화면이 첫 항목에 이어 붙여 무너졌다.
    lines = text.splitlines()
    assert "**IF_ORD_RECV** · 확인됨" in text and "**IF_STK_RECV** · 확인됨" in text
    # 원인 컬럼이 없는 케이스(timeout)는 빈 칸이 아니라 그 사실을 적는다.
    assert "- 원인 컬럼: 메일에 적힌 컬럼 오류가 없다" in text
    assert any(l.startswith("  - `[ERP] ord_join.sql:5` 조회 — `SELECT") for l in lines)
    assert not any(l.startswith("  원인") or l.lstrip().startswith("·") for l in lines)
    data = json.loads(grid.contents[0].text)
    assert data["title"] == "인터페이스 오류 영향도 — 최근 3일"
    assert data["grid"]["rows"]


NORMAL_MAIL = eml(
    "주간 연계 처리 결과 보고", "Wed, 14 Oct 2026 06:00:00 -0000",
    "이번 주 연계는 모두 정상 처리되었습니다. IF_ID : IF_ORD_SEND\n")
OLD_SIZE_MAIL = eml(
    "[연계오류] 주문 수신 인터페이스 처리 실패", "Mon, 12 Oct 2026 08:00:00 -0000",
    "IF_ID : IF_ORD_RECV\n"
    'ORA-12899: value too large for column "ERP"."IF_ORDER_TMP"."ORD_NO" (actual: 22, maximum: 20)\n')


def test_subject_picks_one_mail_ignoring_spaces_and_case():
    """'[연계오류]주문 수신' 처럼 띄어쓰기가 달라도 그 메일 한 통만 본다."""
    r = incident.run_incident(subject="[연계오류]주문 수신")
    assert r["mail_count"] == 1 and [c["key"] for c in r["cases"]] == ["IF_ORD_RECV"]
    assert r["picked_mail"]["subject"] == "[연계오류] 주문 수신 인터페이스 처리 실패"
    assert r["cases"][0]["impacts"][0]["column"] == "ORD_NO"


def test_subject_looks_back_a_week_and_takes_the_latest(demo: Path):
    """지정한 메일은 며칠 전 것일 수 있다. 같은 제목이 여럿이면 가장 최근 것을 보고 알린다."""
    (demo / "03.eml").write_bytes(OLD_SIZE_MAIL)
    r = incident.run_incident(subject="주문 수신 인터페이스")
    assert r["label"] == "최근 7일" and r["hours"] == 168
    assert r["mail_count"] == 1
    assert "실제 25" in r["cases"][0]["impacts"][0]["issue_text"]
    assert any("2통이라 가장 최근 1통" in w for w in r["warnings"])


def test_subject_not_found_is_not_no_error():
    r = incident.run_incident(subject="없는 제목")
    assert r["cases"] == [] and r["picked_mail"] is None
    assert any("찾지 못했다" in w and "확인 필요" in w for w in r["warnings"])


def test_subject_mail_is_investigated_even_without_error_keyword(demo: Path):
    """사람이 '이 메일 오류 확인해줘' 했으면 키워드가 없어도 조사하되, 그 사실을 남긴다."""
    (demo / "03.eml").write_bytes(NORMAL_MAIL)
    r = incident.run_incident(subject="주간 연계")
    assert [c["key"] for c in r["cases"]] == ["IF_ORD_SEND"]
    assert r["cases"][0]["judged_by"] == "user"
    assert any("오류 메일이 아니었다" in w for w in r["warnings"])


def test_subject_with_key_keeps_only_that_key(monkeypatch: Any):
    calls: dict[str, Any] = {}
    real = incident.run_iferr

    def spy(**k: Any) -> Any:
        calls.update(k)
        return real(**k)

    monkeypatch.setattr(incident, "run_iferr", spy)
    r = incident.run_incident(subject="재고 수신", key="IF_STK_RECV")
    assert calls["subject"] == "재고 수신" and [c["key"] for c in r["cases"]] == ["IF_STK_RECV"]


def test_check_interface_errors_by_subject():
    async def call(c: Client) -> Any:
        res = await c.call_tool("check_interface_errors", {"subject": "[연계오류] 주문 수신"})
        grid = await c.read_resource(
            "iferr://impact/-/-/" + quote("[연계오류] 주문 수신", safe=""))
        miss = await c.call_tool("check_interface_errors", {"subject": "없는 제목"})
        return res, grid, miss

    res, grid, miss = _run(_with_client(call))
    text = res.content[0].text
    assert text.startswith("메일 '[연계오류] 주문 수신 인터페이스 처리 실패' (2026-10-14 08:00)에서 "
                           "인터페이스 오류 1건")
    assert "원인: **IF_ORDER_TMP.ORD_NO" in text
    data = json.loads(grid.contents[0].text)
    assert data["title"].startswith("인터페이스 오류 영향도 — 메일 '[연계오류] 주문 수신")
    assert data["grid"]["rows"]
    m = miss.content[0].text
    assert m.startswith("제목에 '없는 제목' 이(가) 들어간 메일을 최근 7일(168시간) 안에서 찾지 못했다")


def test_empty_model_reply_falls_back_to_tool_result():
    """qwen 이 사고 과정만 쓰고 빈 답을 낸 적이 있다. 조사 결과가 사라지면 안 된다."""
    from types import SimpleNamespace as M

    from app.app import _reply_of

    old = [M(type="human", content="이전 질문"), M(type="ai", content="이전 답")]
    turn = [M(type="human", content="오늘 오류"), M(type="ai", content=""),
            M(type="tool", content="오늘(5시간) 메일 2통 …"), M(type="ai", content="")]
    reply = _reply_of(old + turn)
    assert "오늘(5시간) 메일 2통" in reply and "이전 답" not in reply
    assert _reply_of(old + [M(type="human", content="q"), M(type="ai", content="답")]) == "답"


def test_direct_answer_tool_result_is_the_reply():
    """answer="tool" 툴은 결과가 곧 답이다 — 앞 단계 툴 결과·이전 답을 섞지 않고, 안내 문구도 안 붙인다."""
    from types import SimpleNamespace as M

    from app.app import _reply_of

    turn = [M(type="human", content="오늘 오류"), M(type="ai", content=""),
            M(type="tool", content="앞 단계 결과"), M(type="ai", content=""),
            M(type="tool", content="오늘(5시간) 메일 2통 …")]
    assert _reply_of([M(type="ai", content="이전 답")] + turn) == "오늘(5시간) 메일 2통 …"


def test_check_interface_errors_answers_directly():
    """모델이 다시 쓰면 숫자를 섞고 항목을 뺐다(실측). 툴 결과를 그대로 답으로 쓴다."""
    from app.mcp_bridge import _spec_of

    async def call(c: Client) -> Any:
        return {t.name: t for t in (await c.list_tools()).tools}

    tools = _run(_with_client(call))
    assert _spec_of(tools["check_interface_errors"])["answer"] == "tool"
    assert _spec_of(tools["analyze_column_impact"])["answer"] == "model"
