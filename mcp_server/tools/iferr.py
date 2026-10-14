"""iferr MCP 툴 — agents/iferr 를 MCP 로 노출하는 얇은 껍데기.

로직은 넣지 않는다. 에이전트 함수를 부르고 문자열로 돌려주는 것까지가 전부다.

툴 구성 (같은 함수를 재사용하므로 어느 쪽으로 불러도 결과가 같다):

    combo  check_interface_errors   메일 → 키 → 테이블 → 원인 컬럼 → 영향도까지 한 번에 (agents/incident)
    combo  find_interface_tables    IFID 하나 → 참조 테이블(출발·도착) (메일 안 읽음)
    combo  find_table_interfaces    테이블 하나 → 그 테이블을 쓰는 인터페이스 (메일 안 읽음)
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

from config import IFERR_KEY_PREFIXES

from mcp.server.mcpserver import Context

from config import MAIL_LOOKBACK_HOURS

from agents.iferr import (
    extract_keys,
    interface_tables,
    list_mails,
    lookup_key,
    run_iferr,
    table_interfaces,
)
from agents.iferr.agent import flow_text, parse_period, system_table
from core.sqlstmt import bare_table_name

from agents.incident import incident_grid, run_incident

from . import mcp, register, report_while

def sample_key(prefixes: tuple[str, ...]) -> str:
    """예시 질문에 쓸 키를 만든다.

    사내 고유 접두어를 추적되는 파일에 박지 않으면서도, 화면에는 실제
    형태와 같은 예시가 뜨게 하려는 것이다. 접두어가 없으면 일반 문구.
    """
    return f"{prefixes[0]}0001234" if prefixes else "인터페이스ID"


_SAMPLE_KEY = sample_key(IFERR_KEY_PREFIXES)


@register(
    label="인터페이스 오류 확인 (iferr)",
    # 메일의 원인 컬럼까지 영향도를 이어서 조사하므로 결과 그리드가 있다.
    view="grid",
    detail_uri="iferr://impact/{period}/{key}",
    hint=(
        "check_interface_errors 는 연계/인터페이스 오류 메일을 확인할 때 쓴다. "
        "period 에는 사용자가 말한 기간을 그대로 넣어라 — '3일'이면 \"3일\", "
        "'오늘'이면 \"오늘\". 시간으로 바꿔 계산하지 마라. "
        "특정 키만 볼 때는 key 에 인터페이스 키를 넣어라. "
        "이 툴은 메일에 적힌 원인 컬럼(길이 초과 등)의 영향도까지 한 번에 조사한다 — "
        "답에 원인 컬럼과 영향 있는 프로그램을 빠짐없이 전달하고, 전체 목록은 답 아래 "
        "[표로 보기] 에 있다고 알려라. "
        "status 가 unknown 이면 '문제 없음'이 아니라 '확인하지 못했다'는 뜻이니 "
        "그 사실을 반드시 사용자에게 전달하라. "
        "재처리나 데이터 수정은 이 툴로 할 수 없다."
    ),
    examples=(
        "오늘 인터페이스 오류 확인해줘",
        "최근 3일 연계 실패 확인해줘",
        f"{_SAMPLE_KEY} 이 인터페이스 뭐가 문제야?",
        "어제 실패한 인터페이스는 어디로 가는 거였어?",
    ),
    read_only=True,
    tier="combo",
)
async def check_interface_errors(period: str = "", key: str = "", ctx: Context = None) -> str:
    """인터페이스 오류 메일을 읽고 키별로 테이블·원인 컬럼·영향도까지 확인한다.

    타 시스템 연계에서 오류가 났을 때 어떤 인터페이스가 실패했고, 메일에 적힌
    원인 컬럼(길이 초과·NULL 등)을 쓰는 프로그램이 어디인지까지 조사한다.

    period 에는 사용자가 말한 기간을 그대로 넣는다("3일", "오늘", "12시간").
    시간 수로 바꿔 계산하지 말 것.
    key 는 특정 인터페이스 키만 확인할 때 넣는다.
    """
    # 시간 계산을 모델에 맡기지 않는 이유: 소형 모델은 "3일"을 3으로 넣는다.
    # 실제로 그래서 3일치가 아니라 3시간치만 보고 '없다'고 답했다.
    # 메일 판정·컬럼 판정이 수십 초씩 걸려 진행을 중계한다(빈 화면은 멈춘 것과 구분이 안 된다).
    r = await report_while(ctx, lambda: run_incident(period=period, key=key))
    label, hours = r["label"], r["hours"]

    if not r["cases"]:
        head = (
            f"{label}({hours}시간) 메일 {r['mail_count']}통에서 인터페이스 오류 키를 "
            "찾지 못했다"
        )
    else:
        lines = [
            f"{label}({hours}시간) 메일 {r['mail_count']}통에서 "
            f"인터페이스 오류 {len(r['cases'])}건"
        ]
        n_imp = 0
        for c in r["cases"]:
            mark = {"found": "확인됨", "missing": "데이터 없음"}.get(c["status"], "확인 불가")
            src = " / 마스터: 파일" if c["master_source"] == "file" else ""
            lines.append(
                f"- {c['key']}: {mark} — {c['impact']} "
                f"(메일 {c['mail_count']}통, 근거 {c['rule']}{src})"
            )
            for imp in c["impacts"]:
                n_imp += 1
                ci, n = imp["result"], imp["result"]["counts"]
                lines.append(f"  원인: {imp['issue_text']} — 테이블은 {imp['table_from']}에서")
                lines.append(
                    f"  영향도: {imp['table']}.{imp['column']} 을 쓰는 줄 {ci['hit_count']}개 "
                    f"({ci['root']}, 테이블을 쓰는 파일 {ci['table_files']}개) — "
                    f"영향 있음 {n['yes']} / 확인 필요 {n['unknown']} / 영향 없음 {n['no']}"
                )
                lines += [f"    · {p}" for p in ci["places"][:5]]
                if n["yes"] > 5:
                    lines.append(f"    · 외 {n['yes'] - 5}줄")
        if n_imp:
            lines.append("전체 목록(프로그램·줄·판정 근거)은 답 아래 [표로 보기] 에서 볼 수 있다")
        else:
            # 원인 컬럼이 없으면 예전처럼 다음 질문을 짚어 준다. 소형 모델은 툴을 이어 부르지
            # 못하고, 'EMP.TB_ORDER' 를 그대로 보여 주면 그 문자열로 영향도를 물어 엉뚱한 것을 찾는다.
            targets = [
                bare_table_name(f["tar_table"])[0]
                for c in r["cases"] for f in c["flows"] if f.get("tar_table")
            ]
            if targets:
                uniq = sorted(set(targets))[:3]
                lines.append(
                    "영향 조사: " + ", ".join(uniq) + " 이(가) 안 채워졌다. 이 테이블을 쓰는 "
                    "프로그램은 analyze_table_impact 로 확인할 수 있다"
                )
        head = "\n".join(lines)

    if r.get("comment"):
        head += f"\n요약: {r['comment']}"
    if r["warnings"]:
        # 확인 필요를 삼키지 않는다. 장애를 놓치는 것보다 시끄러운 편이 낫다.
        head += "\n확인 필요: " + "; ".join(r["warnings"])
    return head


@mcp.resource(
    "iferr://impact/{period}/{key}",
    name="iferr_impact_grid",
    title="인터페이스 오류 영향도 그리드",
    description="오류 케이스별 원인 컬럼의 영향도 줄 전부. period·key 가 '-' 면 기본값.",
    mime_type="application/json",
)
def iferr_impact_grid(period: str, key: str) -> str:
    """화면 그리드용. 방금 툴이 조사한 결과를 쓴다(없으면 새로 조사)."""
    return json.dumps(incident_grid(period, key), ensure_ascii=False, default=str)


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


@register(
    label="인터페이스 테이블 확인 (iferr)",
    view="text",
    hint=(
        "find_interface_tables 는 인터페이스 ID(IFID)를 주고 그 인터페이스가 참조하는 "
        "테이블(출발·도착)을 물을 때만 쓴다. "
        "테이블 이름을 주고 '어디서 쓰여?', '영향도', '데이터 안 들어오면 뭐가 문제' 를 "
        "물으면 이 툴이 아니라 analyze_table_impact 다. 테이블 이름을 주고 관련 "
        "인터페이스를 물으면 find_table_interfaces 다. 오류·실패 여부는 "
        "check_interface_errors 다. key 에는 사용자가 말한 ID 를 그대로 넣어라. "
        "status 가 unknown 이면 '테이블 없음'이 아니라 '확인하지 못했다'는 뜻이다."
    ),
    examples=(
        f"{_SAMPLE_KEY} 인터페이스가 참조하는 테이블 알려줘",
        f"{_SAMPLE_KEY} 인터페이스 테이블 확인해줘",
    ),
    read_only=True,
    tier="combo",
)
def find_interface_tables(key: str) -> str:
    """인터페이스 ID 가 어떤 테이블을 읽고 어디에 넣는지(출발·도착 테이블) 확인한다.

    '이 인터페이스가 참조하는 테이블' 같은 질문에만 쓴다. 메일은 읽지 않고
    인터페이스 마스터만 조회한다. 테이블에 데이터가 안 들어올 때의 영향이나
    테이블을 쓰는 프로그램은 이 툴이 아니라 analyze_table_impact 가 답한다.
    key 에는 사용자가 말한 인터페이스 ID 를 그대로 넣는다.
    """
    r = interface_tables(key)
    k = r["key"] or key
    if r["status"] == "unknown":
        line = f"{k}: 확인 불가 — 테이블을 확인하지 못했다(없다는 뜻이 아니다)"
    elif r["status"] == "missing":
        line = f"{k}: 인터페이스 마스터에 없는 ID 다. 철자를 확인할 것"
    elif not r["tables"]:
        line = f"{k}: 마스터에 행은 있지만 테이블 이름이 비어 있다"
    else:
        role = {"source": "출발", "target": "도착"}
        lines = [f"{k} 인터페이스가 참조하는 테이블 {len(r['tables'])}개"]
        for t in r["tables"]:
            where = system_table(t["system"], t["table"])
            other = f" (IFID {t['interface']})" if t["interface"].upper() != k.upper() else ""
            lines.append(f"- {role[t['role']]}: {where}{other}")
        for f in r["flows"][:3]:
            src = system_table(f.get("src_sys", ""), f.get("src_table", ""))
            tar = system_table(f.get("tar_sys", ""), f.get("tar_table", ""))
            if src or tar:
                lines.append(f"경로: {src or '?'} → {tar or '?'}"
                             + (f" ({f['schedule']})" if f.get("schedule") else ""))
        # 소형 모델은 툴을 이어 부르지 못한다. 다음 질문을 사용자가 할 수 있게
        # 도착 테이블(안 채워지면 영향을 받는 쪽)을 짚어 준다.
        # 예시 문구는 실측으로 골랐다(qwen3.5:4b): "X 어디서 쓰여?" 는
        # find_usage 로 가고, "X 테이블 영향도 조사해줘" 는 영향도 툴로 간다.
        # 스키마가 붙어 있어도('EMP.TB_ORDER') 다음 질문에는 이름만 쓴다.
        targets = [bare_table_name(t["table"])[0] for t in r["tables"] if t["role"] == "target"]
        if targets:
            lines.append(
                "이 테이블을 쓰는 프로그램은 analyze_table_impact 로 확인할 수 있다 "
                f"(예: \"{targets[0]} 테이블 영향도 조사해줘\")"
            )
        line = "\n".join(lines)
    if r["warnings"]:
        line += "\n확인 필요: " + "; ".join(r["warnings"])
    return line


@mcp.resource(
    "iferr://table/{table}",
    name="iferr_table_interfaces",
    title="테이블을 쓰는 인터페이스",
    description="테이블 하나를 출발·도착으로 쓰는 인터페이스 전체 JSON.",
    mime_type="application/json",
)
def iferr_table_interfaces(table: str) -> str:
    """화면 표시용 전체 데이터. LLM 컨텍스트를 거치지 않는다."""
    return json.dumps(table_interfaces(table), ensure_ascii=False, default=str)


_TABLE_IFS_SHOWN = 15


@register(
    label="테이블 관련 인터페이스 (iferr)",
    view="text",
    detail_uri="iferr://table/{table}",
    hint=(
        "find_table_interfaces 는 테이블 이름을 주고 그 테이블과 연결된 인터페이스"
        "(I/F, IFID, 연계)를 물을 때 쓴다 — 이 테이블에 데이터를 넣는/가져가는 "
        "인터페이스. 테이블을 쓰는 프로그램·소스·영향도는 analyze_table_impact 다. "
        "table 에는 테이블 이름만 넣어라. "
        "status 가 unknown 이면 '인터페이스 없음'이 아니라 '확인하지 못했다'는 뜻이다."
    ),
    examples=(
        "IF_ORDER_TMP 테이블 관련 인터페이스 알려줘",
        "IF_ORDER_TMP 테이블에 데이터 넣는 I/F 뭐야?",
    ),
    read_only=True,
    tier="combo",
)
def find_table_interfaces(table: str) -> str:
    """테이블 이름으로 그 테이블을 출발·도착으로 쓰는 인터페이스(IFID)를 찾는다.

    '이 테이블 관련 인터페이스', '이 테이블에 넣는 I/F' 같은 질문에 쓴다.
    메일은 읽지 않고 인터페이스 마스터만 조회한다. 테이블을 쓰는 프로그램은
    이 툴이 아니라 analyze_table_impact 가 답한다.
    table 에는 테이블 이름만 넣는다.
    """
    r = table_interfaces(table)
    name = r["table"] or table
    if r["status"] == "unknown":
        line = f"{name}: 확인 불가 — 인터페이스를 확인하지 못했다(없다는 뜻이 아니다)"
    elif r["status"] == "missing":
        line = (f"{name}: 인터페이스 마스터에 이 테이블을 출발·도착으로 쓰는 인터페이스가 "
                "없다. 이름을 확인할 것")
    else:
        ifs = r["interfaces"]
        n_in = sum(1 for i in ifs if "target" in i["roles"])
        lines = [f"{name} 테이블과 연결된 인터페이스 {len(ifs)}개 "
                 f"(들어옴 {n_in}, 나감 {len(ifs) - n_in}, 인터페이스 마스터 기준)"]
        for i in ifs[:_TABLE_IFS_SHOWN]:
            role = "/".join("들어옴" if x == "target" else "나감" for x in i["roles"])
            lines.append(f"- {i['id']} [{role}]: {flow_text(i)}")
        if len(ifs) > _TABLE_IFS_SHOWN:
            lines.append(f"- 외 {len(ifs) - _TABLE_IFS_SHOWN}개")
        # 소형 모델은 툴을 이어 부르지 못한다. 다음 질문을 사용자가 할 수 있게
        # 이 테이블에 데이터를 넣는 인터페이스(비었을 때 볼 곳)를 짚어 준다.
        first = next((i["id"] for i in ifs if "target" in i["roles"]), ifs[0]["id"])
        lines.append(f"오류 여부는 check_interface_errors 로 확인할 수 있다 "
                     f"(예: \"{first} 이 인터페이스 뭐가 문제야?\")")
        line = "\n".join(lines)
    if r["qualifier"]:
        line += f"\n(앞에 붙은 '{r['qualifier']}' 은(는) 떼고 이름 {name} 으로 찾았다)"
    if r["warnings"]:
        line += "\n확인 필요: " + "; ".join(r["warnings"])
    return line


# --------------------------------------------------------------------------
# 단계별 툴 (tier="step") — 챗봇에는 숨기고 다른 MCP 호스트에서 쓴다
# --------------------------------------------------------------------------


@register(
    label="오류 메일 목록 (iferr)",
    view="table",
    hint=(
        "list_error_mails 는 메일만 본다. DB 는 확인하지 않는다. "
        "period 에는 사용자가 말한 기간을 그대로 넣어라."
    ),
    read_only=True,
    tier="step",
)
def list_error_mails(period: str = "") -> str:
    """최근 오류 메일 목록을 본다. DB 는 조회하지 않는다.

    '메일이 오긴 왔는지'부터 확인할 때 사용한다.
    period 에는 사용자가 말한 기간을 그대로 넣는다("3일", "오늘").
    발신자 주소는 마스킹되어 나온다.
    """
    hours, label, warn = parse_period(period, MAIL_LOOKBACK_HOURS)
    r = list_mails(hours=hours, detail="summary")
    lines = [f"{label}({hours}시간) 메일 {r['mail_count']}통 (오류 {r['error_count']}통)"]
    if warn:
        lines.append(f"확인 필요: {warn}")
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
