"""impact MCP 툴 — agents/impact 를 MCP 로 노출하는 얇은 껍데기."""

from __future__ import annotations

import json

from agents.impact import run_impact

from . import mcp, register


@register(
    label="테이블 영향도 조사 (impact)",
    view="text",
    detail_uri="impact://detail/{root}/{table}",
    hint=(
        "analyze_table_impact 는 테이블이 소스 어디에서 어떻게 쓰이는지 조사할 때 "
        "쓴다. 인터페이스 오류로 어떤 테이블에 데이터가 안 들어왔을 때, 그 테이블을 "
        "쓰는 프로그램이 무엇인지 확인하는 용도다. table 에는 테이블 이름만 넣어라. "
        "쓰기(write)가 있는 프로그램이 먼저 영향을 받는다."
    ),
    examples=(
        "IF_ORDER_TMP 이 테이블 어디서 쓰여?",
        "이 테이블 영향도 조사해줘",
        "IF_ORDER_TMP 에 데이터 안 들어오면 뭐가 문제 생겨?",
    ),
    read_only=True,
    tier="combo",
)
def analyze_table_impact(table: str, root: str = "") -> str:
    """테이블이 소스 어디에서 읽히고 쓰이는지 조사한다.

    테이블 이름이 나온 줄이 속한 SQL 문장을 통째로 잘라내 읽기/쓰기를
    판정한다. 인터페이스 실패로 그 테이블이 비었을 때 무엇이 영향을
    받는지 가늠하는 데 쓴다.
    """
    # summary 인 이유: SQL 전문을 돌려주면 매 턴 컨텍스트를 먹는다.
    r = run_impact(table, root=root, detail="summary")

    if r["rule"] == "unknown-root":
        return f"{table}: 소스 루트를 찾지 못했다 — " + "; ".join(r["warnings"])
    if not r["write_count"] and not r["read_count"] and not r["unknown_count"]:
        # '안 쓴다'와 '못 찾았다'를 구분한다.
        state = (
            "스캔할 소스가 없다" if r["rule"] == "no-files" else "사용처를 찾지 못했다"
        )
        line = f"{table}: {state} ({r['root']}, {r['files_scanned']}개 파일)"
    else:
        line = (
            f"{table}: 쓰기 {r['write_count']}곳 / 읽기 {r['read_count']}곳"
            + (f" / 판정불가 {r['unknown_count']}곳" if r["unknown_count"] else "")
            + f" ({r['root']}, 파일 {', '.join(r['files'])}"
            + (f" 외 {r['more_files']}개" if r["more_files"] else "")
            + ")"
        )
        for place in r["places"]:
            line += f"\n  - {place}"
    if r.get("warnings"):
        line += "\n확인 필요: " + "; ".join(r["warnings"])
    return line


@mcp.resource(
    "impact://detail/{root}/{table}",
    name="impact_detail",
    title="테이블 영향도 상세",
    description="잘라낸 SQL 문장 전문과 판정 근거를 담은 JSON.",
    mime_type="application/json",
)
def impact_detail(root: str, table: str) -> str:
    """화면 표시용 전체 데이터. LLM 컨텍스트를 거치지 않는다."""
    return json.dumps(
        run_impact(table, root=root, detail="full"), ensure_ascii=False, default=str
    )
