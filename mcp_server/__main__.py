"""MCP 서버 진입점.

    python -m mcp_server                        # stdio
    MCP_TRANSPORT=streamable-http python -m mcp_server

stdio 가 기본인 이유: 폐쇄망 단일 장비에서는 포트를 열 이유가 없고,
호스트가 프로세스를 직접 띄우므로 인증 문제도 생기지 않는다.
에이전트를 별도 장비로 뺄 때만 streamable-http 로 바꾼다.
"""

from __future__ import annotations

import sys
from pathlib import Path

# 저장소 루트를 import 경로에 넣는다(패키지 설치를 하지 않는 배포 방식).
_ROOT = Path(__file__).resolve().parents[1]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

import config  # noqa: E402
from mcp_server.tools import build_server, load_all  # noqa: E402


def main() -> None:
    names = load_all()
    # stdio 모드에서 stdout 은 프로토콜 전용 채널이다. 여기에 print 를 하면
    # JSON-RPC 스트림이 오염되어 클라이언트가 핸드셰이크에서 죽는다.
    # 로그는 반드시 stderr 로 보낸다.
    print(f"[mcp_server] 툴 {len(names)}개: {', '.join(names)}", file=sys.stderr)

    server = build_server()
    transport = config.MCP_TRANSPORT
    if transport == "stdio":
        server.run("stdio")
    elif transport in ("streamable-http", "http"):
        print(
            f"[mcp_server] streamable-http "
            f"http://{config.MCP_HTTP_HOST}:{config.MCP_HTTP_PORT}/mcp",
            file=sys.stderr,
        )
        server.run(
            "streamable-http",
            host=config.MCP_HTTP_HOST,
            port=config.MCP_HTTP_PORT,
            # 2026-07-28 코어는 stateless 다. 세션을 붙들지 않으므로
            # 서버를 여러 벌 띄우고 앞에 로드밸런서를 둬도 세션 고정이
            # 필요 없다. 이걸 켜지 않으면 옛 방식대로 세션을 들고 있어
            # 재기동 때마다 클라이언트가 끊긴다.
            stateless_http=True,
        )
    else:
        raise SystemExit(f"알 수 없는 MCP_TRANSPORT: {transport}")


if __name__ == "__main__":
    main()
