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
비용 때문에 앞쪽 `IMPACT_MAX_STATEMENTS`(기본 8)건만 본다. 로컬 모델은
문장 하나에 수 초가 걸려 수십 건을 돌리면 챗봇이 멎은 것처럼 보인다.

## MCP

| 툴 | 등급 | 무엇 |
|---|---|---|
| `analyze_table_impact(table, root)` | combo | 읽기/쓰기 건수와 위치 |
| 리소스 `impact://detail/{root}/{table}` | | SQL 전문과 판정 근거 |

`check_interface_errors` 결과에 타겟 테이블이 있으면 이 툴을 쓰라는 안내가
붙는다. 소형 모델은 툴을 이어서 부르지 못하므로 사용자가 다음 질문을 할 수
있게 대상 테이블을 짚어 준다.
