"""echo 툴 — agents/echo 를 챗봇에 붙이는 얇은 껍데기.

이 파일에는 로직을 넣지 않는다. run_echo() 를 부르고 문자열로
돌려주는 것까지가 전부다. 판정 규칙을 여기에 슬쩍 추가하면
CLI 실행과 챗봇 실행의 결과가 달라진다.

에이전트당 툴은 하나만 노출한다. 소형 모델은 툴을 순서대로 여러 개
부르지 못한다(첫 툴만 부르고 끝내거나, 인자를 잃어버린다).
내부 단계가 필요하면 일반 함수로 두고 @tool 을 붙이지 않는다.
"""

from __future__ import annotations

from flask import Blueprint, jsonify, request
from langchain_core.tools import tool

from agents.echo import run_echo

from . import register

# detail_endpoint 용 라우트. 화면에 뿌릴 전체 데이터는 LLM 컨텍스트를
# 거치지 않고 여기로 직접 가져간다.
bp = Blueprint("echo", __name__)


@bp.get("/api/echo")
def echo_detail():
    text = request.args.get("text", "")
    return jsonify(run_echo(text, detail="full"))


@tool
def echo_classify(text: str) -> str:
    """문장을 질문/명령/평서로 분류하고 한마디 덧붙인다.

    사용자가 어떤 문장의 성격을 물을 때 사용한다.
    인자 text 에는 사용자가 말한 문장을 원문 그대로 넣는다.
    """
    # summary 를 쓰는 이유: 이 반환값은 LLM 컨텍스트에 그대로 들어간다.
    # full 을 돌려주면 근거·원문까지 매 턴 컨텍스트를 먹는다.
    r = run_echo(text, detail="summary")
    line = f"분류={r['kind']} (근거: {r['decided_by']}/{r['rule']})"
    if r.get("comment"):
        line += f"\n한마디: {r['comment']}"
    if r.get("warnings"):
        # 실패를 조용히 넘기지 않는다. LLM 이 사용자에게 전달하도록 남긴다.
        line += "\n확인 필요: " + "; ".join(r["warnings"])
    return line


register(
    echo_classify,
    label="문장 분류 (echo)",
    view="text",
    detail_endpoint="/api/echo",
    hint=(
        "echo_classify 는 문장 하나를 분류할 때만 쓴다. "
        "text 인자에는 사용자 문장을 요약하지 말고 원문 그대로 넣어라."
    ),
    blueprint=bp,
)
