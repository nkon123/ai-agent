# Claude Code 작업 지침 — ai-agent

로컬 LLM 에이전트(LangGraph)를 MCP 툴로 묶어 Flask 챗봇에서 쓰는 저장소.
설계 원칙·개발 제약·이미 데인 함정은 **`README.md` 가 원본**이다. 작업 전에
README 의 `구조`, `새 에이전트 추가하기`, `개발 제약`, `이미 데인 자리` 를 읽는다.
이 파일은 README 를 복사하지 않고, 작업 순서와 놓치기 쉬운 것만 적는다.

## ai-hub 와 다른 저장소다

- `C:\Dev\ai-hub` 의 규칙(모듈 소유권 M01~M12, `RAG` 용어 금지, open-decisions·
  progress-log 갱신, `verify-change` 스크립트, 샘플 서버의 "표준 라이브러리 + mcp 만")은
  **여기에 적용하지 않는다.** 두 저장소의 코드를 서로 import 하거나 복사해 섞지 않는다.
- 여기서는 Oracle 직접 조회(`core/oracle.py`, `oracledb`)와 Outlook COM
  (`core/outlook.py`, pywin32)이 정상 구성 요소다.

## 작업 시작 전

1. 관련 에이전트의 `agents/<name>/README.md` 를 읽는다.
2. 같은 일을 하는 코드가 `core/` 에 이미 있는지 본다 — 식별자 정규식·주석 제거·
   인코딩(`core/text.py`), SQL 문장 자르기·판정(`core/sqlstmt.py`), 메일
   (`core/outlook.py`), DB(`core/oracle.py`), LLM(`core/llm.py`), 캐시, 진행 알림.
   새로 짜지 말고 쓴다 — README `이미 데인 자리` 의 함정들이 거기서 막혀 있다.
3. `pytest -q` 로 기준선을 본다(아래 '실행 환경').

## 구조 규칙 (README 요약 — 어기면 실제로 깨졌던 것)

- 설정은 `config.py` 한 곳. `core/`·`agents/`·`app/` 에서 `os.getenv` 금지.
  `config.py` 는 기본값만 — 사내 값은 추적하지 않는 `config_local.py`.
  새 설정은 `config.py` 와 `config_local.example.py` 에 함께 추가한다.
- 에이전트(`agents/<name>/agent.py`)는 MCP 를 모른다(`mcp` import 금지).
  툴 파일(`mcp_server/tools/<name>.py`)은 얇게 — 로직을 넣지 않는다.
  툴을 추가해도 `app/app.py`·`mcp_server/__main__.py` 는 고치지 않는다.
- 로컬 소형 모델은 툴을 연쇄 호출하지 못한다. 챗봇에 노출할 기능은 한 번에 끝나는
  `tier="combo"` 툴로 만들고, 단계 툴(`tier="step"`)은 같은 에이전트 함수를 재사용한다.
- LLM 은 `core.llm.get_llm()` / `get_structured_llm()` 으로만. `USE_LLM=False` 에서도
  규칙만으로 동작해야 한다.
- 결과에 판정 근거(`decided_by`·`rule`·`evidence`)를 남기고, 판정 불가는 '확인 필요'로
  남긴다. '모른다'와 '아니다'를 구분한다.
- 챗봇 툴은 `detail="summary"`, 전체 데이터는 MCP 리소스로.
- 오래 걸리는 작업은 `core.progress.notify()` 로 진행을 알리고, 툴 껍데기에서
  `report_while()` 로 감싼다.

## 안전

- 메일 발송·파일 삭제·DDL/DML 은 자동 실행하지 않는다. 초안·SQL 출력까지만.
  되돌리기 어려운 툴은 `destructive=True`.
- DB 조회는 바인드 변수만. 문자열 결합 금지.
- 실제 사내 경로·계정·SQL·메일 내용을 커밋하지 않는다(`config_local.py`, `*.csv`,
  `*.xlsx` 는 ignore 됨). 시험 데이터는 `samples/` 에 가짜 값으로 만든다.
- 개인정보·인증정보를 로그에 남기지 않는다.

## 실행 환경

- 이 PC(`C:\Dev\ai-agent`)에는 아직 의존성이 설치되어 있지 않다(시스템 Python 3.14 에
  `mcp` 없음). 시험 전에 `python -m venv .venv` → `.venv\Scripts\pip install -r requirements.txt`.
  Outlook·Oracle 이 없는 PC 이므로 `MAIL_BACKEND=eml`(samples/mail)과 DB 없는 경로로 확인한다.
- Ollama 는 `http://172.30.0.1:11434` 에 있고 모델은 `qwen3.5:4b` 다(저장소 기본값은
  `gemma4:e2b`). 실측이 필요하면 환경변수로 한 번만 바꿔 실행하고 `config.py` 는 고치지 않는다.
- 시험: `pytest -q`. 새 기능에는 `tests/test_<agent>.py` 에 시험을 추가한다.
  시험 증거 없이 완료라고 보고하지 않는다. Outlook·DB 를 실제로 확인하지 못했으면 그렇게 말한다.

## 커밋

- 메시지는 한국어 한 줄로 '무엇을 왜' (예: `analyze_table_impact 가 대소문자를 가려 못 찾던 문제 수정`).
  접두어(`feat:` 등)를 쓰지 않는다.
- 사용자가 요청할 때만 커밋·푸시한다.
- README 의 툴 표·구조 표·에이전트 README 를 코드와 함께 갱신한다.
