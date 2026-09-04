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

from config import AGENT_REASONING, JUDGE_MODEL, NUM_CTX, OLLAMA_HOST


def get_llm(model: str | None = None, **kw: Any) -> ChatOllama:
    """표준 옵션이 박힌 ChatOllama 인스턴스.

    temperature=0  : 판정용이므로 재현 가능해야 한다.
    num_ctx        : 기본값 2048 은 프롬프트를 조용히 자른다. 필수.
    keep_alive="10m": 루프를 도는 동안 모델이 언로드되면 매 호출마다
                      수 초씩 재적재된다. 6GB 노트북에서 특히 뼈아프다.
    """
    # 기본은 에이전트 내부용 설정을 따른다. 대화(챗봇)는 app.py 가
    # CHAT_REASONING 을 명시해서 부른다.
    if kw.get("reasoning", "unset") is None:
        kw.pop("reasoning")           # None = 모델 기본값 (인자를 넘기지 않는다)
    elif "reasoning" not in kw and AGENT_REASONING is not None:
        kw["reasoning"] = AGENT_REASONING
    return ChatOllama(
        base_url=OLLAMA_HOST,
        model=model or JUDGE_MODEL,
        temperature=0,
        num_ctx=NUM_CTX,
        keep_alive="10m",
        **kw,
    )


def get_structured_llm(
    schema: Any, model: str | None = None, reasoning: bool | None = None
):
    """스키마를 강제하는 LLM.

    소형 모델은 tool calling 이 자주 깨진다(인자를 빼먹거나 JSON 이
    문법적으로 어긋난다). json_schema 방식은 Ollama 의 format 파라미터로
    스키마를 넘겨 디코딩 단계에서 문법을 강제하므로, 어긋난 JSON 이
    나올 수 없다. 판정 결과를 받아올 때는 항상 이쪽을 쓴다.

    reasoning: None 이면 config.AGENT_REASONING 을 따른다. 사고 과정은 판정
    정확도를 크게 올리지만 토큰을 많이 쓴다 — 프롬프트가 크면 사고에
    컨텍스트를 다 써서 본문이 비어 나온다. invoke_structured() 를 쓰면
    그 경우를 자동으로 처리한다.
    """
    kw: dict[str, Any] = {} if reasoning is None else {"reasoning": reasoning}
    return get_llm(model, **kw).with_structured_output(schema, method="json_schema")


def invoke_structured(
    schema: Any,
    prompt: str,
    model: str | None = None,
    reasoning: bool | None = None,
) -> tuple[Any, str]:
    """구조화 출력을 받는다. (결과, 경고문)

    한 번 실패하면 사고 과정을 끄고 다시 시도한다.

    reasoning 은 과제 성격에 따라 호출부가 정한다.
        판정(이 SQL 이 이 테이블을 쓰는가) → 켠다. 정확도가 크게 오른다
                                            (같은 3건에서 3/3 vs 1/3)
        생성(고친 쿼리를 써 봐라)          → 끈다. 사고에만 수백 초를 쓰고
                                            본문을 못 내는 일이 잦다

    왜 이런 재시도가 필요한가:
        추론(thinking) 모델은 사고에 토큰을 쓰는데, 프롬프트가 크면
        컨텍스트가 모자라 정작 본문이 비어서 나온다. 실제로 Qwen3.5-4B 가
        392초를 쓰고 빈 문자열을 돌려줘 'Invalid json output' 으로 떨어졌다.
        같은 요청을 사고 없이 보내면 20초에 정상 JSON 이 나온다.

        그렇다고 처음부터 끄면 안 된다. 판정 과제에서는 사고가 정확도를
        크게 올린다(같은 3건에서 3/3 → 1/3 으로 떨어졌다). 그래서 기본은
        켜 두고, 실패했을 때만 끈다.

    경고문이 비어 있지 않으면 두 번째 시도로 얻은 결과라는 뜻이다.
    호출부는 그 사실을 결과에 남길 것.
    """
    try:
        return get_structured_llm(schema, model, reasoning).invoke(prompt), ""
    except Exception as first:
        if reasoning is False:
            raise
        try:
            result = get_structured_llm(schema, model, reasoning=False).invoke(prompt)
        except Exception as second:
            raise second from first
        return result, (
            "사고 과정을 켠 첫 시도가 실패해 끄고 다시 받았다 "
            f"({type(first).__name__}). 프롬프트가 컨텍스트에 비해 큰지 확인할 것"
        )


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
