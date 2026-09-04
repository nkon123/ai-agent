# 오라클 튜닝 기준 (11g 기준)

이 문서가 판정 기준이다. 규칙 엔진과 LLM 프롬프트가 **같은 문서**를 쓴다.
기준을 코드에 흩어 놓으면 한쪽만 고쳤을 때 서로 다른 답이 나온다.

사내 기준이 다르면 `config.SQLTUNE_RULES_FILE` 로 다른 파일을 지정한다.

**대상 버전은 11g(11.2)다.** 12c 이상에서만 되는 것을 쓰면 안 된다.

| 11g 에 **없는** 것 | 대신 |
|---|---|
| `FETCH FIRST n ROWS ONLY` | `ROWNUM` 페이징 (아래 `sort-order-by`) |
| 적응형 실행계획(adaptive plan) | 계획이 실행 중에 바뀌지 않는다. 통계가 전부다 |
| 하이브리드 히스토그램 | 도수분포(frequency) / 높이균형(height-balanced) 뿐 |
| In-Memory 컬럼 저장소 | 인덱스와 파티션으로 푼다 |
| `APPROX_COUNT_DISTINCT` | 없다 |

| 11g 에 **있는** 것 | 주의 |
|---|---|
| 적응형 커서 공유(ACS) | 바인드 값에 따라 자식 커서가 여러 개 생긴다 |
| 카디널리티 피드백(11.2) | **2회차 실행에서 계획이 바뀔 수 있다** |
| 결과 캐시(result cache) | 2회차가 비현실적으로 빨라 보인다 |
| SQL 계획 관리(SPM) | 좋은 계획을 고정할 수 있다 |
| 확장 통계(extended stats) | 상관 있는 컬럼 조합의 카디널리티 오추정을 고친다 |
| 인덱스 사용 감시 | 안 쓰는 인덱스를 찾아낸다 |

---

## 1. 인덱스를 못 타게 만드는 것

### func-on-column — 인덱스 컬럼에 함수·연산

컬럼을 가공하면 그 컬럼의 일반 인덱스를 쓸 수 없다. 조건을 컬럼 쪽이
아니라 값 쪽으로 옮긴다.

```sql
-- 나쁨
WHERE TO_CHAR(REG_DT, 'YYYYMMDD') = '20260101'
-- 좋음 (하루 범위로)
WHERE REG_DT >= TO_DATE('20260101','YYYYMMDD')
  AND REG_DT <  TO_DATE('20260101','YYYYMMDD') + 1

-- 나쁨
WHERE SUBSTR(ORD_NO, 1, 4) = '2026'
-- 좋음
WHERE ORD_NO >= '2026' AND ORD_NO < '2027'
```

`REG_DT` 가 시분초를 가진 DATE 면 `TRUNC(REG_DT) = :d` 도 같은 문제다.
범위 조건으로 바꾼다.

**SQL 을 못 고치는 경우**(패키지 제품, UI 쿼리 공유 등)에는 함수 기반
인덱스를 만든다. 11g 에서 쓸 수 있다.

```sql
CREATE INDEX IX_ORD_HDR_REGDT_C ON ORD_HDR (TO_CHAR(REG_DT,'YYYYMMDD'));
-- 또는 11g 가상 컬럼 + 일반 인덱스
ALTER TABLE ORD_HDR ADD (REG_YMD AS (TO_CHAR(REG_DT,'YYYYMMDD')));
CREATE INDEX IX_ORD_HDR_REGYMD ON ORD_HDR (REG_YMD);
```

함수 기반 인덱스는 `QUERY_REWRITE_ENABLED=TRUE`(11g 기본값) 여야 하고,
SQL 의 함수 표기가 인덱스 정의와 **정확히** 같아야 쓰인다. 포맷 문자열
하나만 달라도 안 탄다.

### nvl-on-column — 컬럼에 NVL

```sql
-- 나쁨
WHERE NVL(STATUS,'N') = 'N'
-- 좋음
WHERE (STATUS = 'N' OR STATUS IS NULL)
```

주의: 단일 컬럼 B-tree 인덱스는 **NULL 을 저장하지 않는다.** 위로 바꿔도
`STATUS IS NULL` 쪽은 인덱스로 못 찾는다. NULL 도 찾아야 하면
`(STATUS, 1)` 처럼 상수를 붙인 복합 인덱스를 만들거나
`NVL(STATUS,'N')` 함수 기반 인덱스를 쓴다.

### implicit-cast — 암시적 형변환

```sql
-- CHAR_COL 이 VARCHAR2 인데 숫자와 비교
WHERE CHAR_COL = 123          -- TO_NUMBER(CHAR_COL) = 123 으로 변환된다
WHERE CHAR_COL = '123'        -- 좋음
```

오라클은 문자와 숫자를 비교할 때 **문자 쪽을 숫자로** 바꾼다. 컬럼에
함수를 씌운 것과 같아져 인덱스를 못 탄다. 실행계획의 `filter` 술어에
`TO_NUMBER(...)` 가 보이면 이 경우다.

날짜도 같다. `REG_DT = '20260101'` 은 NLS 설정에 기대는 위험한 코드다.
`TO_DATE` 로 명시한다.

### leading-wildcard — 선행 와일드카드

```sql
WHERE NAME LIKE '%김%'        -- 인덱스 못 탐
WHERE NAME LIKE '김%'         -- 탐
```

부분 검색이 꼭 필요하면 11g 에서는 Oracle Text 인덱스(`CONTEXT`)를
검토한다. 또는 뒤집은 값을 가진 컬럼을 두고 `LIKE '%자'` 를 `LIKE '자%'`
로 바꾸는 방법도 있다.

### negation — 부정 조건

```sql
WHERE STATUS != 'Y'           -- 인덱스로 걸러내지 못함
WHERE STATUS IN ('N','E','W') -- 대상 값이 적으면 이쪽
```

`NOT IN` 은 성능뿐 아니라 **정확성 문제**가 있다. 목록에 NULL 이 하나라도
있으면 결과가 0건이 된다.

```sql
-- 위험: 서브쿼리 결과에 NULL 이 있으면 한 건도 안 나온다
WHERE ORD_NO NOT IN (SELECT ORD_NO FROM CANCEL_ORD)
-- 안전 (그리고 대개 더 빠르다: ANTI JOIN)
WHERE NOT EXISTS (SELECT 1 FROM CANCEL_ORD C WHERE C.ORD_NO = O.ORD_NO)
```

### or-condition — 서로 다른 컬럼의 OR

```sql
WHERE A = :a OR B = :b
```

한 인덱스로 두 조건을 다 처리할 수 없다. 각각 인덱스가 있으면
`UNION ALL` 로 나누는 편이 빠르다(중복 제거가 필요하면 `UNION`).

```sql
SELECT ... WHERE A = :a
UNION ALL
SELECT ... WHERE B = :b AND A != :a      -- 중복 방지 조건
```

옵티마이저가 알아서 하는 경우(CONCATENATION 연산)도 있으니 계획을 먼저 본다.

### select-star — 필요 없는 컬럼까지 읽기

```sql
SELECT * FROM ORD_HDR WHERE STATUS = :s          -- 나쁨
SELECT ORD_NO, TOTAL_AMT FROM ORD_HDR WHERE ...  -- 좋음
```

필요한 컬럼만 적으면:

- **커버링 인덱스가 가능해진다.** 인덱스에 그 컬럼들이 다 있으면 테이블을
  아예 읽지 않는다(`TABLE ACCESS BY INDEX ROWID` 가 사라진다)
- LOB·LONG 컬럼을 실수로 끌고 오지 않는다
- 네트워크로 보내는 양이 준다

`SELECT *` 는 정확성 문제도 있다. 나중에 테이블에 컬럼이 추가되면
결과 구조가 조용히 바뀌어 프로그램이 깨진다.

다만 **건수를 바꾸는 변경은 아니어야 한다.** 컬럼을 줄이는 것은 결과 건수에
영향이 없지만, `DISTINCT` 가 걸린 쿼리에서 컬럼을 빼면 건수가 달라진다.

---

## 2. 접근 경로 (실행계획에서 보는 것)

### full-scan — TABLE ACCESS FULL

전체 중 일부만 필요한데 FULL 이면 인덱스를 검토한다.
**반대로 대량이면 FULL 이 정상이다** — 대략 전체의 10~20% 이상을 읽는다면
인덱스로 한 건씩 찾는 것보다 통째로 읽는 편이 빠르다. 무조건 인덱스가
답이 아니다.

FULL 이 나오는 흔한 이유:
- 조건 컬럼에 인덱스가 없다
- 위 1항(함수·형변환·부정·선행 와일드카드)에 걸렸다
- 통계가 낡아 대상 건수를 크게 잡았다
- 인덱스는 있는데 클러스터링 팩터가 나빠 옵티마이저가 포기했다

### index-full-scan — INDEX FULL SCAN / SKIP SCAN

인덱스를 처음부터 끝까지 읽고 있다. **선두 컬럼이 조건에 없다**는 신호다.
`INDEX SKIP SCAN` 은 선두 컬럼의 값 종류가 적을 때 오라클이 건너뛰며
읽는 것인데, 대개는 컬럼 순서가 잘못됐다는 뜻이다.

`INDEX FAST FULL SCAN` 은 다르다 — 인덱스만으로 답이 나올 때(커버링)
멀티블록으로 읽는 것이라 대개 좋은 신호다.

### filter-not-access — filter 술어

`Predicate Information` 에서 `access` 와 `filter` 를 구분해서 본다.

```
2 - access("ORD_NO"=:B1)
2 - filter("STATUS"='N')      ← 인덱스가 걸러내지 못하고 테이블에서 버렸다
```

`filter` 로 밀린 조건은 그만큼 헛읽은 것이다. 그 컬럼을 인덱스에 추가하면
`access` 로 올라가 읽는 양이 줄어든다.

### cartesian — MERGE JOIN CARTESIAN

조인 조건 누락이 의심된다. 거의 항상 버그다. 옵티마이저가 한쪽을 1건으로
추정했을 때도 나오므로, 조인 조건이 멀쩡하면 통계를 의심한다.

---

## 3. 조인

### nl-large-driving — NESTED LOOPS 선행 집합

NESTED LOOPS 는 **선행(driving) 집합이 작아야** 한다. 선행 1건마다 후행을
찾으므로, 선행이 10만 건이면 10만 번 찾는다.

- 소량 대 소량, 또는 소량으로 대량을 찍어 읽을 때 → NESTED LOOPS
- 대량 대 대량 → HASH JOIN
- 양쪽 다 정렬돼 있거나 정렬이 어차피 필요할 때 → SORT MERGE

후행 테이블의 조인 컬럼에는 인덱스가 있어야 한다. 없으면 매번 FULL 이다.

조인 순서가 틀렸다고 판단되면 힌트로 고정하기 전에 **통계부터 본다**.
대개 카디널리티 오추정이 원인이고, 힌트는 그 증상을 덮을 뿐이다.

```sql
-- 그래도 고정해야 한다면
SELECT /*+ LEADING(H) USE_NL(D) INDEX(D IX_ORD_DTL_ORDNO) */ ...
```

---

## 4. 정렬·집계·페이징

### sort-order-by — SORT ORDER BY

정렬은 인덱스로 없앨 수 있다. `ORDER BY` 컬럼이 인덱스 순서와 같으면
오라클은 인덱스를 따라 읽으며 정렬을 건너뛴다(계획에서 `SORT ORDER BY`
가 사라진다).

**11g 페이징은 ROWNUM 으로 쓴다.** `OFFSET/FETCH` 는 12c 부터다.

```sql
-- 상위 N 건 (인덱스 정렬을 이용하면 N 건만 읽고 끝난다)
SELECT * FROM (
    SELECT A.*, ROWNUM RN FROM (
        SELECT ORD_NO, REG_DT FROM ORD_HDR
         WHERE STATUS = :s
         ORDER BY REG_DT DESC        -- 인덱스 (STATUS, REG_DT) 가 있으면 정렬 생략
    ) A WHERE ROWNUM <= :end_row
) WHERE RN > :start_row;
```

`ROWNUM` 은 반드시 **정렬 뒤 바깥쪽**에서 건다. 같은 계층에서 걸면
정렬 전 임의의 N 건이 잘려 결과가 달라진다 — 튜닝이 아니라 버그다.

`COUNT STOPKEY` 가 계획에 보이면 상위 N 건에서 멈추고 있다는 뜻으로 좋은
신호다. 안 보이면 전체를 정렬하고 있다.

집계는 `SORT GROUP BY` 보다 `HASH GROUP BY` 가 대개 빠르다(11g 기본).
`SORT GROUP BY` 가 나오면 정렬을 강제하는 무언가가 있는지 본다.

---

## 5. 통계와 추정

### cardinality-miss — E-Rows 와 A-Rows 차이

`/*+ GATHER_PLAN_STATISTICS */` 로 실행한 뒤 `DBMS_XPLAN.DISPLAY_CURSOR`
의 `ALLSTATS LAST` 를 보면 추정(E-Rows)과 실제(A-Rows)가 나란히 나온다.
**10배 이상 벌어지면** 통계 문제다. 추정이 틀리면 조인 방식과 순서가
통째로 틀어진다.

```sql
SELECT /*+ GATHER_PLAN_STATISTICS */ ... ;
SELECT * FROM TABLE(DBMS_XPLAN.DISPLAY_CURSOR(NULL,NULL,'ALLSTATS LAST'));
```

(`V$SQL` 계열 조회 권한이 필요하다. 없으면 DBA 에게 요청한다.)

통계 수집(11g):

```sql
BEGIN
  DBMS_STATS.GATHER_TABLE_STATS(
    OWNNAME    => 'ERP',
    TABNAME    => 'ORD_HDR',
    ESTIMATE_PERCENT => DBMS_STATS.AUTO_SAMPLE_SIZE,  -- 11g 는 이게 정확하고 빠르다
    METHOD_OPT => 'FOR ALL COLUMNS SIZE AUTO',
    CASCADE    => TRUE);                              -- 인덱스 통계까지
END;
```

- 11g 의 `AUTO_SAMPLE_SIZE` 는 10g 와 달리 해시 기반이라 정확하다.
  `ESTIMATE_PERCENT` 를 직접 주지 않는 편이 낫다.
- 통계는 변경분이 10% 를 넘으면 낡은 것으로 표시된다
  (`DBA_TAB_MODIFICATIONS`, `USER_TAB_STATISTICS.STALE_STATS`).
- **컬럼 사이에 상관관계가 있으면** 확장 통계를 만든다. 예를 들어
  시도명과 시군구는 독립이 아닌데 옵티마이저는 독립으로 보고 곱해서
  건수를 과소평가한다.

```sql
SELECT DBMS_STATS.CREATE_EXTENDED_STATS('ERP','ADDR','(SIDO,SIGUNGU)') FROM DUAL;
```

히스토그램은 값이 **편중된** 컬럼에만 의미가 있다. 11g 에는 도수분포와
높이균형 두 종류뿐이고(하이브리드는 12c), 값 종류가 254 개를 넘으면
높이균형이라 정확도가 떨어진다.

---

## 6. 바인드 변수

### literal-value — 리터럴과 하드파싱

반복 수행되는 SQL 에 리터럴을 쓰면 값마다 다른 SQL 로 취급되어 매번
하드파싱하고 공유풀을 채운다. 바인드로 바꾼다.

반대로 **한 번만 도는 배치**나 값 분포가 심하게 편중된 조건에서는
리터럴이 유리할 수 있다. 옵티마이저가 실제 값을 보고 계획을 세우기 때문이다.

11g 의 바인드 관련 동작:

- **바인드 피킹**: 첫 실행의 바인드 값으로 계획을 만들고 재사용한다.
  첫 값이 특이하면 이후 모든 실행이 나쁜 계획을 쓴다.
- **적응형 커서 공유(ACS)**: 11g 는 값에 따라 선택도가 크게 다르면
  자식 커서를 따로 만든다. `V$SQL.IS_BIND_SENSITIVE` /
  `IS_BIND_AWARE` 로 확인한다. 다만 **처음 몇 번은 나쁜 계획을 쓴 뒤에야**
  학습하므로, 그 사이의 느림을 버그로 오해하기 쉽다.
- `CURSOR_SHARING=FORCE` 는 최후의 수단이다. 리터럴을 바인드로 바꿔치기
  하지만 계획 품질이 떨어질 수 있다. 애플리케이션을 고칠 수 있으면 고친다.

---

## 7. 인덱스 설계

### composite-index — 복합 인덱스 컬럼 순서

1. **등치(`=`) 조건 컬럼을 선두**에
2. 그다음 범위(`>`, `<`, `BETWEEN`, `LIKE 'x%'`) 조건
3. 마지막에 `ORDER BY` 컬럼 (정렬을 없앨 수 있다)

범위 조건 컬럼 뒤의 컬럼은 인덱스 스캔 범위를 좁히지 못한다. 그래서
범위는 뒤로 보낸다.

```sql
-- WHERE STATUS = :s AND REG_DT >= :d ORDER BY ORD_NO
CREATE INDEX IX_ORD_HDR_1 ON ORD_HDR (STATUS, REG_DT, ORD_NO);
```

SELECT 컬럼까지 넣으면(커버링) 테이블 랜덤 액세스가 사라진다. 계획에서
`TABLE ACCESS BY INDEX ROWID` 가 없어지고 `INDEX RANGE SCAN` 만 남는다.
대신 인덱스가 커지고 DML 이 느려진다.

### already-covered — 중복 인덱스

**기존 인덱스의 선두 컬럼이 같으면 새로 만들지 않는다.**
`(A,B)` 가 있는데 `(A)` 를 또 만드는 것은 낭비다. `(A,B,C)` 가 필요하면
`(A,B)` 를 **대체**하는 것을 검토한다(둘 다 두지 않는다).

인덱스를 추가하면 그 테이블의 INSERT/UPDATE/DELETE 가 모두 느려진다.
조회 이득과 저울질한다.

11g 에서 안 쓰는 인덱스를 찾는 방법:

```sql
ALTER INDEX IX_ORD_HDR_9 MONITORING USAGE;
-- 며칠 뒤
SELECT INDEX_NAME, USED FROM V$OBJECT_USAGE;
```

지우기 전에 `ALTER INDEX ... UNUSABLE` 로 먼저 막아 보고, 문제가 없으면
지운다. 되돌리기 쉽다.

기타 11g 인덱스 사항:
- `CREATE INDEX ... COMPRESS n` — 선두 컬럼 중복이 많으면 크기가 준다
- 비트맵 인덱스는 조회 전용(DW)에만. OLTP 에서 쓰면 DML 이 서로를 막는다
- 온라인 생성: `CREATE INDEX ... ONLINE` (Enterprise Edition)

### no-predicate — 조건을 못 찾음

인덱스 후보를 만들 조건이 없다. `WHERE` 절이 없거나 정규식이 못 읽은
형태(서브쿼리·인라인뷰 안의 조건)다. 사람이 직접 봐야 한다.

---

## 8. 비교 기준 (무엇을 보고 좋아졌다고 하는가)

1. **결과 건수가 같은지 먼저 본다.** 다르면 튜닝이 아니라 버그다.
   빠른 게 아니라 덜 한 것이다.
2. **논리적 읽기(Buffers)를 본다.** 수행 시간은 캐시 상태·서버 부하에
   흔들려 같은 SQL 도 실행마다 다르다. Buffers 는 재현된다.
3. 수행 시간은 **2회 이상** 재고 첫 회는 버린다. 첫 회에는 하드파싱과
   캐시 적재가 섞인다.
4. 플랜의 **Cost 는 추정치**다. Cost 가 낮은데 느린 경우가 흔하다.
   실제(A-Rows/Buffers)를 믿는다.

### 11g 에서 측정할 때 조심할 것

- **카디널리티 피드백(11.2)**: 1회차 실행 뒤 추정이 틀렸다고 판단되면
  2회차부터 **계획이 바뀐다.** 2회차가 빨라진 것이 튜닝 덕인지 피드백
  덕인지 구분해야 한다. `DISPLAY_CURSOR` 에 `cardinality feedback used
  for this statement` 라고 표시된다.
- **결과 캐시**: `RESULT_CACHE` 힌트나 테이블 설정이 걸려 있으면 2회차가
  비현실적으로 빠르다. 계획에 `RESULT CACHE` 연산이 보이는지 확인한다.
- **적응형 커서 공유**: 바인드 값을 바꿔 가며 재면 자식 커서가 달라져
  계획이 다를 수 있다. 같은 값으로 비교한다.
- **EXPLAIN PLAN 의 한계**: 바인드를 모두 VARCHAR2 로 가정하고 피킹도
  하지 않는다. 그래서 실제 실행 계획과 다를 수 있다. 정확히 보려면
  실행 후 `DISPLAY_CURSOR` 를 쓴다.
- 버퍼 캐시를 비우고 재는 것(`ALTER SYSTEM FLUSH BUFFER_CACHE`)은
  **운영 DB 에서 하지 않는다.** 다른 세션까지 느려진다.

---

## 9. 손대기 전에

- 운영 DB 에서 무거운 쿼리를 실행하는 것 자체가 부하다. 한적한 시간에 한다.
- 인덱스 생성·삭제는 사람이 판단해 실행한다. 이 도구는 문장만 만들어 준다.
- 계획을 고정해야 하면 힌트를 코드에 박기 전에 **SQL 계획 관리(SPM)** 를
  검토한다. 애플리케이션을 고치지 않고 좋은 계획을 고정할 수 있다.
- SQL Tuning Advisor 와 실시간 SQL 모니터링은 **Tuning Pack 라이선스**가
  있어야 쓸 수 있다. 없는 사이트에서 무심코 돌리면 감사에 걸린다.
