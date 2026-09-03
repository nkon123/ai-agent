# echo (샘플 에이전트)

구조 확인용 최소 에이전트. **새 에이전트를 만들 때 이 폴더를 복사해 시작한다.**

## 하는 일

입력 문장을 규칙으로 분류(`question` / `command` / `statement` / `unknown`)하고,
`config.USE_LLM` 이 True 면 LLM 이 한 문장 덧붙인다.

## 실행

```bash
python agents/echo/agent.py "이거 왜 안 되지?"
python agents/echo/agent.py --detail summary "파일 지워줘"
USE_LLM=false python agents/echo/agent.py "그냥 문장"   # LLM 없이
```

## 반환 형태

```python
{
  "text": "...",
  "kind": "question",        # 판정 결과
  "decided_by": "rule",      # rule | llm | static | fallback
  "rule": "question-marker", # 어떤 규칙이 판정했는가
  "evidence": "왜",          # 판단 근거가 된 실제 조각
  "comment": "...",          # LLM 한마디 (USE_LLM=false 면 "")
  "warnings": []             # 판정 불가/실패는 여기 남는다
}
```

`detail` 인자로 반환량을 조절한다.

| detail | 용도 |
|---|---|
| `full` | 화면 표시용 전체 데이터 (`/api/echo`) |
| `summary` | 챗봇 툴이 LLM 에게 돌려주는 값 |
| `minimal` | 판정 결과만 |

## 이 파일에서 눈여겨볼 것

- **판정 근거를 남긴다** — `decided_by` / `rule` / `evidence` 3종 세트.
  결과만 있으면 오판 원인을 못 찾는다.
- **'모른다'와 '아니다'를 구분한다** — 빈 입력은 `statement` 가 아니라 `unknown`.
- **LLM 실패를 삼키지 않는다** — `warnings` 에 "확인 필요"로 남긴다.
- **`USE_LLM=false` 면 규칙만으로 끝까지 동작한다.**
- **`core/llm.py` import 를 함수 안에서 한다** — `langchain_ollama` 가 없는
  환경에서도 규칙 전용 실행이 죽지 않도록.
