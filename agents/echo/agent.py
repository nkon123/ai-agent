"""echo — 구조 확인용 샘플 에이전트.

새 에이전트를 만들 때 이 파일을 복사해 시작한다. 각 부분이 왜
이렇게 생겼는지 주석으로 남겨 두었으니 지우지 말고 고칠 것.

하는 일:
    입력 문자열을 규칙으로 분류(질문/명령/평서)하고, USE_LLM 이 켜져
    있으면 LLM 이 한마디 덧붙인다.

단독 실행:
    python agents/echo/agent.py "이거 왜 안 되지?"
    python agents/echo/agent.py --detail summary "파일 지워줘"

챗봇에서:
    app/tools/echo.py 가 run_echo() 를 @tool 로 감싼다.
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any, Literal, TypedDict

# 단독 실행(python agents/echo/agent.py) 시에도 저장소 루트의 config 를
# import 할 수 있어야 한다. 패키지로 설치하지 않는 배포 방식이라 필요하다.
_ROOT = Path(__file__).resolve().parents[2]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from langgraph.graph import END, START, StateGraph  # noqa: E402

from config import USE_LLM  # noqa: E402
from core.cache import cached  # noqa: E402

Detail = Literal["full", "summary", "minimal"]

# 규칙 기반 분류표. (규칙명, 판정, 매칭 토큰)
# 규칙에 이름을 붙여 두는 이유: 결과에 'decided_by' 와 'rule' 을 남겨야
# 오판이 어느 규칙에서 나왔는지 추적할 수 있다(규칙 4-6).
_COMMAND_WORDS = ("해줘", "하라", "지워", "삭제", "실행", "보내")
_QUESTION_WORDS = ("왜", "뭐", "무엇", "어떻게", "언제", "누가", "인가요")


class EchoState(TypedDict, total=False):
    """LangGraph 가 노드 사이로 넘기는 상태.

    노드는 상태를 통째로 갈아끼우지 않고 바뀐 키만 돌려준다.
    """

    text: str
    kind: str            # question | command | statement | unknown
    decided_by: str      # rule | llm | static | fallback
    rule: str
    evidence: str
    comment: str
    warnings: list[str]


# --------------------------------------------------------------------------
# 노드 1 — 규칙 판정
# --------------------------------------------------------------------------


def classify(state: EchoState) -> EchoState:
    """규칙만으로 분류한다. LLM 없이도 여기까지는 항상 동작해야 한다."""
    text = (state.get("text") or "").strip()
    if not text:
        # '모른다'와 '아니다'를 같은 이름으로 쓰지 않는다(규칙 4-7).
        # 빈 입력은 statement 가 아니라 unknown 이다.
        return {
            "kind": "unknown",
            "decided_by": "fallback",
            "rule": "empty-input",
            "evidence": "",
            "warnings": ["입력이 비어 있어 판정할 수 없다 — 확인 필요"],
        }

    if text.endswith("?") or any(w in text for w in _QUESTION_WORDS):
        hit = next((w for w in _QUESTION_WORDS if w in text), "?")
        return {
            "kind": "question",
            "decided_by": "rule",
            "rule": "question-marker",
            # evidence 는 '판단 근거가 된 실제 조각'이어야 한다.
            # 규칙명만 남기면 왜 그렇게 판정했는지 재현할 수 없다.
            "evidence": hit,
            "warnings": [],
        }

    for w in _COMMAND_WORDS:
        if w in text:
            return {
                "kind": "command",
                "decided_by": "rule",
                "rule": "command-verb",
                "evidence": w,
                "warnings": [],
            }

    return {
        "kind": "statement",
        "decided_by": "rule",
        "rule": "default-statement",
        "evidence": text[:40],
        "warnings": [],
    }


# --------------------------------------------------------------------------
# 노드 2 — LLM 한마디 (선택)
# --------------------------------------------------------------------------


def comment(state: EchoState) -> EchoState:
    """USE_LLM 이 False 면 아무것도 하지 않는다.

    LLM 호출 실패를 조용히 삼키지 않는다. 실패 사실을 warnings 에
    남겨 결과에 드러낸다(규칙 4-7). 누락은 오탐보다 나쁘다.
    """
    if not USE_LLM:
        return {"comment": "", "warnings": list(state.get("warnings") or [])}

    warnings = list(state.get("warnings") or [])
    try:
        # LLM import 는 여기서 한다. 모듈 최상단에서 하면 langchain_ollama 가
        # 없는 환경에서 규칙 전용 실행까지 같이 죽는다.
        from core.llm import get_llm

        llm = get_llm()
        prompt = (
            "다음 문장을 한 문장으로 짧게 되받아 말해라. 설명이나 사족은 붙이지 마라.\n"
            f"분류: {state.get('kind')}\n문장: {state.get('text')}"
        )
        text = llm.invoke(prompt).content
        return {"comment": str(text).strip(), "warnings": warnings}
    except Exception as e:
        warnings.append(f"LLM 호출 실패 — 확인 필요: {type(e).__name__}: {e}")
        return {"comment": "", "warnings": warnings}


# --------------------------------------------------------------------------
# 그래프 조립
# --------------------------------------------------------------------------


@cached(ttl=3600, maxsize=1, key=lambda: "graph")
def _graph():
    """컴파일된 그래프를 재사용한다.

    StateGraph.compile() 은 매 호출마다 다시 할 이유가 없고,
    루프를 돌 때는 이 비용이 눈에 띈다. '무엇이 비싼가'로 캐시 키를
    잡는 예시이기도 하다 — 인자가 없으므로 키는 상수 하나면 된다.
    """
    g = StateGraph(EchoState)
    g.add_node("classify", classify)
    g.add_node("comment", comment)
    g.add_edge(START, "classify")
    g.add_edge("classify", "comment")
    g.add_edge("comment", END)
    return g.compile()


# --------------------------------------------------------------------------
# 진입점
# --------------------------------------------------------------------------


def run_echo(text: str, detail: Detail = "full") -> dict[str, Any]:
    """에이전트 진입점.

    detail 로 반환량을 조절한다. 로컬 LLM 은 컨텍스트가 비싸므로
    챗봇 툴은 'summary' 를 쓰고, 화면에 뿌릴 전체 데이터는 별도
    API(detail_endpoint)로 'full' 을 가져간다 — LLM 컨텍스트 밖에서.

        full    : 전부 (근거·경고 포함)
        summary : LLM 에게 보여줄 요약
        minimal : 판정 결과만
    """
    state: EchoState = _graph().invoke({"text": text})

    if detail == "minimal":
        return {"kind": state.get("kind"), "decided_by": state.get("decided_by")}

    if detail == "summary":
        return {
            "kind": state.get("kind"),
            "decided_by": state.get("decided_by"),
            "rule": state.get("rule"),
            "comment": state.get("comment") or "",
            "warnings": state.get("warnings") or [],
        }

    return {
        "text": state.get("text", text),
        "kind": state.get("kind"),
        "decided_by": state.get("decided_by"),
        "rule": state.get("rule"),
        "evidence": state.get("evidence"),
        "comment": state.get("comment") or "",
        "warnings": state.get("warnings") or [],
    }


def summarize_runs(results: list[dict[str, Any]]) -> str:
    """규칙별 집계. 콘솔에 출력해 어떤 규칙이 얼마나 쓰였는지 본다.

    결과만 쌓아 두면 '왜 이렇게 많이 걸렸나'를 못 본다(규칙 4-6).
    """
    counts: dict[str, int] = {}
    for r in results:
        k = f"{r.get('decided_by')}/{r.get('rule')}"
        counts[k] = counts.get(k, 0) + 1
    return "\n".join(f"  {k:<28} {v:>4}건" for k, v in sorted(counts.items()))


if __name__ == "__main__":
    import argparse
    import json

    ap = argparse.ArgumentParser(description="echo 에이전트 (샘플)")
    ap.add_argument("text", nargs="+", help="분류할 문장")
    ap.add_argument(
        "--detail", choices=["full", "summary", "minimal"], default="full"
    )
    args = ap.parse_args()

    result = run_echo(" ".join(args.text), detail=args.detail)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    print("\n[규칙별 집계]")
    print(summarize_runs([result]))
