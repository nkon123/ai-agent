"""impact MCP 툴 — agents/impact 를 MCP 로 노출하는 얇은 껍데기."""

from __future__ import annotations

import json

from mcp.server.mcpserver import Context

from agents.impact import run_impact
from agents.impact.column import column_grid, run_column_impact

from . import mcp, register, report_while


@register(
    label="테이블 영향도 조사 (impact)",
    view="text",
    detail_uri="impact://detail/{root}/{table}",
    hint=(
        "analyze_table_impact 는 테이블에 관한 질문에 쓴다 — 테이블이 어디서 쓰이는지, "
        "테이블 영향도, 테이블을 쓰는 프로그램, 테이블에 데이터가 안 들어오면 무엇이 "
        "문제인지. 변수·함수 사용처는 find_usage 다. "
        "테이블과 연결된 인터페이스(I/F, IFID)는 find_table_interfaces 다. "
        "테이블의 특정 컬럼 영향도는 analyze_column_impact 다. "
        "table 에는 테이블 이름만 넣어라. "
        "쓰기(write)가 있는 프로그램이 먼저 영향을 받는다."
    ),
    # 예시마다 '테이블'을 넣는다. 이름만으로는 테이블인지 변수인지 모델이
    # 가르지 못한다. 이름 없는 "이 테이블 영향도 조사해줘" 는 어떤 툴도 고르지
    # 못해(실측) 뺐다.
    examples=(
        "IF_ORDER_TMP 테이블 영향도 조사해줘",
        "IF_ORDER_TMP 테이블 어디서 쓰여?",
        "IF_ORDER_TMP 테이블에 데이터 안 들어오면 뭐가 문제 생겨?",
    ),
    read_only=True,
    tier="combo",
)
async def analyze_table_impact(
    table: str, root: str = "", ctx: Context = None
) -> str:
    """테이블이 소스 어디에서 읽히고 쓰이는지(테이블 영향도) 조사한다.

    테이블 이름이 나온 줄이 속한 SQL 문장을 통째로 잘라내 읽기/쓰기를
    판정한다. 인터페이스 실패로 그 테이블이 비었을 때 무엇이 영향을
    받는지 가늠하는 데 쓴다.
    root 는 사용자가 특정 소스 루트를 말했을 때만 넣는다 — 비우면 전부 찾는다.
    """
    # summary 인 이유: SQL 전문을 돌려주면 매 턴 컨텍스트를 먹는다.
    # 문장 판정은 하나에 수십 초라 진행 상황을 중계한다.
    r = await report_while(ctx, lambda: run_impact(table, root=root, detail="summary"))

    # 시스템·스키마가 붙어 왔으면 에이전트가 이름만 남겼다. 답에도 그 이름을 쓴다.
    table = r.get("table") or table
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
    if r.get("note"):
        line += f"\n참고: {r['note']}"
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


@register(
    label="컬럼 영향도 조사 (impact)",
    # 화면은 view="grid" 인 툴의 답 아래에 [표로 보기] 를 붙이고 detail_uri 를 읽는다.
    view="grid",
    detail_uri="impact://column/{table}/{column}/{root}",
    hint=(
        "analyze_column_impact 는 테이블의 특정 컬럼이 어디서 쓰이는지(컬럼 영향도)를 물을 때 "
        "쓴다. table 에는 테이블 이름, column 에는 컬럼 이름만 따로 넣어라. "
        "테이블을 쓰는 문장 중 컬럼이 없는 것도 '테이블만 씀'으로 센다 — 컬럼 목록 없는 "
        "INSERT 는 영향 있음, SELECT * 는 확인 필요다. "
        "컬럼 없이 테이블만 물으면 analyze_table_impact 다. "
        "답의 '확인 필요' 건수는 '영향 없음'이 아니다. 전체 목록은 답 아래 [표로 보기] 에 있다고 "
        "사용자에게 알려라."
    ),
    examples=(
        "IF_ORDER_TMP 테이블 TOTAL_AMT 컬럼 영향도 조사해줘",
        "IF_ORDER_TMP 테이블의 STATUS 컬럼 어디서 쓰여?",
    ),
    read_only=True,
    tier="combo",
)
async def analyze_column_impact(
    table: str, column: str, root: str = "", ctx: Context = None
) -> str:
    """테이블의 컬럼 하나가 소스 어디에서 실제로 쓰이는지(컬럼 영향도) 조사한다.

    테이블을 쓰는 파일에서 컬럼 이름이 나온 줄을 모두 찾고, 줄마다 그 테이블의
    컬럼으로 쓰였는지(영향 있음/없음/확인 필요)를 판정한다. 테이블을 쓰지만 컬럼
    이름이 없는 문장도 함께 보인다(컬럼 목록 없는 INSERT·SELECT * 는 따로 판정).
    table 과 column 은 따로 넣는다. root 는 사용자가 특정 소스 루트를 말했을
    때만 넣는다 — 비우면 설정된 소스 루트 전부를 찾는다.
    """
    r = await report_while(
        ctx, lambda: run_column_impact(table, column, root=root, detail="summary")
    )
    name = f"{r['table']}.{r['column']}" if r["table"] and r["column"] else f"{table}.{column}"
    c = r["counts"]
    if r["rule"] in ("empty-name", "unknown-root", "no-files"):
        line = f"{name}: 확인 불가 — 조사하지 못했다(없다는 뜻이 아니다)"
    elif r["rule"] == "no-table-hit":
        line = (f"{name}: {r['root']} 소스({r['files_scanned']}개 파일)에서 테이블 {r['table']} 을 "
                "쓰는 파일을 찾지 못했다")
    else:
        found = (f"컬럼이 나온 줄 {r['hit_count']}개" if r["hit_count"]
                 else f"컬럼 {r['column']} 이 나온 줄은 없음")
        line = (f"{name} 컬럼 영향도 ({r['root']}, 테이블을 쓰는 파일 {r['table_files']}개 — "
                f"{found}, 컬럼 없이 테이블만 쓰는 문장 {r['table_only']}개): "
                f"영향 있음 {c['yes']} / 확인 필요 {c['unknown']} / 영향 없음 {c['no']}")
        for p in r["places"]:
            line += f"\n  - {p}"
        if c["yes"] > len(r["places"]):
            line += f"\n  - 외 {c['yes'] - len(r['places'])}곳"
        if r["unknown_places"]:
            line += "\n확인 필요 위치: " + ", ".join(r["unknown_places"])
        line += "\n전체 목록(프로그램·줄·판정 근거)은 답 아래 [표로 보기] 에서 볼 수 있다"
    if r.get("qualifier"):
        line += f"\n참고: 앞의 '{r['qualifier']}' 는 떼고 테이블 {r['table']} 로 찾았다"
    if r.get("warnings"):
        line += "\n확인 필요: " + "; ".join(r["warnings"])
    return line


@mcp.resource(
    "impact://column/{table}/{column}/{root}",
    name="impact_column_grid",
    title="컬럼 영향도 그리드",
    description="찾은 줄 전부와 판정(그리드용 머리·행). root 가 '-' 면 기본 루트.",
    mime_type="application/json",
)
def impact_column_grid(table: str, column: str, root: str) -> str:
    """화면 그리드용. 툴과 같은 캐시를 쓰므로 방금 조사한 것은 바로 나온다."""
    return json.dumps(column_grid(table, column, root), ensure_ascii=False, default=str)
