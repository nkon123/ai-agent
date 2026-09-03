"""ChatOllama 팩토리.

사용법:
    from core.llm import get_llm, get_structured_llm, check_ollama

    llm = get_llm()                       # JUDGE_MODEL 로
    llm = get_llm(model="gemma4:e2b")     # 모델 지정
    slm = get_structured_llm(MySchema)    # 구조화 출력

규칙:
    직접 ChatOllama(...) 를 부르지 말 것. num_ctx, keep_alive 같은 값을
    매번 손으로 넣으면 언젠가 한 곳에서 빠뜨리고, 그 한 곳만 다르게
    동작한다. 반드시 이 팩토리를 거친다.
"""

from __future__ import annotations

from typing import Any

from langchain_ollama import ChatOllama

from config import JUDGE_MODEL, NUM_CTX, OLLAMA_HOST


def get_llm(model: str | None = None, **kw: Any) -> ChatOllama:
    """표준 옵션이 박힌 ChatOllama 인스턴스.

    temperature=0  : 판정용이므로 재현 가능해야 한다.
    num_ctx        : 기본값 2048 은 프롬프트를 조용히 자른다. 필수.
    keep_alive="10m": 루프를 도는 동안 모델이 언로드되면 매 호출마다
                      수 초씩 재적재된다. 6GB 노트북에서 특히 뼈아프다.
    """
    return ChatOllama(
        base_url=OLLAMA_HOST,
        model=model or JUDGE_MODEL,
        temperature=0,
        num_ctx=NUM_CTX,
        keep_alive="10m",
        **kw,
    )


def get_structured_llm(schema: Any, model: str | None = None):
    """스키마를 강제하는 LLM.

    소형 모델은 tool calling 이 자주 깨진다(인자를 빼먹거나 JSON 이
    문법적으로 어긋난다). json_schema 방식은 Ollama 의 format 파라미터로
    스키마를 넘겨 디코딩 단계에서 문법을 강제하므로, 어긋난 JSON 이
    나올 수 없다. 판정 결과를 받아올 때는 항상 이쪽을 쓴다.
    """
    return get_llm(model).with_structured_output(schema, method="json_schema")


def check_ollama(model: str | None = None) -> tuple[bool, str]:
    """Ollama 연결과 모델 존재를 확인한다. (성공여부, 메시지)

    실패를 예외로 던지지 않고 튜플로 돌려주는 이유:
    서버 기동 시 '경고만 하고 계속 뜨는' 동작이 필요하기 때문이다.
    Ollama 가 죽어 있어도 /api/tools 같은 라우트는 살아 있어야 한다.
    """
    want = model or JUDGE_MODEL
    try:
        # httpx 는 langchain-ollama 의 의존성이라 별도 설치가 필요 없다.
        import httpx

        r = httpx.get(f"{OLLAMA_HOST}/api/tags", timeout=3.0)
        r.raise_for_status()
        names = [m.get("name", "") for m in r.json().get("models", [])]
    except Exception as e:  # 연결 실패 원인은 그대로 보여준다
        return False, f"Ollama 연결 실패 ({OLLAMA_HOST}): {e}"

    # Ollama 는 태그 없는 이름을 :latest 로 저장한다. 양쪽을 다 본다.
    if want in names or f"{want}:latest" in names:
        return True, f"Ollama 정상, 모델 '{want}' 확인"
    return False, (
        f"Ollama 는 떠 있으나 모델 '{want}' 이(가) 없다. "
        f"설치된 모델: {', '.join(names) or '(없음)'}"
    )


if __name__ == "__main__":
    ok, msg = check_ollama()
    print(("OK  " if ok else "FAIL") + " " + msg)
