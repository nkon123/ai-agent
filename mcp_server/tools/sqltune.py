"""sqltune MCP 툴 — agents/sqltune 를 MCP 로 노출하는 얇은 껍데기.

리소스를 두지 않는 이유: 다른 에이전트는 키(짧은 문자열)로 상세를 다시
읽지만, 여기서는 입력이 SQL 전문이라 URI 에 담을 수 없다. 전체 결과가
필요하면 CLI(`python agents/sqltune/agent.py -f slow.sql`)를 쓴다.

이 툴은 쿼리를 **실행하지 않는다**. 실행계획(EXPLAIN PLAN)만 본다.
실행은 곧 운영 DB 부하라 사람이 CLI 에서 --run 으로 켤 때만 돈다.
"""

from __future__ import annotations

from mcp.server.mcpserver import Context

from agents.sqltune import run_sqltune

from . import register, report_while


@register(
    label="SQL 튜닝 진단 (sqltune)",
    view="text",
    hint=(
        "tune_query 는 느린 SQL 을 진단할 때 쓴다. sql 인자에 쿼리 전문을 그대로 "
        "넣어라. 조회(SELECT)만 받는다. 이 툴은 쿼리를 실행하지 않고 실행계획만 "
        "본다. 인덱스는 제안만 하며 만들지 않는다 — 사용자에게 DDL 을 보여 주고 "
        "직접 실행하라고 안내하라."
    ),
    examples=(
        "이 쿼리 왜 느린지 봐줘",
        "인덱스 뭐 만들면 좋을까?",
        "이 SQL 튜닝 포인트 알려줘",
    ),
    read_only=True,
    tier="combo",
)
async def tune_query(sql: str, ctx: Context = None) -> str:
    """느린 SQL 을 오라클 튜닝 기준으로 진단하고 인덱스를 제안한다.

    쿼리를 실행하지 않고 실행계획만 본다. 조회(SELECT)만 받는다.
    sql 에는 쿼리 전문을 그대로 넣는다.
    """
    # execute=False 를 명시한다. 설정이 켜져 있어도 챗봇에서는 실행하지 않는다
    # — 모델이 무심코 부를 수 있고, 실행은 운영 DB 부하다.
    r = await report_while(
        ctx, lambda: run_sqltune(sql, execute=False, detail="summary")
    )

    if not r["safe"]:
        return "진단하지 않았다: " + "; ".join(r["warnings"])

    lines = [f"진단 결과 {r['finding_count']}건" + (
        f" (실행계획 {r['plan_rows']}단계 확인)" if r["plan_rows"] else " (실행계획 미확인)"
    )]
    for f in r["findings"]:
        lines.append(f"- {f['rule']}: {f['note']}")

    idx = r.get("index") or {}
    if idx.get("ddl"):
        lines.append(f"인덱스 제안: {idx['ddl']}")
        lines.append("  (실행하지 않았다. 검토 후 직접 수행할 것)")
    elif idx.get("note"):
        lines.append(f"인덱스: {idx['note']}")

    if r.get("proposal"):
        lines.append(f"개선안:\n{r['proposal']}")
    if r.get("warnings"):
        lines.append("확인 필요: " + "; ".join(r["warnings"]))
    return "\n".join(lines)


@register(
    label="실행계획 보기 (sqltune)",
    view="text",
    hint="explain_query 는 실행계획만 볼 때 쓴다. 쿼리를 실행하지 않는다.",
    read_only=True,
    tier="step",
)
def explain_query(sql: str) -> str:
    """쿼리를 실행하지 않고 실행계획만 본다."""
    from agents.sqltune.agent import format_plan

    r = run_sqltune(sql, execute=False, detail="full")
    if not r["safe"]:
        return "진단하지 않았다: " + "; ".join(r["warnings"])
    text = format_plan(r.get("plan") or [])
    if r.get("warnings"):
        text += "\n확인 필요: " + "; ".join(r["warnings"])
    return text
