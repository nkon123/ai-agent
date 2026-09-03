"""Flask 챗봇 — 등록된 에이전트 툴들을 하나의 대화로 묶는다.

실행:
    python app/app.py
    USE_LLM=false python app/app.py     # Ollama 없이 기동만 확인

라우트:
    GET  /            채팅 UI
    GET  /api/tools   등록된 툴 메타데이터
    POST /api/chat    {"message": ..., "thread_id": ...}
    + 각 툴이 제공한 Blueprint (예: /api/echo)

새 툴을 붙일 때 이 파일은 수정하지 않는다. app/tools/ 에 파일만 놓으면 된다.
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

# 저장소 루트를 import 경로에 넣는다. 패키지로 설치하지 않는 배포 방식이라
# python app/app.py 로 바로 실행할 수 있어야 한다.
_HERE = Path(__file__).resolve().parent
_ROOT = _HERE.parent
# python app/app.py 로 실행하면 sys.path[0] 이 app/ 이 된다. 그러면 이름 app 이
# 패키지가 아니라 이 파일(app.py)로 해석되어 from app.tools import ... 가 깨진다.
# 스크립트 디렉터리를 빼고 저장소 루트를 넣어야 한다.
sys.path[:] = [p for p in sys.path if Path(p or ".").resolve() != _HERE]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from flask import Flask, jsonify, render_template, request  # noqa: E402

import config  # noqa: E402
from app.tools import (  # noqa: E402
    all_tools,
    blueprints,
    hints,
    load_all,
    specs,
)

BASE_SYSTEM_PROMPT = """너는 사내 개발자를 돕는 한국어 어시스턴트다.

규칙:
- 질문에 맞는 툴이 있으면 반드시 툴을 사용하고, 결과를 근거로 답하라.
- 툴 결과에 '확인 필요'가 있으면 그 내용을 반드시 사용자에게 전달하라.
  임의로 판단해 넘기지 마라.
- 툴이 없거나 결과가 비면 모른다고 말하라. 지어내지 마라.
- 파일 삭제, 메일 발송, DB 변경 같은 동작은 직접 실행하지 말고
  무엇을 실행하면 되는지 보여만 줘라.

툴별 지침:
{tool_hints}
"""

app = Flask(__name__)

# 툴 로딩은 import 시점에 한 번. 어떤 툴이 떴는지 로그로 남긴다.
_LOADED_TOOLS = load_all()
for _bp in blueprints():
    app.register_blueprint(_bp)

# 챗 에이전트는 첫 요청 때 만든다(지연 생성).
# 기동 시점에 만들면 Ollama 가 꺼져 있을 때 서버 자체가 안 뜬다.
# /api/tools 같은 라우트는 LLM 없이도 살아 있어야 한다.
_agent: Any = None


def get_agent() -> Any:
    global _agent
    if _agent is None:
        from langchain.agents import create_agent
        from langgraph.checkpoint.memory import InMemorySaver

        from core.llm import get_llm

        _agent = create_agent(
            model=get_llm(model=config.CHAT_MODEL),
            tools=all_tools(),
            system_prompt=BASE_SYSTEM_PROMPT.format(
                tool_hints=hints() or "(없음)"
            ),
            # 체크포인터가 대화 이력을 보관한다. thread_id 별로 분리된다.
            checkpointer=InMemorySaver(),
        )
    return _agent


def _turn_tool_calls(messages: list[Any]) -> list[dict[str, Any]]:
    """이번 턴에 호출된 툴만 추린다.

    체크포인터가 전체 대화 이력을 들고 있으므로 messages 를 통째로
    훑으면 이전 턴의 툴 호출까지 딸려와 화면에 중복 표시된다.
    마지막 사용자 메시지 이후만 본다.
    """
    last_human = -1
    for i, m in enumerate(messages):
        if getattr(m, "type", "") == "human":
            last_human = i

    calls: list[dict[str, Any]] = []
    seen: set[tuple[str, str]] = set()
    for m in messages[last_human + 1 :]:
        for c in getattr(m, "tool_calls", None) or []:
            name = c.get("name", "")
            args = c.get("args", {})
            # 같은 인자로 같은 툴을 두 번 부르는 경우(모델이 재시도)도
            # 화면에는 한 번만 보여준다.
            key = (name, repr(sorted(args.items())) if isinstance(args, dict) else repr(args))
            if key in seen:
                continue
            seen.add(key)
            calls.append({"name": name, "args": args})
    return calls


@app.get("/")
def index():
    return render_template("index.html")


@app.get("/api/tools")
def api_tools():
    return jsonify({"tools": specs()})


@app.post("/api/chat")
def api_chat():
    data = request.get_json(silent=True) or {}
    message = (data.get("message") or "").strip()
    thread_id = data.get("thread_id") or "default"
    if not message:
        return jsonify({"error": "message 가 비어 있다"}), 400

    try:
        result = get_agent().invoke(
            {"messages": [{"role": "user", "content": message}]},
            config={"configurable": {"thread_id": thread_id}},
        )
    except Exception as e:
        # LLM 이 죽었다는 사실을 감추지 않는다. 화면에 그대로 보여준다.
        return jsonify({"error": f"{type(e).__name__}: {e}"}), 502

    messages = result.get("messages", [])
    reply = ""
    for m in reversed(messages):
        if getattr(m, "type", "") == "ai" and getattr(m, "content", ""):
            reply = m.content
            break

    return jsonify(
        {
            "reply": reply,
            "tool_calls": _turn_tool_calls(messages),
            "thread_id": thread_id,
        }
    )


def main() -> None:
    print(config.describe())
    print(f"  등록된 툴  : {', '.join(_LOADED_TOOLS) or '(없음)'}")

    if config.USE_LLM:
        # 기동을 막지는 않는다. 경고만 하고 뜬다 — 툴 목록 확인 등
        # LLM 없이 할 수 있는 일이 있다.
        from core.llm import check_ollama

        ok, msg = check_ollama(config.CHAT_MODEL)
        print(f"  Ollama     : {'OK' if ok else '경고'} — {msg}")
    else:
        print("  Ollama     : USE_LLM=false — LLM 없이 규칙만 동작")
    print("─" * 60)

    app.run(host=config.HOST, port=config.PORT, debug=config.DEBUG)


if __name__ == "__main__":
    main()
