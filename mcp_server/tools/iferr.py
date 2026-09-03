"""iferr MCP 툴 — agents/iferr 를 MCP 로 노출하는 얇은 껍데기.

로직은 넣지 않는다. run_iferr() 를 부르고 문자열로 돌려주는 것까지가 전부다.

이 툴은 메일을 '읽기만' 하고 DB 는 SELECT 만 한다. 그래서 read_only=True 다.
회신·삭제·재처리 같은 동작을 추가하게 되면 반드시 별도 툴로 만들고
destructive=True 를 줄 것 — 그래야 클라이언트가 자동 실행을 막는다.
"""

from __future__ import annotations

import json

from agents.iferr import run_iferr

from . import mcp, register


@register(
    label="인터페이스 오류 확인 (iferr)",
    view="text",
    detail_uri="iferr://detail/{key}",
    hint=(
        "check_interface_errors 는 연계/인터페이스 오류 메일을 확인할 때 쓴다. "
        "특정 키만 볼 때는 key 인자에 인터페이스 키를 넣고, 전체를 훑을 때는 "
        "hours 만 넣어라. status 가 unknown 이면 '문제 없음'이 아니라 "
        "'확인하지 못했다'는 뜻이니 그 사실을 반드시 사용자에게 전달하라. "
        "재처리나 데이터 수정은 이 툴로 할 수 없다."
    ),
    read_only=True,
)
def check_interface_errors(hours: int = 24, key: str = "") -> str:
    """인터페이스 오류 메일을 읽고 키별로 DB 영향을 확인한다.

    타 시스템 연계에서 오류가 났을 때 어떤 인터페이스가 실패했고
    그 데이터가 어떤 상태인지 확인할 때 사용한다.
    hours 는 몇 시간 전까지 볼지, key 는 특정 인터페이스 키만 볼 때 넣는다.
    """
    # summary 인 이유: 조회 행까지 돌려주면 매 턴 컨텍스트를 먹는다.
    r = run_iferr(hours=hours, key=key, detail="summary")

    if not r["cases"]:
        head = (
            f"최근 {hours}시간 메일 {r['mail_count']}통에서 인터페이스 오류 키를 "
            "찾지 못했다"
        )
    else:
        lines = [
            f"최근 {hours}시간 메일 {r['mail_count']}통에서 "
            f"인터페이스 오류 {r['case_count']}건"
        ]
        for c in r["cases"]:
            mark = {"found": "확인됨", "missing": "데이터 없음"}.get(
                c["status"], "확인 불가"
            )
            lines.append(
                f"- {c['key']}: {mark} — {c['impact']} "
                f"(메일 {c['mail_count']}통, 근거 {c['rule']})"
            )
        if r["more_cases"]:
            lines.append(f"- 외 {r['more_cases']}건")
        head = "\n".join(lines)

    if r.get("comment"):
        head += f"\n요약: {r['comment']}"
    if r.get("warnings"):
        # 확인 필요를 삼키지 않는다. 장애를 놓치는 것보다 시끄러운 편이 낫다.
        head += "\n확인 필요: " + "; ".join(r["warnings"])
    return head


@mcp.resource(
    "iferr://detail/{key}",
    name="iferr_detail",
    title="인터페이스 오류 상세",
    description="키 하나에 대한 메일 목록과 DB 조회 결과 전체 JSON.",
    mime_type="application/json",
)
def iferr_detail(key: str) -> str:
    """화면 표시용 전체 데이터. LLM 컨텍스트를 거치지 않는다."""
    return json.dumps(run_iferr(key=key, detail="full"), ensure_ascii=False, default=str)
