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

## 점검은 이 한 줄로

```bat
python agents\iferr\agent.py --doctor --hours 72
```

```
[점검] 최근 72시간

  OK    제목 판정 설정       [startswith] (EAA) Alert Mail
  OK    메일 읽기          38통 중 오류 4통
  OK    키 추출           2개: EAIIF0001234, EAIIF0009999
  OK    DB 접속           접속 OK (dsn=..., 접속계정=ERP_READ, 모드=thick, ...)
  OK    마스터 조회         EAIIF0001234: found — SAP.ZORDER → ERP.IF_ORD (매일 08:30, 12:00)

전부 정상 — python app/app.py 로 챗봇을 띄워도 된다
```

설정부터 실제 DB 조회까지 순서대로 확인한다. 앞 단계가 실패해도 뒤 단계를
건너뛰지 않는다 — 문제가 두 개일 수 있다. 실패한 줄에 다음에 할 일이 붙는다.

아래는 단계별로 따로 볼 때 쓰는 명령들이다.

## 사내 PC 첫 점검 (DB 없이 메일만)

DB 설정 전에 메일부터 확인한다. 아래 셋은 **DB 를 건드리지 않는다.**

```bat
pip install pywin32

:: 1. 폴더 이름 확인 — 여기서 나온 경로를 그대로 복사해 쓴다
python agents\iferr\agent.py --folders

:: 2. 메일이 읽히는지 / 키가 뽑히는지
python agents\iferr\agent.py --mails --hours 72

:: 3. 키를 못 뽑으면 본문을 저장해서 형식을 본다 (out/ 은 커밋 안 됨)
python agents\iferr\agent.py --dump 2 --hours 72
```

`--mails` 출력:

```
메일 12통 (오류로 분류 4통)

오류 판정 키워드: 오류, 에러, 실패, ERROR, FAIL

오류   수신시각              걸린키워드    키                    제목
──────────────────────────────────────────────────────────────────────
  O  2026-09-03T13:48  오류        IF_ORD_SEND          [연계오류] 주문 전송 실패
  O  2026-09-03T10:48  ERROR      못찾음                재고 연계 ERROR 발생
  .  2026-09-03T12:48  -          -                    주간 연계 처리 결과 보고
```

`걸린키워드` 컬럼이 **왜 그 메일이 오류로 잡혔는지**를 보여준다.
엉뚱한 메일이 `O` 로 찍히면 여기부터 본다.

| 증상 | 볼 곳 |
|---|---|
| Outlook 연결 실패 | Outlook 이 실행 중인가, 같은 사용자 세션인가, `pip install pywin32` |
| 폴더를 못 찾음 | `--folders` 결과에서 경로를 그대로 복사 (`받은 편지함` vs `Inbox`) |
| 메일 0통 | `--hours` 를 늘려 본다. 폴더가 맞는지 확인 |
| 오류로 분류 안 됨(`.`) | `MAIL_SUBJECT_KEYWORDS` 에 실제 제목의 단어를 추가 |
| 엉뚱한 메일이 `O` 로 잡힘 | `걸린키워드` 컬럼을 본다. 한 글자(`오`)면 쉼표를 빠뜨린 것 — `("오류",)` 처럼 쉼표를 붙일 것 |
| 키 `못찾음` | `--test-key "본문 붙여넣기"` 로 확인하고 `IFERR_KEY_PREFIXES` 를 설정 |

Outlook 없이(개발 PC에서) 확인하려면 `MAIL_BACKEND=eml` 로 `samples/mail/` 을 읽는다.

```bash
MAIL_BACKEND=eml python agents/iferr/agent.py --mails
```

## 설정할 것 세 가지

### 1. 메일 (`config_local.py`)

```python
MAIL_BACKEND = "com"                      # 로컬 Outlook (Windows + pywin32)
MAIL_FOLDER  = r"받은 편지함\인터페이스"    # Outlook 규칙으로 모아 둔 폴더

# 발신 시스템이 고정 머리말을 붙이는 경우
MAIL_SUBJECT_MATCH = "startswith"
MAIL_SUBJECT_KEYWORDS = ("(EAA) Alert Mail",)
```

**비교 방식** (`MAIL_SUBJECT_MATCH`) — 대소문자는 어느 모드에서나 무시한다.

| 모드 | 언제 |
|---|---|
| `contains` (기본) | 제목 어디에든 그 단어가 있으면 |
| `startswith` | 제목이 그 문구로 **시작**할 때만 |
| `regex` | 정규식 |

`startswith` 를 쓰면 이렇게 갈린다.

```
O  (EAA) Alert Mail - 주문 연계 실패          ← 원본 알림
.  RE: FW: (EAA) Alert Mail - 재고 연계 실패   ← 사람이 주고받은 사본
.  문의: (EAA) Alert Mail 설정 관련            ← 알림이 아니다
```

**제목을 있는 그대로 비교한다.** `RE:` `FW:` 가 붙은 것은 시스템이 보낸
원본이 아니라 사람이 주고받은 사본이라 대개 중복이므로 제외한다.

전달·회신분까지 봐야 하면 머리말을 설정한다. 여러 번 붙어 있어도 전부 뗀다.

```python
MAIL_SUBJECT_STRIP_PREFIXES = ("RE:", "FW:", "FWD:", "회신:", "전달:")
```

이때 걸린 건에는 `(전달)` 표시가 붙어 원본과 구별된다. 머리말을 떼도
키워드가 없는 메일(`FW: 전혀 다른 제목`)은 걸리지 않는다.

설정이 조용히 잘못되면 "오류 메일이 하나도 없다"로 보인다. `--mails` 는
실행할 때마다 설정을 점검해 문제를 함께 출력한다(빈 키워드, 한 글자 키워드,
잘못된 정규식, 오타 난 모드).

`pip install pywin32` 가 필요하다. Outlook 규칙으로 오류 메일만 하위 폴더에
모아 두면 스캔 범위가 줄어 훨씬 빠르다.

리눅스 개발 PC 나 테스트에서는 `MAIL_BACKEND=eml` 로 `samples/mail/` 의
`.eml` 파일을 읽는다.

### 2. 키 추출 (`config_local.py`)

ID 가 **"고정 접두어 + 숫자"** 형태면 접두어만 적으면 된다.
정규식은 경계 처리까지 포함해 자동으로 만들어진다.

```python
IFERR_KEY_PREFIXES = ("EAIIF",)      # EAIIF0001234 를 키로 뽑는다
```

```
EAIIF0001234              →  뽑는다
[EAIIF0009999] 주문 오류   →  뽑는다
EAIIF0001234_TMP          →  안 뽑는다 (다른 토큰이다)
xEAIIF123                 →  안 뽑는다
EAIIF (숫자 없음)          →  안 뽑는다
```

`_TMP` 가 붙은 경우 **잘린 키(`EAIIF000123`)를 만들지 않는 것**이 중요하다.
잘린 키로 DB 를 조회하면 없는 행을 찾거나 엉뚱한 행을 집는다 — 못 찾는 것보다 나쁘다.

형태가 다르면 정규식을 직접 넣는다. `(규칙이름, 정규식)` 이며 **그룹 1이 키**다.

```python
IFERR_KEY_PATTERNS = (
    ("our-format", r"연계번호\s*[:=]\s*([A-Z0-9]{8,})"),
)
```

**바로 확인하는 법** — 메일 제목이나 본문을 붙여넣으면 된다.

```bat
python agents\iferr\agent.py --test-key "(EAA) Alert Mail - EAIIF0001234 전송 실패"
```
```
키 접두어: EAIIF
패턴 5개

  EAIIF0001234             (규칙 prefix-eaiif)
    근거: (EAA) Alert Mail - EAIIF0001234 전송 실패
```

### DB 점검 — 스키마를 모를 때

```bat
:: 1. 접속되는지
python agents\iferr\agent.py --check-db

:: 2. 이름으로 후보 찾기 (이름에 EAI 가 든 테이블·컬럼)
python agents\iferr\agent.py --find-column EAI

:: 3. 값으로 찾기 — 이 키가 실제로 들어 있는 테이블
python agents\iferr\agent.py --find-key EAIIF0001234
python agents\iferr\agent.py --find-key EAIIF0001234 --like EAI
```

`--find-key` 는 이름에 `IF`(기본)가 들어간 **문자형 컬럼만** 뒤진다.
전수 조사는 사내 DB 에서 몇 분씩 걸리고 부하도 크다. 못 찾으면 `--like` 를
넓힌다. 권한이 없어 못 읽은 컬럼은 "확인 필요"로 따로 알려 준다 —
조용히 넘기면 '없다'로 오해한다.

```
EAIIF0001234 를 이름에 'IF' 가 든 문자형 컬럼에서 찾는다...
  IF_HDR.IF_KEY      1건
  IF_DTL.IF_KEY      12건
```

접속 계정과 테이블 소유자가 다르면 `ORACLE_SCHEMA` 를 설정한다
(읽기 전용 계정으로 다른 스키마를 보는 경우가 흔하다).

| 증상 | 뜻 / 조치 |
|---|---|
| `DPY-3010` | thin 모드가 그 서버 버전을 지원하지 않는다(대개 12.1 미만). thick 모드로 **자동 전환해 재시도**한다. 그래도 실패하면 Oracle Client 문제다 |
| `DPI-1047` | Oracle Client 를 못 찾았다. `ORACLE_CLIENT_LIB_DIR` 에 Instant Client 폴더를 지정. 32/64비트가 파이썬과 같아야 한다 |
| `ORA-12154` | DSN(TNS) 이름을 못 찾았다. `호스트:포트/서비스명` 형식으로 적어 볼 것 |
| `ORA-01017` | 계정·비밀번호 오류 |
| `ORA-00942` | 테이블이 없거나 권한이 없다. `ORACLE_SCHEMA` 확인 |

### 3. 인터페이스 마스터 테이블 (`config_local.py`)

**테이블·컬럼 이름만 적으면 SQL 이 자동으로 만들어진다.** SQL 을 쓸 필요가 없다.
사이트마다 이름이 다르므로 코드에는 아무것도 박혀 있지 않다.

```python
IFERR_MASTER_TABLE = "IF_MST"
IFERR_MASTER_FIELDS = {
    "id":        "IFID",       # 인터페이스 ID — 필수(메일의 키와 비교)
    "src_sys":   "SRCSYS",     # 소스 시스템
    "tar_sys":   "TARSYS",     # 타겟 시스템
    "src_table": "SRCTNAME",   # 소스 테이블
    "tar_table": "TARTNAME",   # 타겟 테이블
    "sch_day":   "SCH_DAY",    # 스케줄 일자 (매일이면 *)
    "sch_h":     "SCH_H",      # 시
    "sch_m":     "SCH_M",      # 분
}
```

이렇게 만들어진다.

```sql
SELECT IFID, SRCSYS, TARSYS, SRCTNAME, TARTNAME, SCH_DAY, SCH_H, SCH_M
  FROM {schema}IF_MST
 WHERE IFID = :if_key
```

**스케줄이 여러 개면 행이 여러 개로 나온다.** `IFID` 로 묶어 한 건으로 만들고
스케줄만 한 줄로 합친다. 묶지 않으면 같은 인터페이스가 여러 건으로 보여
"몇 개가 깨진 거지"를 셀 수 없다.

```
IFID          SRCSYS  ...  SCH_DAY  SCH_H  SCH_M
EAIIF0001234  SAP     ...  *        8      30
EAIIF0001234  SAP     ...  *        12     0        →  1건으로 묶임
EAIIF0001234  SAP     ...  *        18     0
```
```
EAIIF0001234: found — SAP.ZORDER_OUT → ERP.IF_ORDER_TMP (매일 08:30, 12:00, 18:00)
```

| 원본 | 표시 |
|---|---|
| `*` / 8 / 30 | `매일 08:30` |
| 5 / 3 / 15 | `5일 03:15` |
| 둘 다 있으면 | `매일 08:30 / 5일 03:15` |
| 시·분이 숫자가 아니면 | 원문 그대로 (`매일 *:*`) |

없는 컬럼은 비워 두면 `SELECT` 목록에서 빠진다. `id` 만 필수다.
`{schema}` 는 `ORACLE_SCHEMA` 로 치환된다 — 접속 계정과 테이블 소유자가
달라도 손댈 곳이 없다.

메일에서 뽑은 `IFID` 로 이 행을 찾으면 **그 인터페이스가 무엇을 어디로
나르는지**가 나온다. 실패했다는 것은 곧 타겟 테이블에 데이터가 들어가지
않았다는 뜻이므로, 이 경로가 영향 범위다.

```
EAIIF0001234: found — SAP.ZORDER_OUT → ERP.IF_ORDER_TMP / header 1건
```

이름은 SQL 문자열에 들어가므로(바인드로 넘길 수 없다) `^[A-Za-z][A-Za-z0-9_$#]{0,29}$`
로 검증한다. 이상한 이름은 기동 시 stderr 로 알리고 SQL 을 만들지 않는다.

자동 생성으로 부족하면 `IFERR_SQL` 에 직접 쓴다(이쪽이 우선).
`detail` / `impact` 는 비어 있다 — 전송 이력이나 실패 건수를 담은 테이블이
있으면 채운다.

#### 조회 SQL 직접 쓰기

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
