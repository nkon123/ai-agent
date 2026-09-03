"""MCP 서버 회귀 테스트 (스펙 리비전 2026-07-28).

인메모리 클라이언트로 붙는다. 프로세스를 띄우지 않아도 프로토콜 왕복
(tools/list, tools/call, resources/read)을 그대로 검증할 수 있다.

실행:
    pytest -q
"""

from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import pytest  # noqa: E402
from mcp.client.client import Client  # noqa: E402
from mcp.types import LATEST_PROTOCOL_VERSION  # noqa: E402

import config  # noqa: E402
from mcp_server.tools import build_server, specs  # noqa: E402


def _run(coro: Any) -> Any:
    """테스트마다 새 루프. 세션이 루프에 묶이므로 재사용하지 않는다."""
    return asyncio.run(coro)


async def _with_client(fn: Any) -> Any:
    async with Client(build_server()) as c:
        return await fn(c)


def test_sdk_speaks_2026_07_28():
    """설정에 적힌 리비전과 SDK 가 협상하는 리비전이 같아야 한다.

    어긋나면 툴이 안 보이는 증상으로 나타나 원인을 찾기 어렵다.
    """
    assert LATEST_PROTOCOL_VERSION == config.MCP_PROTOCOL_VERSION

    async def check(c: Client) -> str:
        return c.protocol_version

    assert _run(_with_client(check)) == "2026-07-28"


def test_tools_list_carries_our_metadata():
    """label/view/hint/detail_uri 는 표준 필드가 아니라 _meta 로 실려 온다.

    별도 설정 파일을 두지 않고 이 경로로 보내야 툴과 메타데이터가
    따로 놀지 않는다.
    """

    async def check(c: Client) -> Any:
        return (await c.list_tools()).tools

    tools = _run(_with_client(check))
    by_name = {t.name: t for t in tools}
    assert "echo_classify" in by_name

    t = by_name["echo_classify"]
    assert t.meta["label"] == "문장 분류 (echo)"
    assert t.meta["detail_uri"] == "echo://detail/{text}"
    assert t.meta["hint"]
    # 읽기 전용 툴은 그렇게 표시되어야 한다. 다른 MCP 호스트도 이 힌트로
    # 확인 절차를 넣는다.
    assert t.annotations.read_only_hint is True
    assert t.annotations.destructive_hint is False
    # 입력 스키마가 있어야 클라이언트가 LangChain 툴로 변환할 수 있다.
    assert t.input_schema["properties"]["text"]["type"] == "string"


def test_tool_call_returns_summary_not_full():
    """툴 반환값은 LLM 컨텍스트에 그대로 들어간다. 요약이어야 한다."""

    async def check(c: Client) -> Any:
        return await c.call_tool("echo_classify", {"text": "파일 지워줘"})

    r = _run(_with_client(check))
    assert r.is_error is False
    text = r.content[0].text
    assert "분류=command" in text
    assert "rule/command-verb" in text
    # 근거 원문(evidence)까지 컨텍스트에 넣지 않는다 — 리소스로 따로 읽는다.
    assert "evidence" not in text


def test_resource_returns_full_detail():
    """화면용 전체 데이터는 리소스로 나간다(LLM 컨텍스트 밖)."""

    async def check(c: Client) -> Any:
        return await c.read_resource("echo://detail/파일 지워줘")

    r = _run(_with_client(check))
    data = json.loads(r.contents[0].text)
    assert data["kind"] == "command"
    assert data["decided_by"] == "rule"
    # 판정 근거가 결과와 함께 남아야 오판 원인을 추적할 수 있다.
    assert data["evidence"] == "지워"


def test_registry_rejects_duplicate_names():
    """같은 이름이 두 번 등록되면 LLM 이 어느 쪽을 부를지 알 수 없다."""
    from mcp_server.tools import register

    with pytest.raises(ValueError):

        @register(label="중복")
        def echo_classify(text: str) -> str:  # 이미 등록된 이름
            """중복"""
            return ""


def test_server_specs_match_registered_tools():
    """기동 로그에 찍히는 목록과 실제 노출 툴이 어긋나면 안 된다."""

    async def check(c: Client) -> Any:
        return [t.name for t in (await c.list_tools()).tools]

    assert sorted(s["name"] for s in specs()) == sorted(_run(_with_client(check)))


def test_stdio_child_inherits_our_env(monkeypatch: Any) -> None:
    """stdio 서버(자식 프로세스)가 부모와 같은 설정을 봐야 한다.

    env 를 넘기지 않으면 SDK 가 PATH 등 최소 변수만 물려주고, 자식은
    config.py 기본값으로 돌아간다. 실제로 USE_LLM=false 로 띄웠는데
    MCP 서버 쪽만 Ollama 를 호출했다 — 설정 이원화 사고 그대로다.
    """
    from app.mcp_bridge import _server_spec

    monkeypatch.setenv("USE_LLM", "false")
    monkeypatch.setenv("SOURCE_ROOT_ERP", r"C:\temp\new")
    monkeypatch.setattr(config, "MCP_TRANSPORT", "stdio")

    spec = _server_spec()
    assert spec.env is not None
    assert spec.env["USE_LLM"] == "false"
    assert spec.env["SOURCE_ROOT_ERP"] == r"C:\temp\new"


def test_http_transport_uses_url(monkeypatch: Any) -> None:
    """streamable-http 모드에서는 URL 로 붙는다.

    2026-07-28 코어는 stateless 라 세션 고정 없이 아무 인스턴스에
    보내도 된다 — 그래서 URL 하나면 충분하다.
    """
    from app.mcp_bridge import _server_spec

    monkeypatch.setattr(config, "MCP_TRANSPORT", "streamable-http")
    monkeypatch.setattr(config, "MCP_SERVER_URL", "http://10.0.0.5:8765/mcp")
    assert _server_spec() == "http://10.0.0.5:8765/mcp"


def test_detail_uri_must_be_percent_encoded() -> None:
    """URI 값에 '?' 가 들어가면 인코딩 없이는 템플릿과 매칭되지 않는다.

    "왜 안 되지?" 같은 평범한 한국어 질문이 'Unknown resource' 로 떨어진다.
    fill_uri 를 거치면 통과한다.
    """
    from app.mcp_bridge import fill_uri

    uri = fill_uri("echo://detail/{text}", text="왜 안 되지?")
    assert "?" not in uri and "%3F" in uri

    async def read(c: Client) -> Any:
        return await c.read_resource(uri)

    data = json.loads(_run(_with_client(read)).contents[0].text)
    # 서버 쪽은 SDK 가 디코딩해 주므로 원문이 그대로 들어간다.
    assert data["text"] == "왜 안 되지?"
    assert data["kind"] == "question"

    async def read_raw(c: Client) -> Any:
        return await c.read_resource("echo://detail/왜 안 되지?")

    with pytest.raises(Exception):
        _run(_with_client(read_raw))


def test_resource_proxy_builds_uri_from_template() -> None:
    """/api/resource 는 template + 값을 받아 서버가 조립한다.

    호출부가 URI 를 직접 만들면 퍼센트 인코딩을 두 번 해야 한다
    (URI 값에 한 번, 쿼리 파라미터에 한 번). 반드시 틀린다.
    """
    from app.mcp_bridge import fill_uri

    assert fill_uri("echo://detail/{text}", text="a/b?c") == (
        "echo://detail/a%2Fb%3Fc"
    )


def test_tiers_are_declared_on_every_tool() -> None:
    """모든 툴은 combo(통합) 또는 step(단계별) 중 하나여야 한다."""

    async def check(c: Client) -> Any:
        return (await c.list_tools()).tools

    tools = _run(_with_client(check))
    tiers = {t.name: t.meta.get("tier") for t in tools}
    assert all(v in ("combo", "step") for v in tiers.values()), tiers
    # 통합 툴과 단계별 툴이 둘 다 있어야 한다 — 그게 이 구조의 목적이다.
    assert "combo" in tiers.values() and "step" in tiers.values()
    assert tiers["check_interface_errors"] == "combo"
    assert tiers["lookup_interface"] == "step"


def test_registry_rejects_unknown_tier() -> None:
    from mcp_server.tools import register

    with pytest.raises(ValueError):

        @register(label="이상한 등급", tier="whatever")
        def weird_tool(x: str) -> str:
            """이상한"""
            return ""


def test_bridge_hides_step_tools_from_chat(monkeypatch: Any) -> None:
    """로컬 소형 모델에는 통합 툴만 보여 준다.

    툴을 여러 개 보여 주면 순서대로 부르지 못한다(첫 툴만 부르고 끝내거나
    인자를 잃어버린다). 숨기는 책임은 클라이언트에 있다 — MCP 서버는
    다른 호스트를 위해 전부 노출한다.
    """
    from app import mcp_bridge

    monkeypatch.setattr(config, "CHAT_TOOL_TIERS", ("combo",))
    bridge = mcp_bridge.MCPBridge()
    bridge._specs = [
        {"name": "combo_tool", "tier": "combo", "hint": "통합 지침", "destructive": False},
        {"name": "step_tool", "tier": "step", "hint": "단계 지침", "destructive": False},
    ]
    # 시스템 프롬프트에도 숨긴 툴의 지침이 들어가면 안 된다.
    # 없는 툴을 부르라고 시키는 꼴이 된다.
    hints = bridge.hints()
    assert "통합 지침" in hints and "단계 지침" not in hints


def test_bridge_can_open_all_tiers(monkeypatch: Any) -> None:
    """큰 모델로 바꾸면 CHAT_TOOL_TIERS 로 단계별 툴도 열 수 있다."""
    from app import mcp_bridge

    monkeypatch.setattr(config, "CHAT_TOOL_TIERS", ("combo", "step"))
    bridge = mcp_bridge.MCPBridge()
    bridge._specs = [
        {"name": "step_tool", "tier": "step", "hint": "단계 지침", "destructive": False},
    ]
    assert "단계 지침" in bridge.hints()
