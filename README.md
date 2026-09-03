# ai-agent

여러 개의 로컬 LLM 에이전트를 한 곳에 모으고, 챗봇 하나로 묶어 쓰는 저장소.

각 에이전트는 **CLI 로 단독 실행**할 수도 있고, **챗봇에 툴로 붙일** 수도 있다.
소스 형태로 배포한다 (패키징 없음).

---

## 설치

```bash
git clone https://github.com/nkon123/ai-agent.git
cd ai-agent
python -m venv .venv
.venv\Scripts\activate          # Windows
pip install -r requirements.txt
```

Ollama 와 모델 준비:

```bash
ollama pull gemma4:e2b
```

## 실행

```bash
# 챗봇 서버
python app/app.py                       # → http://127.0.0.1:5000

# Ollama 없이 기동만 확인 (툴 목록·라우트는 살아 있다)
set USE_LLM=false && python app/app.py

# 에이전트 단독 실행
python agents/echo/agent.py "이거 왜 안 되지?"

# 테스트
pytest -q
```

기동하면 콘솔에 현재 설정과 등록된 툴 목록이 찍힌다. **다른 값이 보이면
설정이 이원화된 것이니 먼저 그것부터 고칠 것.**

---

## 구조

```
config.py           공통 설정 — 단일 진실 원천
core/
  llm.py            ChatOllama 팩토리 (직접 ChatOllama 부르지 말 것)
  oracle.py         DB 연결·조회 (바인드 변수 필수)
  text.py           인코딩 감지, 주석 제거, 식별자 정규식
  cache.py          TTL/LRU 캐시
agents/
  echo/             샘플 에이전트 — 새 에이전트의 템플릿
app/
  app.py            Flask 챗봇 (툴 추가 시 수정하지 않는다)
  tools/            @tool 껍데기 + 자동 등록 레지스트리
  templates/        채팅 UI
prompts/            에이전트 생성용 프롬프트 템플릿
tests/              core/ 회귀 테스트
```

| 위치 | 역할 |
|---|---|
| `agents/<name>/agent.py` | LangGraph 흐름, `run_xxx()`, CLI |
| `app/tools/<name>.py` | `@tool` + `register()`. **얇게** |

---

## 새 에이전트 추가하기

`agents/echo/` 와 `app/tools/echo.py` 를 복사해 시작한다.

**1. 에이전트 폴더를 만든다**

```
agents/myagent/
  __init__.py     from .agent import run_myagent
  agent.py
  README.md
```

**2. `agent.py` 를 작성한다** (`agents/echo/agent.py` 복사)

- 저장소 루트를 `sys.path` 에 넣는 상단 블록은 그대로 둔다 (CLI 단독 실행용)
- 판정 단계를 LangGraph 노드로 나눈다
- 진입점은 `run_myagent(..., detail="full") -> dict`
- `if __name__ == "__main__"` 에 CLI 를 둔다

**3. `app/tools/myagent.py` 를 만든다**

```python
from langchain_core.tools import tool
from agents.myagent import run_myagent
from . import register

@tool
def myagent_run(arg: str) -> str:
    """LLM 이 읽는 설명. 언제 이 툴을 쓰는지 여기에 쓴다."""
    return str(run_myagent(arg, detail="summary"))   # summary!

register(myagent_run, label="내 에이전트", view="text",
         detail_endpoint="/api/myagent",
         hint="myagent_run 은 ... 할 때만 쓴다.")
```

**4. 끝.** `app.py` 는 수정하지 않는다. 서버를 다시 띄우면 `/api/tools` 에 나온다.

전체 데이터를 보여줄 API 가 필요하면 툴 파일 안에서 Flask `Blueprint` 를
만들어 `register(..., blueprint=bp)` 로 같이 넘긴다.

**5. 테스트를 추가하고 `pytest -q` 로 확인한다.**

---

## 개발 제약 (반드시 지킬 것)

### 설정은 `config.py` 한 곳에만

`core/` 나 `agents/` 안에서 `os.getenv` 를 다시 부르지 말 것. 전부
`from config import ...` 로 가져온다.

> 설정이 이원화되면 한쪽만 고쳤을 때 서로 다른 값을 보게 된다. 실제로
> 앱은 새 경로를, 툴은 기본 경로를 보고 있어 같은 질문에 다른 답이 나왔다.

Windows 경로 기본값은 반드시 `r"..."` 로. `\t`, `\n` 이 해석돼 깨진다.

### LLM 생성은 `core/llm.get_llm()` 으로만

직접 `ChatOllama(...)` 를 부르지 말 것. `num_ctx` 기본값(2048)은 프롬프트를
조용히 자르고, `keep_alive` 가 없으면 루프 도는 중에 모델이 언로드된다.
구조화 출력이 필요하면 `get_structured_llm(schema)` — 소형 모델은 tool
calling 이 자주 깨지지만 `json_schema` 는 문법을 강제한다.

`config.USE_LLM` 이 False 면 LLM 없이 규칙만으로 동작해야 한다.

### 에이전트당 툴은 하나만 노출

소형 모델은 툴을 순서대로 여러 개 부르지 못한다. 내부 단계는 일반 함수로
두고 `@tool` 을 붙이지 않는다. 툴 파일에는 로직을 넣지 않는다.

### 컨텍스트 절약

`run_xxx(detail=...)` 는 `full` / `summary` / `minimal` 을 지원한다.

- 챗봇 툴 → `summary` (LLM 컨텍스트에 들어간다)
- 화면 표시용 전체 데이터 → `detail_endpoint` 로 따로 (컨텍스트 밖)

같은 작업이 두 번 돌지 않게 `core/cache.py` 를 쓴다. **무엇이 비싼지에 따라
캐시 키를 정할 것** — 파일 스캔이 무겁고 특정 인자와 무관하면 그 인자를 뺀
키로 캐시한다 (`cached(key=lambda root, name: root)`).

### 판정 근거를 남긴다

```python
{"result": ..., "decided_by": "rule|llm|static|fallback",
 "rule": "규칙명", "evidence": "판단 근거가 된 실제 조각"}
```

결과만 있으면 오판 원인을 못 찾는다. 콘솔에 규칙별 집계도 출력한다.

### 실패는 보수적으로

LLM 호출 실패나 판정 불가를 조용히 넘기지 말 것. "확인 필요"로 표시해 결과에
남긴다. **누락은 오탐보다 나쁘다.** "모른다"(판단 불가)와 "아니다"(해당 없음)를
같은 이름으로 쓰지 않는다.

### 안전

- 외부로 나가는 동작(메일 발송, 파일 삭제, DDL/DML)은 자동 실행 금지.
  초안이나 SQL 출력까지만 하고 사람이 확인 후 실행한다
- DB 조회는 반드시 바인드 변수. 문자열 결합 금지
- 개인정보·인증정보를 로그에 남기지 않는다

### 코드 작성

- 주석은 **왜 그렇게 했는지**를 적는다. 무엇을 하는지는 코드가 말한다.
  함정을 피하려고 넣은 코드에는 반드시 이유를 남긴다
- 한국어 주석, 타입 힌트, 모듈 상단 docstring
- 과도한 추상화 금지. 지금 필요한 것만 만든다

---

## 이미 데인 자리 (`tests/test_core.py` 가 지킨다)

| 함정 | 왜 |
|---|---|
| `\b` 로 식별자 매칭 | 정규식은 `_` 를 단어 문자로 본다. `IF_A` 에서 `A` 가 안 잡힌다 |
| `/*` `*/` 개수 세기 | `a*/*c*/b`(포인터 연산)와 `"/* x */"`(문자열 안)에서 깨진다 |
| utf-8 로만 파일 읽기 | 사내 파일은 cp949 인 경우가 흔하다 |
| BOM 방치 | utf-8 디코드는 성공하면서 맨 앞에 U+FEFF 를 남긴다 |
| `num_ctx` 미지정 | 기본 2048 이 프롬프트를 조용히 자른다 |
| 전체 메시지에서 tool_calls 수집 | 체크포인터의 이전 턴 호출까지 딸려와 중복 표시된다 |

---

## 환경

Python 3.10+ / Windows 사내망(폐쇄망) / Ollama + `gemma4:e2b` (VRAM 6GB) /
LangGraph + LangChain 1.x / Oracle(`oracledb`, 없어도 나머지는 동작)
