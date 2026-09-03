"""툴 레지스트리 — app/tools/ 에 파일을 놓으면 자동 등록된다.

새 툴을 붙일 때 app.py 는 건드리지 않는다. 파일 하나 추가로 끝난다.

    # app/tools/mytool.py
    from langchain_core.tools import tool
    from . import register

    @tool
    def my_tool(arg: str) -> str:
        \"\"\"LLM 이 읽는 설명. 언제 이 툴을 쓰는지 여기에 쓴다.\"\"\"
        return str(run_mine(arg, detail="summary"))

    register(my_tool, label="내 툴", view="text",
             detail_endpoint="/api/mine",
             hint="my_tool 은 인자를 원문 그대로 넘겨라.")

제공 함수:
    register(tool_obj, label, view, detail_endpoint, hint, blueprint)
    load_all()   tools 패키지 안의 모든 모듈 import
    all_tools()  @tool 객체 리스트
    specs()      프론트용 메타데이터 (tool 객체 제외 — JSON 직렬화 불가)
    hints()      시스템 프롬프트에 붙일 툴별 지침
    blueprints() 툴이 제공한 Flask Blueprint 목록

hint 를 툴 옆에 두는 이유:
    툴별 주의사항을 app.py 의 시스템 프롬프트에 몰아넣으면, 툴이 열 개가
    될 때쯤 프롬프트가 아무도 못 고치는 덩어리가 된다. 툴을 지울 때
    지침만 남아 LLM 을 헷갈리게 하는 일도 흔하다. 같이 두면 같이 사라진다.
"""

from __future__ import annotations

import importlib
import pkgutil
from typing import Any

# 등록 순서를 유지한다. 화면에 뜨는 순서가 매번 바뀌면 혼란스럽다.
_REGISTRY: list[dict[str, Any]] = []
_LOADED = False


def register(
    tool_obj: Any,
    label: str = "",
    view: str = "text",
    detail_endpoint: str = "",
    hint: str = "",
    blueprint: Any = None,
) -> None:
    """툴 하나를 등록한다.

    label           : 화면에 보일 이름 (비면 툴 이름)
    view            : 프론트 렌더링 힌트 ("text" | "table" | "json")
    detail_endpoint : 전체 데이터를 가져갈 API 경로.
                      LLM 컨텍스트를 아끼려고 툴은 summary 만 돌려주므로,
                      화면용 전체 데이터는 이 경로로 따로 받는다.
    hint            : 시스템 프롬프트에 자동으로 합쳐지는 툴별 지침
    blueprint       : detail_endpoint 를 제공하는 Flask Blueprint.
                      app.py 를 수정하지 않고 라우트를 추가하기 위한 통로.
    """
    name = getattr(tool_obj, "name", None) or getattr(tool_obj, "__name__", "")
    if not name:
        raise ValueError("툴 이름을 알 수 없다. @tool 데코레이터를 확인할 것.")
    if any(e["name"] == name for e in _REGISTRY):
        # 같은 이름이 두 번 등록되면 LLM 이 어느 쪽을 부를지 알 수 없다.
        # 조용히 덮어쓰지 않고 즉시 실패시킨다.
        raise ValueError(f"툴 이름 중복: {name}")

    _REGISTRY.append(
        {
            "name": name,
            "label": label or name,
            "description": (getattr(tool_obj, "description", "") or "").strip(),
            "view": view,
            "detail_endpoint": detail_endpoint,
            "hint": hint.strip(),
            "tool": tool_obj,
            "blueprint": blueprint,
        }
    )


def load_all() -> list[str]:
    """tools 패키지 안의 모든 모듈을 import 해 register() 를 실행시킨다.

    두 번 불러도 안전하다(Flask 디버그 모드는 모듈을 재적재한다).
    등록된 툴 이름 목록을 돌려준다.
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


def all_tools() -> list[Any]:
    """create_agent 에 넘길 @tool 객체 목록."""
    return [e["tool"] for e in _REGISTRY]


def specs() -> list[dict[str, Any]]:
    """프론트로 내려보낼 메타데이터. tool/blueprint 객체는 뺀다(JSON 불가)."""
    return [
        {k: v for k, v in e.items() if k not in ("tool", "blueprint")}
        for e in _REGISTRY
    ]


def hints() -> str:
    """등록된 툴들의 hint 를 합친다. 시스템 프롬프트에 붙인다."""
    parts = [f"- {e['name']}: {e['hint']}" for e in _REGISTRY if e["hint"]]
    return "\n".join(parts)


def blueprints() -> list[Any]:
    """툴이 함께 제공한 Blueprint 목록. app.py 가 일괄 등록한다."""
    return [e["blueprint"] for e in _REGISTRY if e["blueprint"] is not None]
