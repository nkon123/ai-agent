"""iferr MCP 툴 — agents/iferr 를 MCP 로 노출하는 얇은 껍데기.

로직은 넣지 않는다. 에이전트 함수를 부르고 문자열로 돌려주는 것까지가 전부다.

툴 구성 (같은 함수를 재사용하므로 어느 쪽으로 불러도 결과가 같다):

    combo  check_interface_errors   메일 → 키 → DB → 영향까지 한 번에
    step   list_error_mails         메일만 (DB 안 붙음)
    step   extract_interface_keys   텍스트 → 키 (메일도 DB 도 안 붙음)
    step   lookup_interface         키 하나만 DB 확인 (메일 안 읽음)

챗봇(로컬 소형 모델)에는 combo 만 보인다. 여러 툴을 순서대로 부르지
못하기 때문이다. step 툴은 MCP 서버가 그대로 노출하므로 Claude Code·IDE
같은 다른 호스트나 사람이 직접 쓸 수 있다.

이 툴들은 메일을 '읽기만' 하고 DB 는 SELECT 만 한다. 그래서 read_only=True 다.
회신·삭제·재처리 같은 동작을 추가하게 되면 반드시 별도 툴로 만들고
destructive=True 를 줄 것 — 그래야 클라이언트가 자동 실행을 막는다.
"""

from __future__ import annotations

import json

from agents.iferr import extract_keys, list_mails, lookup_key, run_iferr

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
    tier="combo",
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


# --------------------------------------------------------------------------
# 단계별 툴 (tier="step") — 챗봇에는 숨기고 다른 MCP 호스트에서 쓴다
# --------------------------------------------------------------------------


@register(
    label="오류 메일 목록 (iferr)",
    view="table",
    hint="list_error_mails 는 메일만 본다. DB 는 확인하지 않는다.",
    read_only=True,
    tier="step",
)
def list_error_mails(hours: int = 24) -> str:
    """최근 오류 메일 목록을 본다. DB 는 조회하지 않는다.

    '메일이 오긴 왔는지'부터 확인할 때 사용한다.
    발신자 주소는 마스킹되어 나온다.
    """
    r = list_mails(hours=hours, detail="summary")
    lines = [f"최근 {hours}시간 메일 {r['mail_count']}통 (오류 {r['error_count']}통)"]
    for m in r["mails"]:
        if not m["is_error"]:
            continue
        keys = ", ".join(m["keys"]) or "키 없음"
        lines.append(f"- [{m['received'][:16]}] {m['subject']} / {keys} / {m['sender']}")
    if r["warnings"]:
        lines.append("확인 필요: " + "; ".join(r["warnings"]))
    return "\n".join(lines)


@register(
    label="인터페이스 키 추출 (iferr)",
    view="text",
    hint=(
        "extract_interface_keys 는 메일 본문 같은 텍스트에서 인터페이스 키만 "
        "뽑는다. 메일함이나 DB 를 건드리지 않는다."
    ),
    read_only=True,
    tier="step",
)
def extract_interface_keys(text: str) -> str:
    """텍스트에서 인터페이스 키를 뽑는다. 메일함도 DB 도 건드리지 않는다.

    사용자가 메일 본문을 그대로 붙여 넣었을 때 사용한다.
    """
    hits = extract_keys(text)
    if not hits:
        # '없다'와 '못 찾았다'를 구분한다. 정규식이 안 맞는 형식일 수 있다.
        return (
            "인터페이스 키를 찾지 못했다. 설정된 패턴과 형식이 다를 수 있다 "
            "(config.IFERR_KEY_PATTERNS)"
        )
    return "\n".join(
        f"- {h['key']} (규칙 {h['rule']}, 근거: {h['evidence']})" for h in hits
    )


@register(
    label="인터페이스 키 조회 (iferr)",
    view="text",
    detail_uri="iferr://detail/{key}",
    hint=(
        "lookup_interface 는 이미 아는 키 하나만 DB 에서 확인한다. "
        "메일은 읽지 않는다. status 가 unknown 이면 '문제 없음'이 아니다."
    ),
    read_only=True,
    tier="step",
)
def lookup_interface(key: str) -> str:
    """키 하나로 DB 를 조회해 데이터 상태와 영향을 확인한다.

    메일을 읽지 않으므로 이미 키를 아는 경우에 빠르다.
    """
    r = lookup_key(key, detail="summary")
    if not r["cases"]:
        return f"{key}: 조회 결과 없음"
    c = r["cases"][0]
    mark = {"found": "확인됨", "missing": "데이터 없음"}.get(c["status"], "확인 불가")
    line = f"{c['key']}: {mark} — {c['impact']} (근거 {c['rule']})"
    if r["warnings"]:
        line += "\n확인 필요: " + "; ".join(r["warnings"])
    return line
