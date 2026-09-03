# ai-agent

여러 개의 로컬 LLM 에이전트를 한 곳에 모으고, 챗봇 하나로 묶어 쓰는 저장소.

각 에이전트는 **CLI 로 단독 실행**할 수도 있고, **MCP 툴로 챗봇에 붙일** 수도 있다.
툴 계층은 **MCP 스펙 리비전 `2026-07-28`** 위에 있다 (stateless 코어, MRTR,
확장 프레임워크). 소스 형태로 배포한다 (패키징 없음).

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
# 챗봇 서버 (MCP 서버를 stdio 로 직접 띄운다)
python app/app.py                       # → http://127.0.0.1:5000

# Ollama 없이 기동만 확인 (툴 목록·라우트는 살아 있다)
set USE_LLM=false && python app/app.py

# MCP 서버만 단독 실행 (다른 MCP 호스트에 붙일 때)
python -m mcp_server                    # stdio
set MCP_TRANSPORT=streamable-http && python -m mcp_server

# 에이전트 단독 실행 (MCP 없이)
python agents/echo/agent.py "이거 왜 안 되지?"
python agents/usage/agent.py TOTAL_AMT --root SAMPLE

# 테스트
pytest -q
```

### 다른 MCP 호스트에 붙이기

Claude Code, IDE 등 MCP 호스트에서 같은 툴을 그대로 쓸 수 있다.

```json
{
  "mcpServers": {
    "ai-agent": {
      "command": "python",
      "args": ["-m", "mcp_server"],
      "cwd": "C:\\path\\to\\ai-agent"
    }
  }
}
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
  echo/             구조 확인용 최소 샘플 — 새 에이전트의 템플릿
  usage/            식별자 사용처 찾기 — core/ 를 실제로 쓰는 샘플
samples/src/        usage 를 바로 돌려 볼 예제 소스 (함정 포함)
mcp_server/         MCP 서버 (툴 계층)
  __main__.py       python -m mcp_server
  tools/            MCP 툴 껍데기 + 자동 등록 레지스트리
app/
  app.py            Flask 챗봇 = MCP 클라이언트 (툴 추가 시 수정하지 않는다)
  mcp_bridge.py     MCP 툴 → LangChain 툴 변환
  templates/        채팅 UI
prompts/            에이전트 생성용 프롬프트 템플릿
tests/              core/ + MCP 회귀 테스트
```

| 위치 | 역할 |
|---|---|
| `agents/<name>/agent.py` | LangGraph 흐름, `run_xxx()`, CLI. MCP 를 모른다 |
| `mcp_server/tools/<name>.py` | MCP 툴/리소스 등록. **얇게** |

에이전트와 챗봇은 **프로세스가 분리**되어 있고 MCP 로만 통신한다.
덕분에 (1) 다른 MCP 호스트에서도 같은 툴을 쓰고, (2) 에이전트가 죽어도
챗봇은 살아 있고, (3) 에이전트를 다른 장비로 옮겨도 transport 만 바꾸면 된다.

---

## 샘플 에이전트 둘

| 에이전트 | 무엇 | 볼 것 |
|---|---|---|
| `echo` | 문장을 질문/명령/평서로 분류 | 최소 구조. 새 에이전트는 여기서 복사해 시작한다 |
| `usage` | 소스에서 식별자 사용처 찾기 | `core/` 를 실제로 쓰는 형태. 캐시 키 설계, 근거 남기기, '없다'와 '모른다' 구분 |

`usage` 를 돌려 보면 `core/text.py` 가 왜 그렇게 생겼는지 바로 보인다.

```bash
python agents/usage/agent.py TOTAL_AMT --root SAMPLE
```

```
erp_calc.c    : 5 | long TOTAL_AMT;          ← 선언
erp_calc.c    :10 | TOTAL_AMT = r;           ← 대입
erp_calc.c    :11 | return TOTAL_AMT;        ← 반환
legacy_cp949.c: 2 | void legacy(void) {...}  ← cp949 파일도 읽는다
order_pkg.sql : 5 | UPDATE ORDERS SET ...    ← SQL
```

주석 속 `TOTAL_AMT`, 문자열 속 `"/* TOTAL_AMT */"`, `IF_A` 의 `A` 는
**잡히지 않는다**. 세 가지 다 실제로 오탐이 났던 형태다
(`samples/README.md` 에 무엇을 심어 뒀는지 적어 두었다).

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
- **에이전트는 MCP 를 모른다.** `mcp` 를 import 하지 않는다

**3. `mcp_server/tools/myagent.py` 를 만든다**

```python
import json
from agents.myagent import run_myagent
from . import mcp, register

@register(label="내 에이전트", view="text",
          detail_uri="myagent://detail/{arg}",
          hint="myagent_run 은 ... 할 때만 쓴다.",
          read_only=True)          # 파괴적이면 destructive=True
def myagent_run(arg: str) -> str:
    """LLM 이 읽는 설명. 언제 이 툴을 쓰는지 여기에 쓴다."""
    return str(run_myagent(arg, detail="summary"))   # summary!

@mcp.resource("myagent://detail/{arg}", mime_type="application/json")
def myagent_detail(arg: str) -> str:
    return json.dumps(run_myagent(arg, detail="full"), ensure_ascii=False)
```

**4. 끝.** `app.py` 도 `mcp_server/__main__.py` 도 수정하지 않는다.
다시 띄우면 `/api/tools` 에 나온다.

전체 데이터는 **MCP 리소스**로 내보낸다. 화면은 프록시로 읽어 간다 —
툴마다 라우트를 만들 필요가 없다.

```
GET /api/resource?template=myagent://detail/{arg}&arg=값
```

`template` 과 값을 따로 넘기면 서버가 퍼센트 인코딩해 조립한다.
직접 만든 URI 를 `uri=` 로 넘길 수도 있지만 이중 인코딩이 필요하다.

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
두고 등록하지 않는다. 툴 파일에는 로직을 넣지 않는다.

### MCP 메타데이터는 `_meta` 와 annotations 로

`label`/`view`/`hint`/`detail_uri` 는 표준 필드가 아니라 MCP `_meta` 에 실어
보낸다. 별도 설정 파일을 두면 툴과 메타데이터가 따로 놀다가 어긋난다.

`read_only` / `destructive` 는 **표준 annotations** 다. 우리 챗봇뿐 아니라
다른 MCP 호스트도 이 힌트를 읽고 확인 절차를 넣는다. 파일 삭제·메일 발송·DML
처럼 되돌리기 어려운 툴에는 반드시 `destructive=True` 를 줄 것. 클라이언트가
시스템 프롬프트에 확인 문구를 자동으로 덧붙이고, 서버가 MRTR/elicitation 으로
사람에게 물으면 **기본적으로 거절**한 뒤 화면에 "확인 필요"로 띄운다.

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
| 문자열 리터럴을 그대로 두고 검색 | `'TOTAL_AMT is not a hit'` 이 사용처로 잡힌다. 사용처 검색에는 `strip_comments(..., mask_strings=True)` |
| BOM 방치 | utf-8 디코드는 성공하면서 맨 앞에 U+FEFF 를 남긴다 |
| `num_ctx` 미지정 | 기본 2048 이 프롬프트를 조용히 자른다 |
| 전체 메시지에서 tool_calls 수집 | 체크포인터의 이전 턴 호출까지 딸려와 중복 표시된다 |
| stdio 자식에 env 안 넘기기 | SDK 가 PATH 등만 물려줘 MCP 서버가 config 기본값으로 돌아간다. `USE_LLM=false` 인데 서버만 Ollama 를 부른다 |
| 요청마다 MCP 클라이언트 생성 | stdio 서버 프로세스가 매번 새로 뜬다. 루프 하나를 전용 스레드에 띄우고 재사용한다 |
| 리소스 URI 값을 인코딩 안 함 | "왜 안 되지?" 의 `?` 가 URI 문법으로 해석돼 `Unknown resource` 가 된다. `template=` 형태로 넘기거나 `mcp_bridge.fill_uri()` 를 쓸 것 |

---

## 환경

Python 3.10+ / Windows 사내망(폐쇄망) / Ollama + `gemma4:e2b` (VRAM 6GB) /
MCP `2026-07-28` (`mcp>=2.1`) / LangGraph + LangChain 1.x /
Oracle(`oracledb`, 없어도 나머지는 동작)

> `langchain-mcp-adapters` 는 쓰지 않는다 — `mcp<2.0` 을 핀하고 있어
> 2026-07-28 리비전과 같이 설치할 수 없다. MCP 툴 → LangChain 툴 변환은
> `app/mcp_bridge.py` 에서 직접 한다(스키마를 그대로 넘기는 수준이라 짧다).
