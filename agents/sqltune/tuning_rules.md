# 오라클 튜닝 기준

이 문서가 판정 기준이다. 규칙 엔진과 LLM 프롬프트가 **같은 문서**를 쓴다.
기준을 코드에 흩어 놓으면 한쪽만 고쳤을 때 서로 다른 답이 나온다.

사내 기준이 다르면 `config.SQLTUNE_RULES_FILE` 로 다른 파일을 지정한다.

---

## 1. 인덱스를 못 타게 만드는 것 (가장 흔한 원인)

| 규칙 | 나쁜 예 | 고친 예 |
|---|---|---|
| `func-on-column` 인덱스 컬럼에 함수·연산 | `TO_CHAR(REG_DT,'YYYYMMDD') = '20260101'` | `REG_DT >= TO_DATE('20260101') AND REG_DT < TO_DATE('20260102')` |
| `nvl-on-column` 컬럼에 NVL | `NVL(STATUS,'N') = 'N'` | `(STATUS = 'N' OR STATUS IS NULL)` |
| `implicit-cast` 암시적 형변환 | `CHAR_COL = 123` | `CHAR_COL = '123'` |
| `leading-wildcard` 선행 와일드카드 | `NAME LIKE '%김%'` | 접두 검색으로 바꾸거나 텍스트 인덱스 |
| `negation` 부정 조건 | `STATUS != 'Y'` | 대상 값 열거(`IN`) 검토 |
| `or-condition` OR 로 묶인 다른 컬럼 | `A = :a OR B = :b` | `UNION ALL` 분리 검토 |

## 2. 접근 경로 (플랜에서 보는 것)

- `full-scan` **TABLE ACCESS FULL** — 전체 중 일부만 필요한데 FULL 이면 인덱스 검토.
  반대로 대량(대략 10~20% 이상)이면 FULL 이 정상이다. 무조건 인덱스가 답이 아니다.
- `index-full-scan` **INDEX FULL SCAN / SKIP SCAN** — 선두 컬럼이 조건에 없다는 신호.
- `filter-not-access` **filter 술어** — 인덱스가 걸러내지 못하고 테이블에서 버린 조건이다.
  access 술어로 옮길 수 있는지 본다.
- `cartesian` **MERGE JOIN CARTESIAN** — 조인 조건 누락 의심. 거의 항상 버그다.

## 3. 조인

- `nl-large-driving` NESTED LOOPS 는 선행 집합이 작아야 한다.
- 대량 대 대량은 HASH JOIN.
- 카디널리티가 작은 쪽을 선행으로.

## 4. 정렬·집계·페이징

- `sort-order-by` SORT ORDER BY 는 인덱스 정렬로 없앨 수 있는지 본다.
- 페이징은 정렬 컬럼이 인덱스 선두에 있어야 상위 N 건만 읽고 끝난다.

## 5. 통계와 추정

- `cardinality-miss` **E-Rows 와 A-Rows 차이가 10배 이상**이면 통계·히스토그램을 의심한다.
  추정이 틀리면 조인 방식 선택이 통째로 틀어진다.

## 6. 바인드 변수

- `literal-value` 리터럴은 하드파싱을 유발하고 공유풀을 압박한다. 반복 수행 SQL 은 바인드로.
- 다만 값이 심하게 편중된 컬럼은 바인드 피킹으로 잘못된 플랜이 굳을 수 있다.

## 7. 인덱스 설계

- **등치(=) 조건 컬럼을 선두**, 범위(`>`,`<`,`BETWEEN`,`LIKE 'x%'`) 조건은 그 뒤.
- ORDER BY 컬럼을 뒤에 붙이면 정렬을 없앨 수 있다.
- SELECT 컬럼까지 넣으면(커버링) 테이블 랜덤 액세스가 사라진다. 대신 인덱스가 커진다.
- **기존 인덱스의 선두 컬럼이 같으면 새로 만들지 않는다.** 중복 인덱스는 DML 만 느리게 한다.
- 인덱스 추가는 INSERT/UPDATE/DELETE 를 느리게 한다. 조회 이득과 저울질한다.

## 8. 비교 기준 (무엇을 보고 좋아졌다고 하는가)

1. **논리적 읽기(Buffers)를 먼저 본다.** 수행 시간은 캐시 상태·서버 부하에 흔들려
   같은 SQL 도 실행마다 다르다. Buffers 는 재현된다.
2. 수행 시간은 **2회 이상** 재고 첫 회는 버린다. 첫 회에는 하드파싱과 캐시 적재가 섞인다.
3. 플랜의 **Cost 는 추정치**다. Cost 가 낮은데 느린 경우가 흔하다. A-Rows/Buffers 를 믿는다.
4. 결과 건수가 달라지면 튜닝이 아니라 **버그다.** 건수부터 맞춘다.
