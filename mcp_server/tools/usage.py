"""usage MCP 툴 — agents/usage 를 MCP 로 노출하는 얇은 껍데기.

로직은 넣지 않는다. run_usage() 를 부르고 문자열로 돌려주는 것까지가 전부다.
"""

from __future__ import annotations

import json

from agents.usage import run_usage

from . import mcp, register


@register(
    label="식별자 사용처 찾기 (usage)",
    view="text",
    # {root} 와 {name} 두 값을 받는 리소스. 화면은
    # /api/resource?template=...&root=SAMPLE&name=TOTAL_AMT 로 읽어 간다.
    detail_uri="usage://detail/{root}/{name}",
    hint=(
        "find_usage 는 소스에서 변수·함수·테이블 같은 식별자가 쓰인 곳을 "
        "찾을 때만 쓴다. name 에는 식별자만 넣어라(문장을 넣지 마라). "
        "결과의 used 가 unknown 이면 '없다'가 아니라 '확인하지 못했다'는 뜻이니 "
        "그대로 사용자에게 전달하라."
    ),
    read_only=True,
)
def find_usage(name: str, root: str = "") -> str:
    """소스 트리에서 식별자가 실제로 쓰인 곳을 찾는다.

    주석과 문자열 안의 언급은 사용처로 세지 않는다.
    name 에는 찾을 식별자 이름을, root 에는 소스 루트 라벨(생략 가능)을 넣는다.
    """
    # summary 인 이유: 매칭이 수백 줄이면 full 은 매 턴 컨텍스트를 먹는다.
    r = run_usage(name, root=root, detail="summary")

    if r["used"] == "unknown":
        # '모른다'를 '없다'로 읽히게 두지 않는다.
        line = f"{name}: 확인 불가 (근거: {r['decided_by']}/{r['rule']})"
    elif r["used"] == "no":
        line = (
            f"{name}: 사용처 없음 — {r['root']} 의 "
            f"{r['files_scanned']}개 파일에서 매칭 0건"
        )
    else:
        files = ", ".join(r["files"])
        more = f" 외 {r['more_files']}개" if r["more_files"] else ""
        line = (
            f"{name}: {r['hit_count']}곳에서 사용 "
            f"({r['root']}, 파일 {files}{more})"
        )
    if r.get("comment"):
        line += f"\n요약: {r['comment']}"
    if r.get("warnings"):
        line += "\n확인 필요: " + "; ".join(r["warnings"])
    return line


@mcp.resource(
    "usage://detail/{root}/{name}",
    name="usage_detail",
    title="식별자 사용처 전체 목록",
    description="매칭된 파일·줄번호·원문 줄을 전부 담은 JSON.",
    mime_type="application/json",
)
def usage_detail(root: str, name: str) -> str:
    """화면 표시용 전체 데이터. LLM 컨텍스트를 거치지 않는다."""
    return json.dumps(run_usage(name, root=root, detail="full"), ensure_ascii=False)
