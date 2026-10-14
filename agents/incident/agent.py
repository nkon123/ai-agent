"""incident — 인터페이스 오류 한 건을 영향도까지 끝까지 따라간다.

"오늘 인터페이스 오류 확인해줘" 한마디로 사람이 하던 조사를 이어서 한다.

    오류 메일 → IFID → 마스터의 테이블(출발·도착)            (iferr)
    → 메일의 오류 내용에서 원인 컬럼(ORA-12899 길이 초과 등)  (iferr.extract_issues)
    → 그 테이블·컬럼을 쓰는 소스와 줄마다 영향 판정          (impact.column)

로컬 소형 모델은 툴을 이어 부르지 못한다. 그래서 이어 붙이는 일을 모델이 아니라
이 에이전트가 한다. 각 단계는 원래 에이전트의 함수를 그대로 부른다 — 단계 툴로
따로 물어도 같은 답이 나온다.

원인 컬럼의 테이블을 정하는 순서:
    1. 오류 메시지에 테이블이 적혀 있으면 그것("ERP"."IF_ORDER_TMP"."ORD_NO")
    2. 없으면 마스터의 도착 테이블 — 실패하면 데이터가 안 들어가는 쪽이다
    3. 둘 다 없으면 조사하지 않고 '확인 필요'로 남긴다(테이블을 지어내지 않는다)

단독 실행:
    python agents/incident/agent.py --period 오늘
    python agents/incident/agent.py --key IF_ORD_RECV
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

_ROOT = Path(__file__).resolve().parents[2]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from agents.iferr.agent import (  # noqa: E402
    flow_text,
    issue_text,
    parse_period,
    run_iferr,
)
from agents.impact.column import (  # noqa: E402
    GRID_COLUMNS,
    VERDICT_LABEL,
    column_grid,
    run_column_impact,
)
from config import INCIDENT_MAX_IMPACTS, MAIL_LOOKBACK_HOURS  # noqa: E402
from core.cache import TTLCache  # noqa: E402
from core.progress import notify  # noqa: E402
from core.sqlstmt import bare_table_name  # noqa: E402


def targets_of(case: dict[str, Any]) -> tuple[list[dict[str, Any]], list[str]]:
    """케이스 하나에서 영향도를 조사할 (테이블, 컬럼) 과 경고.

    컬럼이 없는 원인(ORA-01438 처럼 컬럼 이름이 없는 메시지)은 조사하지 않고 알린다.
    """
    flows = case.get("flows") or []
    tar_tables = list(dict.fromkeys(
        bare_table_name(f.get("tar_table") or "")[0].upper()
        for f in flows if (f.get("tar_table") or "").strip()))
    out: list[dict[str, Any]] = []
    warnings: list[str] = []
    for issue in case.get("issues") or []:
        if not issue.get("column"):
            warnings.append(f"[{case['key']}] {issue_text(issue)} — 메일에 컬럼 이름이 없어 "
                            "영향도를 조사하지 못했다, 확인 필요")
            continue
        if issue.get("table"):
            table, how = bare_table_name(issue["table"])[0].upper(), "오류 메시지"
        elif len(tar_tables) == 1:
            table, how = tar_tables[0], "마스터 도착 테이블"
        else:
            why = "도착 테이블이 여럿이다" if tar_tables else "마스터에서 테이블을 찾지 못했다"
            warnings.append(f"[{case['key']}] {issue_text(issue)} — {why}. 테이블을 정하지 못해 "
                            "영향도를 조사하지 못했다, 확인 필요")
            continue
        out.append({"table": table, "column": issue["column"].upper(), "table_from": how,
                    "issue": issue, "issue_text": issue_text({**issue, "table": table})})
    return out, warnings


def _investigate(period: str, key: str) -> dict[str, Any]:
    hours, label, period_warn = parse_period(period, MAIL_LOOKBACK_HOURS)
    notify(f"메일 확인 중 ({label})" if not key else f"{key} 조회 중")
    r = run_iferr(hours=hours, key=key, detail="full")
    warnings: list[str] = ([period_warn] if period_warn else []) + list(r.get("warnings") or [])

    cases: list[dict[str, Any]] = []
    budget = INCIDENT_MAX_IMPACTS
    skipped = 0
    for c in r["cases"]:
        targets, t_warn = targets_of(c)
        warnings += t_warn
        impacts: list[dict[str, Any]] = []
        for t in targets:
            if budget <= 0:
                skipped += 1
                continue
            budget -= 1
            notify(f"{c['key']} — {t['issue_text']} 영향도 조사 중")
            ci = run_column_impact(t["table"], t["column"], detail="summary")
            impacts.append({**t, "result": ci})
            warnings += [f"[{c['key']} {t['table']}.{t['column']}] {w}" for w in ci["warnings"]]
        db = c.get("db") or {}
        cases.append({
            "key": c["key"], "status": db.get("status", "unknown"),
            "master_source": db.get("source", "db"), "impact": c.get("impact", ""),
            "rule": c.get("rule", ""), "flows": c.get("flows") or [],
            "flow_texts": [flow_text(f) for f in (c.get("flows") or [])[:3]],
            "issues": c.get("issues") or [], "impacts": impacts,
            "mail_count": len(c.get("mails") or []),
            "judged_by": c.get("judged_by", ""), "found_by": c.get("found_by", ""),
        })
    # 원인 컬럼까지 밝혀진 케이스가 먼저다 — 할 일이 분명한 쪽을 먼저 보인다.
    cases.sort(key=lambda c: not c["impacts"])
    if skipped:
        warnings.append(f"영향도 조사 상한({INCIDENT_MAX_IMPACTS}건)을 넘은 원인 컬럼 {skipped}건은 "
                        "조사하지 않았다 — 확인 필요 (INCIDENT_MAX_IMPACTS, 또는 컬럼 영향도를 따로 물을 것)")
    return {"period": period, "key": key, "hours": hours, "label": label,
            "mail_count": r.get("mail_count", 0), "cases": cases,
            "comment": r.get("comment") or "", "warnings": warnings}


# 툴 답 뒤에 [표로 보기] 를 누르면 리소스가 같은 결과를 읽는다. 툴은 늘 새로 조사하고
# (장애 대응 중에는 상태가 바뀐다) 결과를 여기에 넣어 둔다. 리소스만 이걸 쓴다.
_LAST = TTLCache(ttl=1800, maxsize=8)


def _cache_key(period: str, key: str) -> tuple[str, str]:
    return ((period or "").strip() or "-", (key or "").strip().upper() or "-")


def run_incident(period: str = "", key: str = "") -> dict[str, Any]:
    """진입점. 항상 새로 조사한다(그리드용으로 결과를 남겨 둔다)."""
    period = "" if (period or "").strip() == "-" else period
    key = "" if (key or "").strip() == "-" else (key or "").strip()
    r = _investigate(period, key)
    _LAST.set(_cache_key(period, key), r)
    return r


def incident_grid(period: str = "", key: str = "") -> dict[str, Any]:
    """조사 결과의 영향도를 한 표로. 방금 조사한 결과가 있으면 그것을 쓴다."""
    period = "" if (period or "").strip() == "-" else period
    key = "" if (key or "").strip() == "-" else (key or "").strip()
    r = _LAST.get(_cache_key(period, key)) or run_incident(period, key)
    rows: list[dict[str, Any]] = []
    for c in r["cases"]:
        for imp in c["impacts"]:
            # 컬럼 영향도 결과는 컬럼 쪽 캐시에 있다 — LLM 을 다시 돌리지 않는다.
            g = column_grid(imp["table"], imp["column"], "-")
            rows += [{"ifid": c["key"], "cause": imp["issue_text"], **row}
                     for row in g["grid"]["rows"]]
    impacts = [i for c in r["cases"] for i in c["impacts"]]
    counts = {k: sum(1 for row in rows if row["verdict"] == v) for k, v in VERDICT_LABEL.items()}
    return {
        "title": f"인터페이스 오류 영향도 — {r['label']}",
        "subtitle": (f"메일 {r['mail_count']}통 · 오류 {len(r['cases'])}건 · 원인 컬럼 {len(impacts)}개 · "
                     f"찾은 줄 {len(rows)}개 (영향 있음 {counts['yes']} / 확인 필요 "
                     f"{counts['unknown']} / 영향 없음 {counts['no']})"),
        "grid": {"columns": [{"key": "ifid", "label": "IFID"}, {"key": "cause", "label": "원인"}]
                 + [{"key": k, "label": v} for k, v in GRID_COLUMNS],
                 "rows": rows, "filter_key": "verdict",
                 "tones": {VERDICT_LABEL["yes"]: "bad", VERDICT_LABEL["unknown"]: "warn",
                           VERDICT_LABEL["no"]: "muted"}},
        "warnings": r["warnings"],
    }


if __name__ == "__main__":
    import argparse

    ap = argparse.ArgumentParser(description="인터페이스 오류 → 원인 컬럼 → 영향도")
    ap.add_argument("--period", default="", help="'오늘', '3일' 처럼")
    ap.add_argument("--key", default="", help="메일을 읽지 않고 이 IFID 만")
    args = ap.parse_args()

    res = run_incident(args.period, args.key)
    print(f"{res['label']}({res['hours']}시간) 메일 {res['mail_count']}통, 오류 {len(res['cases'])}건")
    for c in res["cases"]:
        print(f"- {c['key']}: {c['status']} {' / '.join(c['flow_texts'])} (마스터: {c['master_source']})")
        for imp in c["impacts"]:
            ci = imp["result"]
            n = ci["counts"]
            print(f"    원인: {imp['issue_text']} ({imp['table_from']})")
            print(f"    영향도: {ci['hit_count']}줄 — 영향 있음 {n['yes']} / 확인 필요 {n['unknown']}"
                  f" / 영향 없음 {n['no']}")
            for p in ci["places"]:
                print(f"      · {p}")
    for w in res["warnings"]:
        print(f"  [확인 필요] {w}")
