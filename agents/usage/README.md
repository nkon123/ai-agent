# usage (식별자 사용처 찾기)

소스 트리에서 변수·함수·테이블 같은 **식별자가 실제로 쓰인 곳**을 찾는다.
`core/` 의 도구들이 왜 그렇게 생겼는지를 보여주는 실전 샘플이다.

## 실행

```bash
python agents/usage/agent.py TOTAL_AMT --root SAMPLE
python agents/usage/agent.py TOTAL_AMT --root SAMPLE --detail summary
USE_LLM=false python agents/usage/agent.py IF_A --root SAMPLE   # LLM 없이
```

`samples/src/` 에 함정을 심어 둔 예제 소스가 있어 바로 돌려 볼 수 있다.

## 무엇을 사용처로 세지 않는가

| 형태 | 왜 |
|---|---|
| `/* TOTAL_AMT 를 채운다 */` | 주석 — `strip_comments` 로 제거 |
| `'TOTAL_AMT is not a hit'` | 문자열 — `strip_comments(mask_strings=True)` 로 마스킹 |
| `IF_A` 안의 `A` | 식별자 경계 — `ident_pattern` (`\b` 를 쓰면 여기서 깨진다) |

## 대소문자

기본은 **가린다**. 코드의 식별자는 `TOTAL_AMT` 와 `total_amt` 가 서로 다른
것이기 때문이다.

다만 못 찾았을 때 대소문자만 다른 것이 있으면 `used="no"` 가 아니라
**`unknown`** 으로 남기고 알려 준다. 있는데 '없다'고 답하는 것이 제일 나쁘다.

```
IF_ORDER_TMP: 확인 불가 (근거: rule/case-mismatch)
확인 필요: 대소문자가 다른 'if_order_tmp' 가 있다 — 확인 필요
```

테이블 이름을 찾는 경우라면 무시하고 다시 찾는다.

```bash
python agents/usage/agent.py IF_ORDER_TMP -i
```

`impact` 는 애초에 대소문자를 무시한다 — SQL 테이블 이름이 대상이라서다.

cp949 파일도 읽는다 (`read_text_with_encoding`). 사내 파일은 흔히 cp949 다.

## 판정

```python
{"used": "yes|no|unknown", "decided_by": "rule|fallback",
 "rule": "ident-found|no-hit|no-files|unknown-root|empty-name",
 "evidence": "erp_calc.c:10 TOTAL_AMT = r;"}
```

`no`(**없다**)와 `unknown`(**확인하지 못했다**)은 다른 값이다.
루트가 비어 있거나 잘못된 라벨이면 `unknown` 이고 `warnings` 에 "확인 필요"가 남는다.
읽지 못한 파일이 있어도 조용히 넘기지 않는다 — 누락은 오탐보다 나쁘다.

## 캐시 키

```python
@cached(ttl=300, maxsize=8, key=lambda root_path: root_path)
def scan_files(root_path: str) -> list[str]: ...
```

비싼 것은 **디렉터리를 훑는 일**이고 그 결과는 찾는 이름과 무관하다.
그래서 키에서 `name` 을 뺀다. 넣으면 이름을 바꿀 때마다 재스캔한다.

## MCP

| MCP | 무엇 |
|---|---|
| 툴 `find_usage(name, root)` | `detail="summary"` — 건수와 파일 목록 일부 |
| 리소스 `usage://detail/{root}/{name}` | `detail="full"` — 매칭된 줄 전부 |
| annotations | `readOnly=true`, `destructive=false` |

```
GET /api/resource?template=usage://detail/{root}/{name}&root=SAMPLE&name=TOTAL_AMT
```
