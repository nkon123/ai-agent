# samples

`usage` 에이전트를 바로 돌려 보기 위한 예제 소스. 사내 코드를 대신한다.

함정을 일부러 심어 두었다.

| 파일 | 심어 둔 것 |
|---|---|
| `src/erp_calc.c` | 주석 속 `TOTAL_AMT`, 문자열 속 `"/* TOTAL_AMT */"`, 포인터 연산 `unit*/*...*/qty`, 별개 식별자 `IF_A` |
| `src/order_pkg.sql` | `--` 주석 속 언급, SQL 문자열 속 언급 |
| `src/legacy_cp949.c` | **cp949 인코딩** (utf-8 로만 읽으면 깨진다) |
| `src/order_load.pc` | Pro\*C. 주석 속 언급, `SELECT ... FROM` / `DELETE FROM` |
| `src/ord_batch.prc` | 프로시저. `INSERT INTO A SELECT FROM T` — T 는 **읽기**다 |
| `src/order_query.xml` | UI 쿼리. **세미콜론이 없어** 태그로 경계를 잡아야 한다 |
| `src/OrderLoader.java` | SQL 이 **문자열 안**에 있다. 문자열을 지우면 통째로 놓친다 |

`TOTAL_AMT` 를 찾으면 주석·문자열 속 언급은 빠지고 실제 사용처만 나와야 한다.
