# iferr (인터페이스 오류 확인)

타 시스템 연계에서 오류가 나면 인터페이스 키가 적힌 메일이 온다.
그 키로 테이블을 확인해 **어떤 데이터가 어떻게 영향을 받는지** 정리한다.

```
메일 수집 → 키 추출 → DB 조회 → 영향 판정 → (선택) LLM 요약
```

## 실행

```bash
python agents/iferr/agent.py                    # 최근 24시간
python agents/iferr/agent.py --hours 72
python agents/iferr/agent.py --key IF_ORD_SEND  # 메일을 읽지 않고 이 키만
MAIL_BACKEND=eml python agents/iferr/agent.py   # 파일로 테스트
```

## 설정할 것 세 가지

### 1. 메일 (`config.py`)

```python
MAIL_BACKEND = "com"                      # 로컬 Outlook (Windows + pywin32)
MAIL_FOLDER  = r"받은 편지함\인터페이스"    # Outlook 규칙으로 모아 둔 폴더
MAIL_SUBJECT_KEYWORDS = ("오류","에러","실패","ERROR","FAIL",...)
```

`pip install pywin32` 가 필요하다. Outlook 규칙으로 오류 메일만 하위 폴더에
모아 두면 스캔 범위가 줄어 훨씬 빠르다.

리눅스 개발 PC 나 테스트에서는 `MAIL_BACKEND=eml` 로 `samples/mail/` 의
`.eml` 파일을 읽는다.

### 2. 키 추출 정규식 (`config.IFERR_KEY_PATTERNS`)

`(규칙이름, 정규식)` 목록이며 **그룹 1이 키**다. 위에서부터 시도하고,
맞은 규칙 이름이 근거로 남는다. 실제 메일 형식을 보고 이 목록만 고치면 된다.

```python
("if-id-labeled", r"(?i)\bIF[_\-]?ID\s*[:=]\s*([A-Za-z0-9_\-]{3,40})"),
("interface-ko",  r"인터페이스\s*(?:ID|아이디|키|번호)\s*[:=]?\s*([A-Za-z0-9_\-]{3,40})"),
```

### 3. 조회 SQL (`config.IFERR_SQL`) — **아직 비어 있다**

```python
IFERR_SQL = {
    "header": "SELECT ... FROM if_hdr WHERE if_key = :if_key",
    "detail": "SELECT ... FROM if_dtl WHERE if_key = :if_key",
    "impact": "SELECT ... WHERE ... = :if_key",
}
```

- 바인드 변수 이름은 **`:if_key` 로 고정**이다
- 문자열 결합 금지. 키는 반드시 바인드로 들어간다 (테스트로 고정되어 있다)
- SELECT 만 넣는다. DML/DDL 은 이 에이전트가 실행하지 않는다
- 셋 다 넣을 필요는 없다. 채운 것만 실행한다

SQL 이 비어 있으면 키 추출까지만 하고 **"확인 불가"** 로 남긴다.
"영향 없음"으로 처리하지 않는다.

## 판정

```python
{"key": "IF_ORD_SEND",
 "db": {"status": "found|missing|unknown", "rule": "...", "rows": {...}},
 "impact": "header 3건 / 상태 E=2, S=1",
 "decided_by": "rule|fallback",
 "rule": "rows-found|no-rows|sql-not-configured|db-not-configured|query-failed",
 "evidence": "IF_ID : IF_ORD_SEND 발생시각"}
```

| status | 뜻 |
|---|---|
| `found` | 데이터가 있고 상태를 확인했다 |
| `missing` | **없다** — 해당 키의 행이 테이블에 없다 |
| `unknown` | **확인하지 못했다** — SQL/DB 미설정, 조회 실패 |

`missing` 과 `unknown` 을 섞으면 장애를 놓친다. 이게 이 에이전트에서
가장 중요한 구분이다.

상태 컬럼은 `config.IFERR_STATUS_COLUMNS` 후보 중 결과에 있는 것을
값별로 집계한다. 스키마를 몰라도 동작하게 하려는 장치이므로, 스키마가
확정되면 `assess()` 를 구체화하는 편이 낫다.

## 안전

- **메일은 읽기만 한다.** 회신·삭제·이동·읽음 표시 변경을 하지 않는다
- **DB 는 SELECT 만.** 재처리·데이터 수정은 이 툴로 할 수 없다.
  필요해지면 별도 툴로 만들고 `destructive=True` 를 줄 것
- 발신자 주소는 마스킹해서 남긴다 (`if.monitor@...` → `if***@...`)

## MCP

기능별로 나눈 단계별 툴과, 한 번에 도는 통합 툴을 함께 둔다.
**같은 에이전트 함수를 재사용**하므로 어느 쪽으로 불러도 결과가 같다.

| 툴 | 등급 | 메일 | DB | 무엇 |
|---|---|---|---|---|
| `check_interface_errors(hours, key)` | combo | ○ | ○ | 전체를 한 번에 |
| `list_error_mails(hours)` | step | ○ | ✕ | 메일이 오긴 왔는지 |
| `extract_interface_keys(text)` | step | ✕ | ✕ | 붙여 넣은 본문에서 키만 |
| `lookup_interface(key)` | step | ✕ | ○ | 키를 이미 알 때 |

챗봇(로컬 소형 모델)에는 **combo 만 보인다** — 툴을 여러 개 주면 순서대로
부르지 못한다. step 툴은 MCP 서버가 그대로 노출하므로 Claude Code·IDE
같은 다른 호스트에서 쓴다 (`CHAT_TOOL_TIERS=combo,step` 으로 챗봇에도 열 수 있다).

| 리소스 | 무엇 |
|---|---|
| `iferr://detail/{key}` | `detail="full"` — 메일 목록 + 조회 행 전부 |

annotations: `readOnly=true`, `destructive=false`

```
GET /api/resource?template=iferr://detail/{key}&key=IF_ORD_SEND
```

## 알려진 함정

| 함정 | 왜 |
|---|---|
| COM 을 워커 스레드에서 초기화 없이 호출 | `pythoncom.CoInitialize()` 없이 부르면 원인 알기 어려운 에러가 난다. Flask/MCP 브리지는 별도 스레드다 |
| `Restrict` 없이 전체 순회 | 메일 한 통마다 COM 왕복이 일어나 수만 통에서 몇 분씩 걸린다 |
| `Sort` 를 `Restrict` 뒤에 | 결과가 정렬되지 않아 상위 N 통이 최신이 아니다 |
| 본문을 utf-8 로만 디코드 | 사내 메일 본문은 cp949/euc-kr 이 흔하다 |
| 메일 읽기 실패를 빈 결과로 | '오류 없음'으로 읽혀 장애를 놓친다 |
