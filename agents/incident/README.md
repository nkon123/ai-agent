# incident (인터페이스 오류 → 원인 컬럼 → 영향도)

"오늘 인터페이스 오류 확인해줘" 한마디로 사람이 하던 조사를 끝까지 잇는다.

```
오류 메일 수신 ─ IFID 추출 ─ 마스터에서 테이블(출발 → 도착)            iferr
             └ 메일의 오류 내용에서 원인 컬럼(ORA-12899 길이 초과 등)   iferr.extract_issues
                 └ 그 테이블·컬럼을 쓰는 소스, 줄마다 영향 판정          impact.column
             └ 원인 컬럼이 없으면(timeout 등) 도착 테이블을 쓰는 SQL 문장  impact.agent
                     └ 챗봇 답 + [표로 보기] 그리드(엑셀 복사·CSV·소스 보기)
```

로컬 소형 모델은 툴을 이어 부르지 못한다. 이어 붙이는 일은 모델이 아니라 이 에이전트가
한다. 챗봇에서는 `check_interface_errors` 가 이 에이전트를 부른다. 각 단계는 원래
에이전트 함수를 그대로 쓰므로 단계별 툴로 따로 물어도 같은 답이 나온다.

```bash
python agents/incident/agent.py --period 오늘
python agents/incident/agent.py --key IF_ORD_RECV      # 메일 없이 IFID 만 (원인 컬럼은 메일에서 오므로 없음)
python agents/incident/agent.py --subject "[연계오류] 주문 수신"   # 제목으로 메일 한 통 (기본 최근 7일)
```

챗봇에서는 `"[연계오류] 주문 수신 인터페이스 처리 실패" 메일 오류 확인해줘` 처럼 메일 한 통을
집어 물을 수 있다. 답은 어느 메일(제목·수신 시각)을 봤는지부터 밝힌다. 규칙은
`agents/iferr/README.md` 의 '메일 한 통 지정'. 실측: 데모 메일로 2.9초, 17줄 판정은 기간으로
물었을 때와 같다.

그리드에는 컬럼이 나온 줄(17줄) 말고도 `IF_ORDER_TMP` 를 쓰지만 `ORD_NO` 가 없는 문장
(`OrdDao.java` 의 `SUM(TOTAL_AMT)`, `UPDATE IF_ORDER_TMP SET STATUS`)이 '테이블만 씀'으로 함께 나온다.
컬럼 목록 없는 `INSERT INTO IF_ORDER_TMP VALUES` 가 있으면 그것이 '영향 있음'으로 잡힌다 —
길이 초과를 일으킨 바로 그 프로그램일 수 있다. 규칙은 `agents/impact/README.md` 컬럼 영향도.

## 원인 컬럼과 테이블

| 메일 내용 | 뽑는 방법 | 결과 |
|---|---|---|
| `ORA-12899: value too large for column "ERP"."IF_ORDER_TMP"."ORD_NO" (actual: 25, maximum: 20)` | 규칙(영문·한글 메시지) | `IF_ORDER_TMP.ORD_NO 길이 초과(실제 25 > 최대 20)` |
| `ORA-01400: cannot insert NULL into ("ERP"."ORD_HDR"."CUST_CD")` | 규칙 | `ORD_HDR.CUST_CD NULL 불가` |
| `ORA-01438` / `ORA-01401` (컬럼 이름 없음) | 규칙 | '컬럼 미상' — 컬럼 영향도 대신 **도착 테이블 영향도** |
| "주문번호(ORD_NO) 체계가 20자리 → 25자리로…" 처럼 사람이 쓴 글 | 컬럼은 메일 판정 LLM, 문제 종류는 그 줄의 말로 규칙 | `ORD_NO 길이 초과(실제 25 > 최대 20)` — 메일에 **실제로 적힌** 이름만 쓴다 |
| "IF_ORDER_TMP 의 TOTAL_AMT 에 소수점이 들어와…" | 규칙이 못 가르면 LLM 요약 | `IF_ORDER_TMP.TOTAL_AMT 소수점 포함 변환 불가` (테이블 출처: 메일 본문) |

답에는 원인 아래 `메일 오류 내용:` 으로 메일 원문 줄이, 그리드에는 부제 맨 앞 `원인:` 과
`메일 오류 내용` 칸이 나온다 — 요약이 맞는지 원문과 대 볼 수 있게.

영향도를 조사할 테이블은 ① 오류 메시지에 적힌 테이블, ② 없으면 마스터의 도착 테이블,
③ 둘 다 없거나 도착 테이블이 여럿이면 **조사하지 않고 확인 필요**로 남긴다 — 테이블을
지어내지 않는다.

오류 메시지가 테이블·컬럼이라고 밝힌 이름은 인터페이스 키에서 뺀다. `IF_` 로 시작하는
인터페이스 테이블(`IF_ORDER_TMP`)이 `if-prefix` 규칙에 키로 잡혀 없는 IFID 를 조회했다(실측).

## 원인 컬럼이 없는 오류 — 도착 테이블 영향도

timeout 처럼 메일에 컬럼 오류가 없거나, 컬럼 이름이 없는 오류(ORA-01438)는 마스터의 **도착 테이블**을
쓰는 SQL 문장을 전부 찾는다(`analyze_table_impact` 와 같은 `run_impact`). 예전에는 "이 테이블은
analyze_table_impact 로 확인" 안내에서 끝나, 원인 컬럼이 없는 오류만 영향도가 비었다(사용자 지적).

판정은 '데이터가 안 들어왔다' 는 관점이다.

| 문장 | 판정 | 근거 |
|---|---|---|
| 그 테이블을 **읽는** 문장 (SELECT, MERGE 의 USING 등) | 영향 있음 | 데이터가 안 들어오면 빈 결과·누락으로 처리된다 |
| 그 테이블에 **쓰는** 문장 | 확인 필요 | 적재 프로그램 자신이거나 다른 경로로 채우는 곳일 수 있다 |
| 읽기·쓰기를 못 정함, 규칙과 LLM 이 갈림 | 확인 필요 | |

- 데모: `IF_STK_RECV`(재고 timeout) → `IF_STOCK_TMP` 문장 3개 — `stock_query.xml:7` 조회·`stock_sync.prc:6`
  병합(읽기, 영향 있음), `stock_sync.prc:13` 수정(쓰기, 확인 필요). 데모 소스는 `samples/src/stock_*`(가짜 값).
- 그리드 행의 원인은 `IF_STOCK_TMP 미적재 (원인 컬럼 없음)`, 위치는 `테이블 영향도`. 소스 보기는 테이블 이름을 강조한다.
- 컬럼 영향도와 같은 상한(`INCIDENT_MAX_IMPACTS`)을 나눠 쓴다. 문장 확인 LLM 은 앞 `IMPACT_MAX_STATEMENTS`(3)문장.
- 도착 테이블을 마스터에서 못 찾으면 조사하지 않고 확인 필요로 알린다(테이블을 지어내지 않는다).
- 실측(`qwen3.5:4b`, 데모 메일 3통): 두 오류 모두 영향도까지 9.8초. 테이블 문장 LLM 확인 3건이 약 1.4초.

## 답 모양

`check_interface_errors` 는 `answer="tool"` — 툴 결과가 그대로 답이다(모델이 다시 쓰지 않는다,
README '툴 결과를 그대로 답으로'). 케이스마다 굵은 머리 줄과 하위 목록이다:

```
**IF_ORD_RECV** · 확인됨 — [SAP] ZORDER_OUT → [ERP] IF_ORDER_TMP (매일 08:30, 12:00)
- 원인: **IF_ORDER_TMP.ORD_NO 길이 초과(실제 25 > 최대 20)** — 테이블은 오류 메시지에서
- 메일 오류 내용: `오류내용 : ORA-12899: value too large for column ...`
- 영향도: 영향 있음 **11** / 확인 필요 2 / 영향 없음 7 — ... 이 나온 줄 17개 + 테이블만 쓰는 문장 3개 (...)
- 영향 있는 곳:
  - `[ERP] ord_join.sql:5` 조회 — `SELECT T.ORD_NO, T.TOTAL_AMT`
  - 외 6곳
- 근거: 메일 1통, rows-found / 마스터: 파일

**IF_STK_RECV** · 확인됨 — [WMS] STOCK_OUT → [ERP] IF_STOCK_TMP (매일 06:00)
- 원인 컬럼: 메일에 적힌 컬럼 오류가 없다 — 도착 테이블을 쓰는 프로그램을 조사했다
- 영향도: 영향 있음 **2** / 확인 필요 1 — IF_STOCK_TMP 을(를) 쓰는 문장 3개 (읽기 2 · 쓰기 1, ...)
- 영향 있는 곳 (읽는 곳):
  - `stock_query.xml:7` 조회 — `FROM IF_STOCK_TMP`
- 확인 필요한 곳 (쓰는 곳·판정 못 함):
  - `stock_sync.prc:13` 수정 — `UPDATE IF_STOCK_TMP`
```

## 상한과 시간

- `INCIDENT_MAX_IMPACTS`(기본 2): 한 번에 이어서 조사하는 영향도 수(원인 컬럼 + 도착 테이블). 넘으면 확인 필요로 알린다.
- 컬럼 영향도 한 건은 LLM 최대 `COLUMN_IMPACT_MAX_LLM`(40)줄. 규칙으로 확정되는 줄은
  LLM 에 보내지 않는다(아래).
- 툴은 늘 새로 조사한다(장애 대응 중에는 상태가 바뀐다). [표로 보기] 는 방금 조사한
  결과를 30분 들고 있다가 쓴다.

## 컬럼 판정 — 규칙으로 정할 수 있는 것은 규칙으로

실측(`ORD_NO`, 17줄)에서 LLM 이 틀린 3줄이 전부 규칙으로 정확히 정해지는 줄이었다.

| 줄 | 규칙 | 판정 |
|---|---|---|
| `INTO :ord_no` / `#{ordNo}` | 호스트 변수 자리 | 영향 없음 |
| `SELECT T.ORD_NO … FROM IF_ORDER_TMP T` | 별칭이 가리키는 테이블 | 영향 있음 |
| `JOIN ORD_HDR B ON B.ORD_NO = A.ORD_NO` | 한 줄의 여러 별칭 중 하나라도 그 테이블 | 영향 있음 |
| `INSERT INTO ORD_HDR (ORD_NO, …)` | INSERT 대상의 컬럼 목록 | 영향 없음 |
| `INSERT INTO ORD_HDR … SELECT ORD_NO FROM IF_ORDER_TMP` | 대상을 빼면 읽는 테이블이 하나 | 영향 있음 |
| `SELECT ORD_NO FROM IF_ORDER_TMP` | 문장의 테이블이 하나 | 영향 있음 |

남은 줄(테이블이 없는 문장, 여러 테이블에 별칭 없는 컬럼, 문자열)만 LLM 이 '어느 테이블의
컬럼인가'를 답한다. 쓰인 자리(조회·조건·수정·조인)는 SQL 절로 정한다.

## 실측 (`qwen3.5:4b`, `CHAT_REASONING=false`, 데모 메일 3통)

```
[0.7s] 인터페이스 오류 확인 실행 중
[0.9~1.9s] 메일 판정 3통
[2.6~3.0s] 컬럼 판정 2줄 (나머지 15줄은 규칙으로 확정)
[3.6s] 완료 — 오류 2건, 원인 컬럼 1개, 17줄 (영향 있음 11 / 확인 필요 0 / 영향 없음 6), 17/17 정답
```

| 항목 | 결과 |
|---|---|
| 컬럼 판정 평가셋(18줄) | 18/18 ×2, 4초 (규칙 보강 전 9초) |
| 툴 선택(15질문 ×2) | 30/30 — "오늘 인터페이스 오류 확인해줘" 는 `check_interface_errors` |
| `CHAT_REASONING` 을 비워 둠(모델 기본값) | qwen 이 사고 과정만 쓰고 **빈 답**을 냈다. 앱은 이때 툴 결과를 그대로 보여 준다. 데모는 `false` 로 둔다 |

## 발표 데모

```bat
samples\demo\run_demo.bat
```

| 무엇 | 어디 |
|---|---|
| 데모 메일 3통(오늘 날짜로 그때 생성) | `samples/demo/make_mail.py` → `out/demo_mail/` (git 제외) |
| 인터페이스 마스터(가짜) | `samples/demo/if_master.json` → `IFERR_MASTER_FILE` |
| 소스 루트 두 개 | `samples/colimpact`(ERP), `samples/src`(SAMPLE) |

발표 중에 조사를 기다리지 않으려면 미리 한 번 물어 둔다. 답과 [표로 보기] 는 저장되므로(브라우저 대화 기록 +
`out/grid_snapshots/`) 발표 때는 왼쪽 대화 목록에서 그 대화를 열어 바로 보여 줄 수 있다 — 앱을 다시 띄워도 된다.
소스 보기(줄 더블클릭)는 저장본이 아니라 소스 파일을 그때 읽으므로 그대로 동작한다.

`config_local.py` 는 건드리지 않는다. 사내 PC 에서 실제 메일·DB 로 보이려면 배치 파일 없이
평소처럼 `python app/app.py` 를 띄우면 된다(마스터는 DB, 메일은 Outlook).
