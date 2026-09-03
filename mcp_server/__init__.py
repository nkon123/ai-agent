"""MCP 서버 — 에이전트를 MCP 툴/리소스로 노출한다.

챗봇(app/)은 이 서버의 클라이언트일 뿐이다. 둘은 프로세스가 분리되어
있고 MCP 프로토콜로만 통신한다. 덕분에

  - 챗봇이 아닌 다른 MCP 호스트(Claude Code, IDE 등)에서도 같은 툴을 쓴다
  - 에이전트가 죽어도 챗봇은 살아 있다
  - 나중에 에이전트를 다른 장비로 옮겨도 transport 만 바꾸면 된다

실행:
    python -m mcp_server                      # stdio (기본)
    MCP_TRANSPORT=streamable-http python -m mcp_server
"""

from .tools import build_server

__all__ = ["build_server"]
