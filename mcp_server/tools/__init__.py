"""MCP 툴 레지스트리 — 이 폴더에 파일을 놓으면 자동 등록된다.

MCP 스펙 리비전 2026-07-28 기준. 툴 하나를 추가할 때 서버 코드도,
챗봇 코드도 수정하지 않는다. 파일 하나면 끝난다.

    # mcp_server/tools/mytool.py
    import json
    from agents.myagent import run_myagent
    from . import mcp, register

    @register(label="내 툴", view="text",
              detail_uri="myagent://detail/{arg}",
              hint="myagent_run 은 인자를 원문 그대로 넘겨라.",
              read_only=True)
    def myagent_run(arg: str) -> str:
        \"\"\"LLM 이 읽는 설명. 언제 이 툴을 쓰는지 여기에 쓴다.\"\"\"
        return str(run_myagent(arg, detail="summary"))

    @mcp.resource("myagent://detail/{arg}", mime_type="application/json")
    def myagent_detail(arg: str) -> str:
        return json.dumps(run_myagent(arg, detail="full"), ensure_ascii=False)

왜 메타데이터를 MCP `_meta` 와 annotations 에 싣는가:
    label/view/hint 는 우리 챗봇에만 필요한 정보라 프로토콜 표준 필드가
    아니다. 2026-07-28 리비전은 모든 메시지에 `_meta` 를 허용하므로
    여기에 실어 보내면 클라이언트가 tools/list 한 번으로 전부 받는다.
    별도 설정 파일을 두면 툴과 메타데이터가 따로 놀다가 어긋난다.

    readOnly/destructive 는 표준 annotations 다. 우리 챗봇뿐 아니라
    다른 MCP 호스트(IDE 등)도 이 힌트를 읽고 확인 절차를 넣는다.
    파괴적 동작에는 반드시 destructive=True 를 줄 것(안전 규칙).

툴 등급(tier) — 왜 필요한가:
    기능별로 툴을 잘게 나누면 다른 MCP 호스트에서 쓰기 좋다. 하지만
    로컬 소형 모델은 툴을 순서대로 여러 개 부르지 못한다(첫 툴만 부르고
    끝내거나 인자를 잃어버린다). 그래서 두 종류를 함께 둔다.

        tier="combo" : 한 번 호출로 끝나는 통합 툴. 챗봇에 노출한다
        tier="step"  : 단계별 툴. 서버는 노출하되 챗봇에는 숨긴다

    MCP 서버는 둘 다 내보낸다 — 숨기는 쪽은 클라이언트다
    (config.CHAT_TOOL_TIERS, app/mcp_bridge.py). 다른 호스트에서는
    단계별 툴을 그대로 쓸 수 있어야 하기 때문이다.
"""

from __future__ import annotations

import importlib
import pkgutil
from typing import Any, Callable, TypeVar

from mcp.server.mcpserver import MCPServer
from mcp.types import ToolAnnotations

INSTRUCTIONS = """사내 개발 지원 에이전트 모음.

각 툴은 요약(summary)만 돌려준다. 화면에 뿌릴 전체 데이터가 필요하면
툴 메타데이터의 detail_uri 가 가리키는 리소스를 읽어라.
'확인 필요'가 붙은 결과는 임의로 해석하지 말고 사용자에게 그대로 전달하라.
"""

# 서버 인스턴스. 툴 모듈들이 이걸 import 해서 리소스를 붙인다.
mcp = MCPServer(
    "ai-agent",
    title="사내 AI 에이전트",
    instructions=INSTRUCTIONS,
    version="0.1.0",
)

# 등록 순서를 유지한다. 화면에 뜨는 순서가 매번 바뀌면 혼란스럽다.
_REGISTRY: list[dict[str, Any]] = []
_LOADED = False

F = TypeVar("F", bound=Callable[..., Any])


def register(
    *,
    label: str = "",
    view: str = "text",
    detail_uri: str = "",
    hint: str = "",
    examples: tuple[str, ...] = (),
    read_only: bool = True,
    destructive: bool = False,
    idempotent: bool = True,
    tier: str = "combo",
    name: str | None = None,
) -> Callable[[F], F]:
    """MCP 툴로 등록하는 데코레이터.

    label       : 화면에 보일 이름 (MCP title 로도 나간다)
    view        : 프론트 렌더링 힌트 ("text" | "table" | "json")
    detail_uri  : 전체 데이터를 담은 MCP 리소스 URI 템플릿.
                  툴은 컨텍스트를 아끼려고 요약만 돌려주므로, 화면용
                  전체 데이터는 리소스로 따로 읽는다(LLM 컨텍스트 밖).
    hint        : 시스템 프롬프트에 자동으로 합쳐지는 툴별 지침
    examples    : 화면에 띄울 예시 질문. 사용자가 클릭하면 입력창에 들어간다.
                  hint 와 같은 이유로 툴 옆에 둔다 — 툴을 지우면 예시도 같이
                  사라져야 한다. 한곳에 모아 두면 없는 툴의 예시가 남는다.
    read_only   : 외부 상태를 바꾸지 않는가
    destructive : 파일 삭제·메일 발송·DML 처럼 되돌리기 어려운가.
                  True 면 클라이언트가 자동 실행을 막고 사람 확인을 받는다.
    tier        : "combo" 는 한 번 호출로 끝나는 통합 툴(챗봇에 노출),
                  "step" 은 단계별 툴(챗봇에는 숨기고 다른 MCP 호스트용).
                  소형 모델에 툴을 여럿 보여 주면 순서대로 부르지 못한다.
    """

    def deco(fn: F) -> F:
        tool_name = name or fn.__name__
        if tier not in ("combo", "step"):
            raise ValueError(f"tier 는 combo | step 이어야 한다: {tier}")
        if any(e["name"] == tool_name for e in _REGISTRY):
            # 같은 이름이 두 번 등록되면 LLM 이 어느 쪽을 부를지 알 수 없다.
            # 조용히 덮어쓰지 않고 즉시 실패시킨다.
            raise ValueError(f"툴 이름 중복: {tool_name}")

        _REGISTRY.append(
            {
                "name": tool_name,
                "label": label or tool_name,
                "view": view,
                "detail_uri": detail_uri,
                "hint": hint.strip(),
                "examples": tuple(examples),
                "destructive": destructive,
                "tier": tier,
            }
        )

        return mcp.tool(
            name=tool_name,
            title=label or None,
            annotations=ToolAnnotations(
                title=label or None,
                read_only_hint=read_only,
                destructive_hint=destructive,
                idempotent_hint=idempotent,
                # 폐쇄망이고 외부 세계를 건드리지 않는다.
                open_world_hint=False,
            ),
            meta={
                "label": label or tool_name,
                "view": view,
                "detail_uri": detail_uri,
                "hint": hint.strip(),
                "examples": list(examples),
                "tier": tier,
            },
        )(fn)

    return deco


def load_all() -> list[str]:
    """tools 패키지 안의 모든 모듈을 import 해 register() 를 실행시킨다.

    두 번 불러도 안전하다. 등록된 툴 이름 목록을 돌려준다.
    """
    global _LOADED
    if _LOADED:
        return [e["name"] for e in _REGISTRY]

    for mod in pkgutil.iter_modules(__path__):
        if mod.name.startswith("_"):
            continue
        importlib.import_module(f"{__name__}.{mod.name}")
    _LOADED = True
    return [e["name"] for e in _REGISTRY]


def build_server() -> MCPServer:
    """툴을 모두 적재한 서버 인스턴스를 돌려준다.

    테스트는 이 함수로 서버를 만들어 인메모리 클라이언트로 붙는다
    (프로세스를 띄우지 않아도 프로토콜 왕복을 그대로 검증할 수 있다).
    """
    load_all()
    return mcp


def specs() -> list[dict[str, Any]]:
    """서버 쪽 등록 현황. 기동 로그용이다.

    챗봇은 이걸 쓰지 않는다 — MCP 로 tools/list 해서 받는다.
    서버 내부 자료구조를 클라이언트가 직접 들여다보면 프로세스를
    분리한 의미가 없어진다.
    """
    return [dict(e) for e in _REGISTRY]
