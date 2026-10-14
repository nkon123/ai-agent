# impact (테이블 영향도 조사)

인터페이스가 실패해 어떤 테이블이 안 채워졌을 때, **그 테이블을 쓰는
프로그램이 무엇인지** 찾는다. 사람이 하던 조사 순서를 그대로 따른다.

```
테이블 이름 → 소스에서 찾기 → 그 줄이 속한 SQL 문장을 통째로 잘라내기
→ 읽기/쓰기 판정 → (선택) LLM 이 실제 사용인지 확인
```

## 실행

```bash
python agents/impact/agent.py IF_ORDER_TMP --root SAMPLE --sql
python agents/impact/agent.py IF_ORDER_TMP --detail summary
USE_LLM=false python agents/impact/agent.py IF_ORDER_TMP   # 규칙만
```

```
── samples/src/ord_batch.prc:4~7 [insert/read] 규칙=from
  INSERT INTO ORD_HDR (ORD_NO, TOTAL_AMT)
  SELECT ORD_NO, TOTAL_AMT
    FROM IF_ORDER_TMP
   WHERE STATUS = 'N';            ← INSERT 문이지만 IF_ORDER_TMP 는 '읽기'다

── samples/src/order_query.xml:5~9 [select/read] 규칙=from
  <select id="selectOrderList" resultType="map">
    SELECT ORD_NO, TOTAL_AMT
      FROM IF_ORDER_TMP
     WHERE STATUS = #{status}
  </select>                        ← 세미콜론이 없다. 태그로 경계를 잡는다

── samples/src/order_load.pc:8~11 [select/read] 규칙=from
        SELECT ORD_NO, TOTAL_AMT
          INTO :ord_no, :amt
          FROM IF_ORDER_TMP
         WHERE STATUS = 'N';

── samples/src/order_load.pc:18~18 [delete/write] 규칙=delete-from
        DELETE FROM IF_ORDER_TMP WHERE ORD_NO = :ord_no;

[영향] 쓰기 2건 / 읽기 2건 / 판정불가 0건
```

## 소스 루트 — 설정한 루트는 모두 찾는다

`--root`(툴의 `root`)를 비우면 `SOURCE_ROOTS` 에 적은 루트 **전부**를 찾는다.
예전에는 첫 번째 루트만 봐서 두 번째 루트에만 있는 사용처를 '없음'으로 답했다.
테이블 영향도·컬럼 영향도·사용처 찾기(usage)가 같은 함수(`agents/usage/agent.py`
의 `resolve_roots`·`scan_roots`)를 쓴다.

- `--root MES` 는 그 루트만, `--root ERP,MES` 는 여러 개. 이름의 대소문자는 가리지 않는다.
- 결과 줄마다 `root` 가 붙는다. 컬럼 영향도 그리드에는 '루트' 칸이 있다.
- 한 루트의 경로가 틀려 파일이 없으면 나머지 결과는 그대로 내고 그 루트만 '확인 필요'로
  알린다 — 조용히 빠지면 '사용처 없음'으로 오해한다.
- 루트가 겹치면(한 루트가 다른 루트 안) 같은 파일을 한 번만 센다.
- 파일 상한(`MAX_FILES` 3000)은 루트마다 따로 걸리고, 걸리면 경고한다.

## 왜 문장 단위인가

테이블 이름이 나온 **줄 하나만 보면 읽는지 쓰는지 알 수 없다.**
`FROM` 절 한 줄, `INSERT` 한 줄만 보고 판단하면 반드시 틀린다.

```sql
INSERT INTO ORD_HDR
SELECT * FROM IF_ORDER_TMP;    -- IF_ORDER_TMP 는 '읽기'다
```

문장 종류(INSERT)만 보고 쓰기로 판정하면 틀린다. 그래서 앞뒤로 확장해
**완결된 문장**을 잘라낸 뒤, 테이블이 어느 자리에 나왔는지로 판정한다.

| 자리 | 판정 |
|---|---|
| `INSERT INTO T` / `UPDATE T` / `DELETE FROM T` / `MERGE INTO T` | write |
| `FROM T` / `JOIN T` / `USING T` | read |
| 이름만 있고 자리를 못 정함 | **unknown** (‘아니다’가 아니다) |

## 소스 유형

| 확장자 | 언어 | 문장 경계 | 문자열 |
|---|---|---|---|
| `.pc` | Pro\*C | 세미콜론 | 지운다 (SQL 은 코드에 박혀 있다) |
| `.prc` | 프로시저(PL/SQL) | 세미콜론 | 지운다 |
| `.xml` | UI 쿼리 | **태그** (`<select>…</select>`) | 작은따옴표만 지운다 |
| `.java` `.js` | | 세미콜론 | **안 지운다** (SQL 이 문자열 안에 있다) |

`config.SOURCE_LANG_BY_SUFFIX` 로 바꾼다. 여기 없는 확장자는 아예 스캔하지
않는다 — '무엇을 안 보는지'가 명시되어야 결과가 비었을 때 원인을 안다.

### UI 쿼리 XML

SQL 에 세미콜론이 없는 경우가 대부분이라 세미콜론만 찾으면 파일 전체를 한
덩어리로 물고 온다. 그래서 `<select>` `<insert>` `<update>` `<delete>`
`<merge>` `<sql>` `<statement>` `<query>` 태그로 경계를 잡는다.

판정 전에는 태그를 지운다. `<select id="...">` 의 `select` 를 SQL 키워드로
보면 `<update>` 안의 SELECT 를 update 로 잘못 분류한다. `<if test="...">`
같은 동적 태그도 함께 사라진다.

## 대소문자

**테이블 이름은 대소문자를 가리지 않는다.** SQL 식별자 규칙이 그렇고,
소스마다 표기가 달라(`FROM if_order_tmp` / `FROM IF_ORDER_TMP`) 가리면
통째로 놓친다. 찾기와 읽기/쓰기 판정 양쪽 모두 무시한다.

`usage` 는 반대로 **가린다** — 코드의 식별자는 `TOTAL_AMT` 와 `total_amt` 가
서로 다른 것이기 때문이다. 대신 못 찾았을 때 대소문자만 다른 것이 있으면
`used="unknown"` 과 함께 알려 준다. '없다'와 '확인 필요'는 다른 값이다.

```bash
python agents/usage/agent.py IF_ORDER_TMP -i    # 대소문자 무시하고 찾기
```

## 문장 경계 찾기

주석과 문자열을 지운 사본에서 세미콜론을 찾는다. 원문에는 주석 안이나
문자열 안에도 `;` 가 흔해 그대로 세면 엉뚱한 곳에서 끊긴다.
`strip_comments` 가 줄 수를 보존하므로, 지운 사본에서 찾은 줄 번호를
원문에 그대로 대응시켜 원문을 잘라낼 수 있다.

**Java·JS 는 문자열을 지우지 않는다.** SQL 이 문자열 안에 있어서 지우면
SQL 자체가 사라진다. Pro\*C·PL/SQL 은 코드에 그대로 박혀 있어 지워도 남는다.

세미콜론을 못 찾으면 `complete=false` 로 표시하고 "확인 필요"에 남긴다 —
잘린 조각으로 판단하면 엉뚱한 결론이 난다. 세미콜론이 없는 파일에서 파일
전체를 한 문장으로 물고 오지 않도록 길이 상한(기본 120줄)도 둔다.

## LLM 확인 (`USE_LLM=true`)

규칙이 판정한 뒤, LLM 에게 문장을 보여 주고 "이게 정말 그 테이블을
쓰는가"를 확인시킨다. **LLM 은 규칙 판정을 뒤집지 않는다.** 다르게 보면
`conflict` 로 표시하고 "확인 필요"에 남긴다 — 조용히 한쪽을 고르면 오판을
영영 못 찾는다.

`json_schema` 구조화 출력을 쓴다(소형 모델은 tool calling 이 자주 깨진다).

**사고 과정은 기본으로 꺼져 있다**(`config.AGENT_REASONING`). 문장당 40초가
2~3초로 줄어든다. 대신 3건짜리 표본에서 정확도가 3/3 → 1/3 이었다.
규칙 판정은 그대로이므로 결과가 뒤집히지는 않지만, LLM 재확인이 중요하면 켠다.

```bash
AGENT_REASONING=true python agents/impact/agent.py IF_ORDER_TMP
```

비용 때문에 앞쪽 `IMPACT_MAX_STATEMENTS`(기본 8)건만 본다. 로컬 모델은
문장 하나에 수 초가 걸려 수십 건을 돌리면 챗봇이 멎은 것처럼 보인다.

## 컬럼 영향도 — `analyze_column_impact` (`column.py`)

테이블 영향도에서 한 단계 더 들어가 **"그 프로그램에서 이 컬럼을 쓰나"** 를 본다.

```
테이블이 나오는 파일 → 그 파일에서 컬럼이 나온 줄 전부 + 컬럼 없이 테이블만 쓰는 문장
→ 규칙 1차 분류(주석 / 문자열 / 테이블과 같은 문장 / 다른 코드 / 테이블만 씀)
→ 줄마다 LLM 판정 → 그리드
```

```bash
python agents/impact/column.py IF_ORDER_TMP TOTAL_AMT --root SAMPLE
USE_LLM=false python agents/impact/column.py IF_ORDER_TMP TOTAL_AMT   # 규칙만
```

- **찾은 줄은 판정과 상관없이 전부 남긴다.** 어느 프로그램에서 이름이 나왔는지는 사람이
  다 봐야 하고, 실제 영향 여부만 판정으로 가른다. 주석에만 테이블이 나오는 파일은 뺀다.
- **테이블을 쓰는 문장 중 컬럼이 없는 문장도 남긴다**(위치 `테이블만 씀(컬럼 없음)`).
  테이블 영향도에는 나오는데 컬럼 영향도에서 빠지면 안 본 건지 안 쓰는 건지 알 수 없었다
  (사용자 지적). 문장은 테이블 영향도와 같은 방식으로 자르고, 규칙으로만 판정한다(LLM 안 씀):

  | 문장 | 판정 | 규칙 |
  |---|---|---|
  | `INSERT INTO T VALUES …` / `INSERT INTO T SELECT …` (컬럼 목록 없음) | 영향 있음 | `insert-all-columns` — 모든 컬럼에 넣는다 |
  | `SELECT * FROM T` / `SELECT A.* … FROM T A` | 확인 필요 | `select-star` — 읽은 값을 쓰는지 모른다 |
  | 그 밖(`UPDATE T SET 다른컬럼`, `DELETE`, `COUNT(*)`, 다른 테이블의 `B.*`) | 영향 없음 | `column-absent` |

  답과 그리드 부제는 '컬럼이 나온 줄 N개 + 테이블만 쓰는 문장 M개'로 나눠 센다. 컬럼이 한 줄도
  없어도 '없다'로 끝내지 않고 테이블 문장을 보인다.
- 판정은 `영향 있음` / `영향 없음` / `확인 필요` 세 값이다. 판정 불가·LLM 미확인·LLM 실패는
  `확인 필요`다 — '아니다'로 처리하지 않는다.
- 주석은 규칙만으로 `영향 없음`. 나머지는 **줄마다** LLM 이 본다. 같은 문장이라도 줄마다
  답이 다르다:
  ```sql
  INSERT INTO ORD_HDR (ORD_NO, TOTAL_AMT)   -- ORD_HDR 의 컬럼 → 영향 없음
  SELECT T.ORD_NO, T.TOTAL_AMT               -- IF_ORDER_TMP 의 컬럼 → 영향 있음
    FROM IF_ORDER_TMP T
  ```
- **LLM 에게 yes/no 를 묻지 않는다.** "이 이름이 무엇인가(column/variable/text)" 와
  "어느 테이블의 컬럼인가" 를 묻고, 테이블 이름 비교는 코드가 한다. yes/no 를 바로 물으면
  근거에는 'ORD_HDR 의 컬럼'이라 쓰고 답은 yes 를 냈다(아래 실측 13/18 → 18/18).
- 여기서는 **LLM 판정이 최종**이다(테이블 영향도와 반대). 같은 이름 컬럼이 여러 테이블에
  흔해 규칙으로는 가를 수 없다. 대신 규칙 추정을 그리드에 함께 남기고, 갈리면 `≠ LLM` 을 붙인다.
- 테이블과 같은 문장부터 판정한다. `COLUMN_IMPACT_MAX_LLM`(기본 40줄)을 넘는 줄은 버리지
  않고 '규칙 추정 + 확인 필요'로 남긴다. 그리드는 `COLUMN_IMPACT_MAX_HITS`(기본 2000줄)까지.
- 결과는 30분 캐시한다 — 챗봇 답 뒤에 [표로 보기] 를 누르면 같은 조사를 다시 하지 않는다.
  **LLM 이 실패한 결과는 캐시하지 않는다**(Ollama 가 잠깐 죽은 결과가 30분 남으면 안 된다).
  처음부터 연달아 두 번 실패하면 연결 문제로 보고 나머지를 기다리지 않는다.
- 아직 못 하는 것: MyBatis `resultMap` 의 `totalAmt` 처럼 이름이 바뀐 매핑, `SELECT *`.
  이름이 그대로 나온 줄만 찾는다.

실측 (`qwen3.5:4b`, 사고 끔, `samples/colimpact` + `samples/src` 18줄, 2회):

| 묻는 방식 | 정확도 | 시간 |
|---|---|---|
| 문장째 묶어 yes/no | 같은 문장의 줄이 같은 답 — 설계 결함 | |
| 줄마다 yes/no | 13/18 (근거와 답이 어긋남) | 10초 |
| 줄마다 '어느 테이블의 컬럼인가' → 코드가 비교 | **18/18, 18/18** | 9초 |

### 화면 — [표로 보기]

`view="grid"` 로 등록한 툴의 답 아래에 **[표로 보기]** 가 붙는다. 누르면 팝업 그리드가 뜨고
`detail_uri` 리소스(`impact://column/{table}/{column}/{root}`)를 읽는다. 모델이 넣지 않은
인자(`root`)는 `-` 로 채우고 리소스가 기본 루트로 본다.

- 판정별 걸러 보기, 검색
- **엑셀에 붙여넣기용 복사** — 탭 구분. 엑셀에 그대로 붙는다. `http://서버이름:포트` 로
  접속하면 브라우저가 클립보드 API 를 막으므로 예전 방식으로 떨어진다
- **CSV 저장** — 엑셀에서 한글이 안 깨지게 BOM 을 붙인다
- 복사·저장은 지금 걸러 보고 있는 줄만 담는다

리소스는 `{title, subtitle, grid: {columns, rows, filter_key, tones}, warnings}` 를 준다.
화면은 어떤 툴인지 모르고 이 모양만 그린다 — 다른 툴도 같은 모양을 주면 그리드가 붙는다.

## MCP

| 툴 | 등급 | 무엇 |
|---|---|---|
| `analyze_table_impact(table, root)` | combo | 읽기/쓰기 건수와 위치 |
| `analyze_column_impact(table, column, root)` | combo | 컬럼이 나온 줄과 판정 요약 |
| 리소스 `impact://detail/{root}/{table}` | | SQL 전문과 판정 근거 |
| 리소스 `impact://column/{table}/{column}/{root}` | | 컬럼 영향도 그리드 (root 가 `-` 면 기본 루트) |

툴 선택 실측(`qwen3.5:4b`, 2회씩 26/26): "X 테이블 Y 컬럼 영향도 조사해줘",
"X 테이블의 Y 컬럼 어디서 쓰여?", "X.Y 컬럼 쓰는 프로그램 찾아줘",
"X 테이블 Y 컬럼 바꾸면 영향 있는 곳" 은 이 툴로, 테이블만 묻는 질문·변수 사용처·
인터페이스 질문은 원래 툴로 갔다.

`check_interface_errors` 결과에 타겟 테이블이 있으면 이 툴을 쓰라는 안내가
붙는다. 소형 모델은 툴을 이어서 부르지 못하므로 사용자가 다음 질문을 할 수
있게 대상 테이블을 짚어 준다.
