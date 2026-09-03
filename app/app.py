"""Flask 챗봇 — MCP 서버의 툴들을 하나의 대화로 묶는다.

MCP 스펙 리비전 2026-07-28. 툴은 전부 mcp_server/ 프로세스가 노출하고
이 앱은 MCP 클라이언트일 뿐이다. 그래서 이 파일은 어떤 툴이 있는지
알지 못한다 — tools/list 로 받아서 그대로 쓴다.

실행:
    python app/app.py                       # stdio 로 MCP 서버를 직접 띄운다
    USE_LLM=false python app/app.py         # Ollama 없이 기동만 확인

    # 서버를 따로 띄워 HTTP 로 붙는 경우
    MCP_TRANSPORT=streamable-http python -m mcp_server
    MCP_TRANSPORT=streamable-http python app/app.py

라우트:
    GET  /              채팅 UI
    GET  /api/tools     MCP tools/list 결과 + 프로토콜 정보
    GET  /api/resource  MCP resources/read 프록시 (화면용 전체 데이터)
    POST /api/chat      {"message": ..., "thread_id": ...}

새 툴을 붙일 때 이 파일은 수정하지 않는다. mcp_server/tools/ 에 파일만 놓으면 된다.
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

_HERE = Path(__file__).resolve().parent
_ROOT = _HERE.parent
# python app/app.py 로 실행하면 sys.path[0] 이 app/ 이 된다. 그러면 이름 app 이
# 패키지가 아니라 이 파일(app.py)로 해석되어 from app.mcp_bridge import ... 가
# 깨진다. 스크립트 디렉터리를 빼고 저장소 루트를 넣어야 한다.
sys.path[:] = [p for p in sys.path if Path(p or ".").resolve() != _HERE]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from flask import Flask, jsonify, render_template, request  # noqa: E402

import config  # noqa: E402
from app.mcp_bridge import BRIDGE, fill_uri  # noqa: E402

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
            tools=BRIDGE.tools(),
            system_prompt=BASE_SYSTEM_PROMPT.format(
                tool_hints=BRIDGE.hints() or "(없음)"
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
            key = (
                name,
                repr(sorted(args.items())) if isinstance(args, dict) else repr(args),
            )
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
    return jsonify({"mcp": BRIDGE.protocol_info(), "tools": BRIDGE.specs()})


@app.get("/api/resource")
def api_resource():
    """MCP 리소스 프록시.

    툴은 컨텍스트를 아끼려고 요약만 돌려준다. 화면에 뿌릴 전체 데이터는
    이 경로로 직접 읽어 간다 — LLM 컨텍스트를 거치지 않는다.
    툴마다 라우트를 새로 만들 필요가 없어 app.py 가 툴을 몰라도 된다.

    두 가지 형태를 받는다.
        /api/resource?template=echo://detail/{text}&text=왜 안 되지?   (권장)
        /api/resource?uri=echo%3A%2F%2Fdetail%2F%25EC%2599%259C...     (직접 조립)

    template 형태를 권장하는 이유:
        URI 값은 퍼센트 인코딩해야 하는데(값 안의 '?' 가 URI 문법으로
        해석된다), 그렇게 만든 URI 를 다시 쿼리 파라미터에 실으면
        '%3F' 의 '%' 가 또 인코딩되어야 한다. 이중 인코딩을 호출부가
        직접 하다 보면 반드시 틀린다. 템플릿과 값을 따로 받아 서버가
        조립하면 이 문제가 사라진다.
    """
    template = (request.args.get("template") or "").strip()
    if template:
        params = {k: v for k, v in request.args.items() if k != "template"}
        uri = fill_uri(template, **params)
    else:
        uri = (request.args.get("uri") or "").strip()
    if not uri:
        return jsonify({"error": "template 또는 uri 가 필요하다"}), 400
    try:
        return app.response_class(
            BRIDGE.read_resource(uri), mimetype="application/json"
        )
    except Exception as e:
        msg = f"{type(e).__name__}: {e}"
        if "Unknown resource" in str(e):
            # 거의 항상 인코딩 문제다. 값 안의 '?' 나 '/' 가 URI 문법으로
            # 해석되어 템플릿과 매칭되지 않는다. 증상만 보면 원인을 못 찾으므로
            # 여기서 알려 준다.
            msg += (
                " — URI 값은 퍼센트 인코딩해야 한다. "
                "template= 과 값들을 따로 넘기면 서버가 조립한다."
            )
        return jsonify({"error": msg}), 502


@app.post("/api/chat")
def api_chat():
    data = request.get_json(silent=True) or {}
    message = (data.get("message") or "").strip()
    thread_id = data.get("thread_id") or "default"
    if not message:
        return jsonify({"error": "message 가 비어 있다"}), 400

    try:
        # MCP 툴은 코루틴이라 동기 invoke 로는 못 돈다. 브리지 루프에서
        # ainvoke 를 돌린다(클라이언트 세션이 그 루프에 묶여 있기도 하다).
        result = BRIDGE.run(
            get_agent().ainvoke(
                {"messages": [{"role": "user", "content": message}]},
                config={"configurable": {"thread_id": thread_id}},
            )
        )
    except Exception as e:
        # LLM 이나 MCP 서버가 죽었다는 사실을 감추지 않는다. 화면에 그대로 보여준다.
        return jsonify({"error": f"{type(e).__name__}: {e}"}), 502

    messages = result.get("messages", [])
    reply = ""
    for m in reversed(messages):
        if getattr(m, "type", "") == "ai" and getattr(m, "content", ""):
            reply = m.content
            break

    # 서버가 사람 확인을 요구해 거절한 건이 있으면 결과에 실어 보낸다.
    pending = BRIDGE.pending_confirmations[:]
    BRIDGE.pending_confirmations.clear()

    return jsonify(
        {
            "reply": reply,
            "tool_calls": _turn_tool_calls(messages),
            "needs_confirmation": pending,
            "thread_id": thread_id,
        }
    )


def main() -> None:
    print(config.describe())

    # MCP 서버에 먼저 붙는다. 여기서 실패하면 툴이 하나도 없는 챗봇이 되므로
    # 조용히 넘기지 않고 원인을 그대로 보여준다.
    BRIDGE.start()
    info = BRIDGE.protocol_info()
    print(
        f"  MCP 연결   : {info.get('server')} v{info.get('version')} "
        f"(protocol {info.get('protocol')}, {info.get('transport')})"
    )
    names = [s["name"] for s in BRIDGE.specs()]
    print(f"  등록된 툴  : {', '.join(names) or '(없음)'}")

    if config.USE_LLM:
        # 기동을 막지는 않는다. 경고만 하고 뜬다 — 툴 목록 확인 등
        # LLM 없이 할 수 있는 일이 있다.
        from core.llm import check_ollama

        ok, msg = check_ollama(config.CHAT_MODEL)
        print(f"  Ollama     : {'OK' if ok else '경고'} — {msg}")
    else:
        print("  Ollama     : USE_LLM=false — LLM 없이 규칙만 동작")
    print("─" * 60)

    # 리로더는 프로세스를 두 번 띄운다. stdio 모드에서는 MCP 서버도 두 벌
    # 뜨고 하나가 고아가 되므로 끈다.
    app.run(
        host=config.HOST, port=config.PORT, debug=config.DEBUG, use_reloader=False
    )


if __name__ == "__main__":
    main()
