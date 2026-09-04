"""MCP 클라이언트 브리지 — MCP 툴을 LangChain 툴로 바꿔 준다.

MCP 스펙 리비전 2026-07-28 / Python SDK mcp>=2.1 기준.

사용법:
    from app.mcp_bridge import BRIDGE
    BRIDGE.start()
    tools = BRIDGE.tools()          # LangChain BaseTool 목록
    BRIDGE.run(agent.ainvoke(...))  # 브리지 루프에서 코루틴 실행

langchain-mcp-adapters 를 쓰지 않는 이유:
    그 패키지는 mcp<2.0 을 핀하고 있어 2026-07-28 리비전을 못 쓴다.
    변환 자체는 tools/list 의 input_schema 를 StructuredTool 에
    그대로 넘기는 정도라 직접 만드는 편이 의존성도 줄고 명확하다.

왜 전용 스레드에서 이벤트 루프를 도는가:
    Flask 라우트는 동기이고 MCP 클라이언트는 비동기다. 요청마다
    asyncio.run() 을 하면 그때마다 stdio 서버 프로세스가 새로 뜬다.
    루프 하나를 전용 스레드에 띄워 두고 클라이언트를 그 위에서만
    쓰면 프로세스는 하나로 유지된다. (클라이언트 세션은 자신이 만들어진
    루프에 묶여 있어 다른 스레드에서 그대로 쓰면 깨진다.)
"""

from __future__ import annotations

import asyncio
import contextvars
import json
import os
import sys
import threading
import time
from contextlib import AsyncExitStack
from pathlib import Path
from typing import Any
from urllib.parse import quote

from langchain_core.tools import StructuredTool, ToolException
from mcp.client.client import Client
from mcp.client.stdio import StdioServerParameters
from mcp.types import ElicitResult, ErrorData

import config

_ROOT = Path(__file__).resolve().parents[1]

# 이 프로세스가 시작된 시각. 재시작을 화면이 알아채는 표식이다.
_BOOT_ID = str(int(time.time()))


def _server_spec() -> Any:
    """설정에 따라 붙을 대상을 만든다.

    stdio  : 우리가 서버 프로세스를 직접 띄운다.
    http   : 이미 떠 있는 서버 URL 에 붙는다. 2026-07-28 코어가
             stateless 라 세션 고정 없이 아무 인스턴스로 보내도 된다.
    """
    if config.MCP_TRANSPORT == "stdio":
        return StdioServerParameters(
            command=sys.executable,
            args=["-m", "mcp_server"],
            cwd=str(_ROOT),
            # env 를 명시하지 않으면 SDK 가 PATH 등 최소한의 변수만 물려준다.
            # 그러면 자식(MCP 서버)이 USE_LLM, OLLAMA_HOST, SOURCE_ROOTS 를
            # 못 받아 config.py 의 기본값으로 돌아간다 — 앱과 툴이 서로 다른
            # 설정을 보는 바로 그 사고다. 실제로 USE_LLM=false 로 띄웠는데
            # 서버 쪽만 Ollama 를 호출했다. 환경을 통째로 물려준다.
            env=dict(os.environ),
        )
    return config.MCP_SERVER_URL


# 지금 처리 중인 요청의 진행 상황 수신처.
# 툴 코루틴은 여러 요청이 공유하므로, 어느 요청의 진행인지 인자로 넘길 수가
# 없다. contextvar 는 요청마다 값이 따로 잡히고 await 를 건너 전파되므로
# "지금 이 요청" 을 그대로 따라간다.
PROGRESS_SINK: contextvars.ContextVar[Any] = contextvars.ContextVar(
    "mcp_progress_sink", default=None
)


def emit_progress(kind: str, text: str, **extra: Any) -> None:
    """진행 상황을 현재 요청의 수신처로 보낸다. 수신처가 없으면 조용히 버린다.

    조용히 버리는 것이 맞는 경우다 — CLI 나 테스트에서는 받을 곳이 없고,
    진행 표시가 없다고 작업이 실패해서는 안 된다.
    """
    sink = PROGRESS_SINK.get()
    if sink is not None:
        sink({"type": kind, "text": text, **extra})


class MCPBridge:
    """MCP 서버 하나에 붙어 있는 클라이언트. 프로세스당 하나면 충분하다."""

    def __init__(self) -> None:
        self._loop: asyncio.AbstractEventLoop | None = None
        self._thread: threading.Thread | None = None
        self._stack: AsyncExitStack | None = None
        self._client: Client | None = None
        self._tools: list[StructuredTool] = []
        self._specs: list[dict[str, Any]] = []
        self._lock = threading.Lock()
        # 사람 확인이 필요해 거절한 요청들. 화면에 "확인 필요"로 띄운다.
        self.pending_confirmations: list[dict[str, Any]] = []

    # ---------------------------------------------------------------- 수명
    def start(self, timeout: float = 30.0) -> None:
        """루프 스레드를 띄우고 서버에 붙는다. 두 번 불러도 안전하다."""
        with self._lock:
            if self._loop is not None:
                return
            loop = asyncio.new_event_loop()
            self._loop = loop
            self._thread = threading.Thread(
                target=loop.run_forever, name="mcp-bridge", daemon=True
            )
            self._thread.start()
        asyncio.run_coroutine_threadsafe(self._connect(), self._loop).result(timeout)

    def submit(self, coro: Any) -> Any:
        """코루틴을 브리지 루프에 던져 두고 기다리지 않는다.

        스트리밍 응답용이다. 결과는 코루틴이 큐로 흘려보낸다.
        """
        if self._loop is None:
            raise RuntimeError("BRIDGE.start() 를 먼저 호출할 것")
        return asyncio.run_coroutine_threadsafe(coro, self._loop)

    def run(self, coro: Any, timeout: float | None = None) -> Any:
        """브리지 루프에서 코루틴을 돌리고 결과를 기다린다(동기 호출용)."""
        if self._loop is None:
            raise RuntimeError("BRIDGE.start() 를 먼저 호출할 것")
        return asyncio.run_coroutine_threadsafe(coro, self._loop).result(timeout)

    async def _connect(self) -> None:
        self._stack = AsyncExitStack()
        self._client = await self._stack.enter_async_context(
            Client(
                _server_spec(),
                read_timeout_seconds=config.MCP_TOOL_TIMEOUT_SEC,
                # MRTR: 서버가 resultType="input_required" 로 되물으면
                # 클라이언트가 답을 채워 재요청한다. 상한을 둬 무한 왕복을 막는다.
                input_required_max_rounds=config.MCP_INPUT_REQUIRED_MAX_ROUNDS,
                elicitation_callback=self._decline_elicitation,
            )
        )
        listed = await self._client.list_tools()
        self._specs = [_spec_of(t) for t in listed.tools]
        # 챗봇에 넘길 툴은 등급으로 거른다. 서버는 단계별 툴도 노출하지만
        # 로컬 소형 모델에 열 개를 보여 주면 순서대로 부르지 못한다.
        # 숨기는 책임은 클라이언트에 있다 — 다른 MCP 호스트는 전부 봐야 한다.
        allowed = set(config.CHAT_TOOL_TIERS)
        self._tools = [
            self._to_langchain(t)
            for t in listed.tools
            if _spec_of(t)["tier"] in allowed
        ]

    async def _decline_elicitation(self, context: Any, params: Any) -> Any:
        """서버가 사람에게 무언가를 물으면 기본적으로 거절한다.

        자동으로 "예"를 채워 보내면 파일 삭제·메일 발송 같은 동작이
        사람 확인 없이 실행된다(안전 규칙: 외부로 나가는 동작 자동 실행 금지).
        요청 내용은 남겨 두었다가 화면에 "확인 필요"로 띄우고,
        사람이 명시적으로 다시 실행하게 한다.
        """
        try:
            self.pending_confirmations.append(
                {"message": getattr(params, "message", ""), "declined": True}
            )
            return ElicitResult(action="decline")
        except Exception as e:  # 콜백이 터지면 툴 호출 전체가 죽는다
            return ErrorData(code=-32603, message=f"elicitation 처리 실패: {e}")

    # ---------------------------------------------------------------- 조회
    def tools(self) -> list[StructuredTool]:
        return list(self._tools)

    def specs(self) -> list[dict[str, Any]]:
        """프론트로 내려보낼 메타데이터. tools/list 의 _meta 에서 나온다."""
        return [dict(s) for s in self._specs]

    def hints(self) -> str:
        """등록된 툴들의 hint 를 합친다. 시스템 프롬프트에 붙인다.

        파괴적 툴에는 확인 문구를 자동으로 덧붙인다. 툴을 만든 사람이
        이 문구를 깜빡해도 안전 규칙이 프롬프트에서 빠지지 않도록.
        """
        allowed = set(config.CHAT_TOOL_TIERS)
        lines: list[str] = []
        for s in self._specs:
            if s.get("tier") not in allowed:
                continue          # 챗봇에 없는 툴의 지침을 프롬프트에 넣지 않는다
            note = s.get("hint", "")
            if s.get("destructive"):
                note = (note + " 이 툴은 되돌리기 어려운 동작이다. "
                        "실행하지 말고 무엇을 실행하면 되는지 사용자에게 보여만 줘라.").strip()
            if note:
                lines.append(f"- {s['name']}: {note}")
        return "\n".join(lines)

    def protocol_info(self) -> dict[str, Any]:
        """어느 리비전으로 붙었는지. 화면과 기동 로그에 보여준다."""
        if self._client is None:
            return {"connected": False}
        info = self._client.server_info
        return {
            "connected": True,
            # 화면이 서버 재시작을 알아채는 데 쓴다. 재시작되면 모델은
            # 이전 대화를 기억하지 못하는데(체크포인터가 프로세스 메모리다)
            # 화면에는 남아 있어 사용자가 오해한다.
            "boot": _BOOT_ID,
            "protocol": self._client.protocol_version,
            "server": getattr(info, "name", ""),
            "version": getattr(info, "version", ""),
            "transport": config.MCP_TRANSPORT,
        }

    def read_resource(self, uri: str) -> str:
        """MCP 리소스를 읽는다. 화면용 전체 데이터 통로(LLM 컨텍스트 밖)."""
        if self._client is None:
            raise RuntimeError("MCP 클라이언트가 연결되어 있지 않다")
        result = self.run(
            self._client.read_resource(uri), timeout=config.MCP_TOOL_TIMEOUT_SEC
        )
        parts = [getattr(c, "text", "") for c in result.contents]
        return "\n".join(p for p in parts if p)

    # ---------------------------------------------------------------- 변환
    def _to_langchain(self, mcp_tool: Any) -> StructuredTool:
        name = mcp_tool.name
        client = self._client

        label = _spec_of(mcp_tool)["label"]

        async def _call(**kwargs: Any) -> str:
            assert client is not None
            # 수신처를 '지금' 붙잡아 클로저로 넘긴다.
            # 진행 콜백은 MCP 세션의 수신 태스크에서 불리는데, 그 태스크는
            # 세션을 열 때 만들어져서 이 요청의 contextvar 를 보지 못한다.
            # 콜백 안에서 PROGRESS_SINK.get() 을 하면 항상 None 이 나온다.
            sink = PROGRESS_SINK.get()

            async def on_progress(
                progress: float, total: float | None, message: str | None
            ) -> None:
                """MCP 서버가 보낸 진행 알림을 화면 쪽으로 넘긴다."""
                if sink is None:
                    return
                text = message or f"{label} 진행 중"
                if total:
                    text = f"{text} ({int(progress)}/{int(total)})"
                sink({"type": "progress", "text": text, "tool": name})

            emit_progress("tool_start", f"{label} 실행 중…", tool=name, args=kwargs)
            try:
                result = await client.call_tool(
                    name,
                    kwargs,
                    read_timeout_seconds=config.MCP_TOOL_TIMEOUT_SEC,
                    progress_callback=on_progress,
                )
            except Exception as e:
                # 시간 초과는 원인이 대개 정해져 있다. 그대로 던지면
                # 'timed out' 한 줄뿐이라 무엇을 줄여야 할지 알 수 없다.
                hint = ""
                if "timed out" in str(e).lower():
                    hint = (
                        f" (상한 {config.MCP_TOOL_TIMEOUT_SEC}초. 로컬 모델은 판정 "
                        "하나에 수십 초가 걸린다 — IMPACT_MAX_STATEMENTS 를 줄이거나 "
                        "MCP_TOOL_TIMEOUT_SEC 를 늘릴 것)"
                    )
                emit_progress("tool_end", f"{label} 실패", tool=name)
                raise ToolException(f"[{name}] 툴 실행 실패: {e}{hint}") from e
            emit_progress("tool_end", f"{label} 완료", tool=name)
            text = "\n".join(
                getattr(c, "text", "") for c in result.content if getattr(c, "text", "")
            )
            if result.is_error:
                # 실패를 조용히 삼키지 않는다. LangGraph 가 ToolMessage 로
                # 모델에 전달하고, 모델이 사용자에게 알리도록 한다.
                raise ToolException(f"[{name}] 툴 실행 실패: {text}")
            if not text and result.structured_content is not None:
                text = json.dumps(result.structured_content, ensure_ascii=False)
            return text

        return StructuredTool(
            name=name,
            description=mcp_tool.description or "",
            # MCP 의 input_schema 는 JSON Schema 그대로다. LangChain 이
            # dict 형태 args_schema 를 받으므로 변환이 필요 없다.
            args_schema=mcp_tool.input_schema,
            coroutine=_call,
            metadata=_spec_of(mcp_tool),
        )


def fill_uri(template: str, **params: Any) -> str:
    """detail_uri 템플릿에 값을 채운다. 값은 반드시 퍼센트 인코딩한다.

    인코딩하지 않으면 값 안의 '?' 나 '/' 가 URI 문법으로 해석되어
    템플릿과 매칭되지 않는다. "왜 안 되지?" 같은 평범한 한국어 질문이
    'Unknown resource' 로 떨어진다 — 원인을 찾기 아주 어려운 증상이다.
    (서버 쪽은 SDK 가 알아서 디코딩하므로 별도 처리가 필요 없다.)

        fill_uri("echo://detail/{text}", text="왜 안 되지?")
        → "echo://detail/%EC%99%9C%20%EC%95%88%20%EB%90%98%EC%A7%80%3F"
    """
    out = template
    for key, value in params.items():
        out = out.replace("{" + key + "}", quote(str(value), safe=""))
    return out


def _spec_of(mcp_tool: Any) -> dict[str, Any]:
    """MCP Tool 에서 우리 화면이 쓰는 메타데이터를 뽑는다.

    label/view/hint/detail_uri 는 표준 필드가 아니라 _meta 에 실려 온다.
    """
    meta = mcp_tool.meta or {}
    ann = mcp_tool.annotations
    return {
        "name": mcp_tool.name,
        "label": meta.get("label") or mcp_tool.title or mcp_tool.name,
        "description": (mcp_tool.description or "").strip(),
        "view": meta.get("view", "text"),
        "detail_uri": meta.get("detail_uri", ""),
        "hint": meta.get("hint", ""),
        "examples": list(meta.get("examples") or []),
        "tier": meta.get("tier", "combo"),
        "in_chat": meta.get("tier", "combo") in set(config.CHAT_TOOL_TIERS),
        "read_only": bool(getattr(ann, "read_only_hint", False)) if ann else False,
        "destructive": bool(getattr(ann, "destructive_hint", False)) if ann else False,
    }


# 프로세스당 하나. app.py 가 import 해서 쓴다.
BRIDGE = MCPBridge()
