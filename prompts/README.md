# prompts

새 에이전트를 만들 때 코딩 어시스턴트(Claude Code 등)에게 넘길
프롬프트 템플릿을 모아 두는 곳.

아직 비어 있다. 에이전트를 하나 만들 때마다, 그때 쓴 프롬프트를
`<agent-name>.md` 로 남겨 두면 다음 에이전트를 만들 때 재사용할 수 있다.

템플릿에 반드시 포함할 것:

- 저장소의 개발 제약 (README 의 "개발 제약" 절)
- 참고할 샘플: `agents/echo/agent.py` 와 `mcp_server/tools/echo.py`
- MCP 리비전 `2026-07-28` 기준이라는 점 (`mcp>=2.1`, FastMCP 아님 → `MCPServer`)
- 그 에이전트가 판정할 대상과 규칙
- `detail` 별로 무엇을 돌려줄지
- 무엇을 캐시할지 (무엇이 비싼지)
- 파괴적 동작이면 `destructive=True` 를 줄 것
