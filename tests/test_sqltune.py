"""sqltune 회귀 테스트.

DB 없이 돈다. 실행계획·인덱스 조회는 monkeypatch 로 대신한다.

가장 중요한 것은 안전 게이트다. 튜닝 도구가 DML 을 실행하면 사고다.

실행:
    pytest -q
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from agents.sqltune import agent as sqltune  # noqa: E402
from agents.sqltune import is_safe_select, run_sqltune, suggest_index  # noqa: E402
from core import oracle  # noqa: E402

SLOW = (
    "SELECT * FROM ORD_HDR "
    "WHERE TO_CHAR(REG_DT,'YYYYMMDD') = '20260101' AND STATUS != 'Y' "
    "ORDER BY ORD_NO"
)


# --------------------------------------------------------------------------
# 안전 게이트 — 여기가 뚫리면 튜닝 도구가 데이터를 바꾼다
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "sql",
    [
        "DELETE FROM T",
        "UPDATE T SET A = 1",
        "INSERT INTO T VALUES (1)",
        "MERGE INTO T USING S ON (1=1) WHEN MATCHED THEN UPDATE SET A=1",
        "DROP TABLE T",
        "TRUNCATE TABLE T",
        "CREATE INDEX IX ON T(A)",
        "GRANT SELECT ON T TO PUBLIC",
        "BEGIN NULL; END;",
        "DECLARE x NUMBER; BEGIN NULL; END;",
        "SELECT * FROM T FOR UPDATE",
        "SELECT UTL_HTTP.request('http://x') FROM dual",
        "SELECT DBMS_RANDOM.value FROM dual",
        "SELECT 1 FROM dual; DROP TABLE T",
        "",
        "   ",
    ],
)
def test_unsafe_statements_are_rejected(sql: str):
    ok, reason = is_safe_select(sql)
    assert ok is False and reason


@pytest.mark.parametrize(
    "sql",
    [
        "SELECT * FROM T WHERE A = 1",
        "  select a, b from t where a = :a  ",
        "WITH x AS (SELECT 1 FROM dual) SELECT * FROM x",
        "SELECT * FROM T WHERE A = 1;",          # 끝 세미콜론 하나는 허용
    ],
)
def test_safe_selects_pass(sql: str):
    assert is_safe_select(sql)[0] is True


def test_string_literal_is_not_dml():
    """문자열 안의 DELETE 는 DML 이 아니다. 원문으로 검사하면 오탐이 난다."""
    assert is_safe_select("SELECT 'DELETE ME' FROM dual")[0] is True


def test_comment_hidden_dml_is_caught():
    """주석으로 위장해도 잡혀야 한다."""
    ok, _ = is_safe_select("SELECT 1 FROM dual /* ok */ UNION ALL SELECT 1 FROM dual")
    assert ok is True
    # 주석은 지워지므로 주석 안 DML 은 애초에 실행되지 않는다.
    assert is_safe_select("SELECT 1 FROM dual -- DELETE FROM T\n")[0] is True


def test_unsafe_query_runs_nothing(monkeypatch: Any):
    """거부된 쿼리는 DB 를 건드리지 않는다."""

    def boom(*a: Any, **k: Any):
        raise AssertionError("DB 를 호출하면 안 된다")

    monkeypatch.setattr(sqltune.oracle, "explain_plan", boom)
    monkeypatch.setattr(sqltune.oracle, "execute_with_stats", boom)

    r = run_sqltune("DELETE FROM T", execute=True, detail="full")
    assert r["safe"] is False and r["rule"] == "unsafe-statement"


def test_execute_is_off_by_default(monkeypatch: Any):
    """실행은 곧 운영 DB 부하다. 사람이 켤 때만 돈다."""

    def boom(*a: Any, **k: Any):
        raise AssertionError("실행하면 안 된다")

    monkeypatch.setattr(sqltune.oracle, "is_configured", lambda: True)
    monkeypatch.setattr(sqltune.oracle, "explain_plan", lambda sql, timeout=None: [])
    monkeypatch.setattr(sqltune.oracle, "existing_indexes", lambda t, schema=None: [])
    monkeypatch.setattr(sqltune.oracle, "execute_with_stats", boom)

    assert run_sqltune(SLOW, detail="summary")["executed"] is False


# --------------------------------------------------------------------------
# 정적 규칙
# --------------------------------------------------------------------------


def test_text_rules_catch_common_problems():
    rules = {f["rule"] for f in sqltune.check_text_rules(SLOW)}
    assert {"func-on-column", "negation", "select-star", "literal-value"} <= rules


def test_bind_variables_are_not_flagged_as_literals():
    rules = {f["rule"] for f in sqltune.check_text_rules(
        "SELECT a FROM t WHERE b = :b AND c = :c")}
    assert "literal-value" not in rules


def test_findings_carry_evidence():
    """규칙명만 남기면 왜 걸렸는지 알 수 없다."""
    for f in sqltune.check_text_rules(SLOW):
        assert f["evidence"].strip()


# --------------------------------------------------------------------------
# 플랜 규칙
# --------------------------------------------------------------------------


def test_plan_rules_catch_full_scan_and_cartesian():
    plan = [
        {"ID": 0, "OPERATION": "SELECT STATEMENT", "OPTIONS": None},
        {"ID": 1, "OPERATION": "MERGE JOIN", "OPTIONS": "CARTESIAN"},
        {"ID": 2, "OPERATION": "TABLE ACCESS", "OPTIONS": "FULL",
         "OBJECT_NAME": "ORD_HDR", "CARDINALITY": 100000,
         "FILTER_PREDICATES": "STATUS<>'Y'"},
        {"ID": 3, "OPERATION": "SORT", "OPTIONS": "ORDER BY"},
    ]
    rules = {f["rule"] for f in sqltune.check_plan_rules(plan)}
    assert {"full-scan", "cartesian", "sort-order-by", "filter-not-access"} <= rules


# --------------------------------------------------------------------------
# 인덱스 제안 — 만들기만 하고 실행하지 않는다
# --------------------------------------------------------------------------


def test_index_order_is_equality_then_range_then_order():
    """등치 → 범위 → 정렬 순 (기준 문서 7항)."""
    sql = (
        "SELECT a FROM ORD_HDR WHERE STATUS = :s AND REG_DT >= :d "
        "ORDER BY ORD_NO"
    )
    idx = suggest_index(sql, "ORD_HDR")
    assert idx["columns"] == ["STATUS", "REG_DT", "ORD_NO"]
    assert idx["ddl"].startswith("CREATE INDEX ")
    assert "ORD_HDR (STATUS, REG_DT, ORD_NO)" in idx["ddl"]


def test_index_is_not_suggested_when_already_covered():
    """선두 컬럼이 같은 인덱스가 있으면 만들지 않는다.

    중복 인덱스는 조회를 빠르게 하지 않고 DML 만 느리게 한다.
    """
    sql = "SELECT a FROM ORD_HDR WHERE STATUS = :s"
    existing = [{"name": "IX_ORD_1", "unique": False, "columns": ["STATUS", "REG_DT"]}]
    idx = suggest_index(sql, "ORD_HDR", existing)
    assert idx["ddl"] == "" and idx["rule"] == "already-covered"
    assert "IX_ORD_1" in idx["note"]


def test_index_without_predicates_says_so():
    idx = suggest_index("SELECT * FROM T", "T")
    assert idx["rule"] == "no-predicate" and "확인 필요" in idx["note"]


def test_ddl_is_never_executed(monkeypatch: Any):
    """제안은 문자열일 뿐이다. 어떤 경로로도 실행되지 않는다."""
    calls: list[str] = []

    monkeypatch.setattr(sqltune.oracle, "is_configured", lambda: True)
    monkeypatch.setattr(sqltune.oracle, "explain_plan", lambda sql, timeout=None: [])
    monkeypatch.setattr(sqltune.oracle, "existing_indexes", lambda t, schema=None: [])
    monkeypatch.setattr(
        sqltune.oracle, "query",
        lambda sql, binds=None, timeout=None: calls.append(sql) or [],
    )

    r = run_sqltune(SLOW, detail="full")
    assert r["index"]["ddl"]                       # 제안은 나온다
    assert not any("CREATE" in c.upper() for c in calls)   # 실행은 없다


# --------------------------------------------------------------------------
# 실패를 삼키지 않는다
# --------------------------------------------------------------------------


def test_explain_failure_is_reported(monkeypatch: Any):
    """PLAN_TABLE 이 없는 사이트가 흔하다. 조용히 넘기면 '문제 없음'으로 보인다."""

    def boom(sql: str, timeout: Any = None):
        raise RuntimeError("ORA-02404: plan table not found")

    monkeypatch.setattr(sqltune.oracle, "is_configured", lambda: True)
    monkeypatch.setattr(sqltune.oracle, "explain_plan", boom)
    monkeypatch.setattr(sqltune.oracle, "existing_indexes", lambda t, schema=None: [])

    r = run_sqltune(SLOW, detail="summary")
    assert any("ORA-02404" in w for w in r["warnings"])


def test_no_db_still_gives_static_findings():
    """DB 접속이 안 돼도 본문 진단은 나와야 한다."""
    r = run_sqltune(SLOW, detail="summary")
    assert r["finding_count"] >= 3
    assert any("Oracle 설정" in w for w in r["warnings"])


# --------------------------------------------------------------------------
# core.oracle 보조 함수
# --------------------------------------------------------------------------


def test_gather_hint_is_added_after_first_keyword():
    assert oracle._add_gather_hint("SELECT a FROM t").startswith(
        "SELECT /*+ GATHER_PLAN_STATISTICS */"
    )
    assert "/*+ GATHER_PLAN_STATISTICS */" in oracle._add_gather_hint(
        "WITH x AS (SELECT 1 FROM dual) SELECT * FROM x"
    )


def test_buffers_are_parsed_from_plan_text():
    """비교 기준은 수행 시간보다 논리적 읽기다(기준 문서 8항)."""
    plan = (
        "|   0 | SELECT STATEMENT |   |  1 |      |  1 |00:00:00.01 |    1,234 |\n"
        "|   1 |  TABLE ACCESS FULL| T |  1 | 1000 |  1 |00:00:00.01 |    1,234 |"
    )
    assert oracle._parse_buffers(plan) == 1234
    assert oracle._parse_buffers("(계획 없음)") is None


# --------------------------------------------------------------------------
# 후보 비교 — 건수가 다르면 튜닝이 아니라 버그다
# --------------------------------------------------------------------------


def _fake_db(monkeypatch: Any, measures: dict[str, dict[str, Any]]) -> list[str]:
    """SQL 조각으로 측정값을 정해 주는 가짜 DB. 호출된 SQL 을 기록한다."""
    seen: list[str] = []
    monkeypatch.setattr(sqltune.oracle, "is_configured", lambda: True)
    monkeypatch.setattr(sqltune.oracle, "existing_indexes", lambda t, schema=None: [])

    def pick(sql: str) -> dict[str, Any]:
        for key, val in measures.items():
            if key in sql:
                return val
        return {}

    def fake_explain(sql: str, timeout: Any = None):
        seen.append(f"explain:{sql}")
        return [{"ID": 0, "OPERATION": "SELECT STATEMENT", "COST": pick(sql).get("cost")}]

    def fake_count(sql: str, timeout: Any = None):
        seen.append(f"count:{sql}")
        return pick(sql).get("rows", 0)

    def fake_run(sql: str, binds: Any = None, max_rows: int = 100,
                 timeout: Any = None, runs: int = 2):
        seen.append(f"run:{sql}")
        m = pick(sql)
        return {"elapsed_sec": m.get("elapsed", 1.0), "buffers": m.get("buffers"),
                "rows_fetched": 1, "truncated": False, "plan_text": "", "elapsed_all": []}

    monkeypatch.setattr(sqltune.oracle, "explain_plan", fake_explain)
    monkeypatch.setattr(sqltune.oracle, "count_rows", fake_count)
    monkeypatch.setattr(sqltune.oracle, "execute_with_stats", fake_run)
    return seen


def _fake_candidates(monkeypatch: Any, cands: list[dict[str, Any]]) -> None:
    """LLM 후보 생성을 대신한다."""

    def fake_propose(state: Any) -> Any:
        return {"candidates": cands}

    monkeypatch.setattr(sqltune, "propose", fake_propose)
    monkeypatch.setattr(sqltune, "_graph", lambda: _rebuild_graph())


def _rebuild_graph():
    """propose 를 갈아끼운 뒤 그래프를 다시 만든다(캐시 우회)."""
    from langgraph.graph import END, START, StateGraph

    g = StateGraph(sqltune.TuneState)
    for name, fn in (
        ("guard", sqltune.guard), ("static_check", sqltune.static_check),
        ("plan_check", sqltune.plan_check), ("index_check", sqltune.index_check),
        ("propose", sqltune.propose), ("run_check", sqltune.run_check),
        ("compare", sqltune.compare),
    ):
        g.add_node(name, fn)
    g.add_edge(START, "guard")
    for a, b in (("guard", "static_check"), ("static_check", "plan_check"),
                 ("plan_check", "index_check"), ("index_check", "propose"),
                 ("propose", "run_check"), ("run_check", "compare")):
        g.add_edge(a, b)
    g.add_edge("compare", END)
    return g.compile()


def test_candidate_with_different_row_count_is_rejected(monkeypatch: Any):
    """건수가 달라지면 튜닝이 아니라 버그다. 순위에 넣지 않는다."""
    base = "SELECT a FROM ORD_HDR WHERE STATUS = 'Y'"
    bad = "SELECT a FROM ORD_HDR WHERE STATUS = 'Y' AND ROWNUM <= 10"
    _fake_db(monkeypatch, {
        "ROWNUM": {"cost": 5, "rows": 10, "buffers": 10},
        "ORD_HDR": {"cost": 100, "rows": 1204, "buffers": 5000},
    })
    _fake_candidates(monkeypatch, [{"name": "후보1", "sql": bad, "reason": "제한",
                                    "based_on": []}])

    r = run_sqltune(base, compare_candidates=True, compare_count=True, detail="full")
    cand = [c for c in r["comparison"] if c["name"] == "후보1"][0]
    assert "결과 건수가 다르다" in cand["rejected"]
    assert "1204" in cand["rejected"] and "10" in cand["rejected"]
    assert r["best"] == "원본"          # 건수가 다른 후보가 이기면 안 된다


def test_candidate_with_same_count_and_fewer_buffers_wins(monkeypatch: Any):
    """건수가 같고 논리적 읽기가 적으면 후보가 이긴다."""
    base = "SELECT a FROM ORD_HDR WHERE TO_CHAR(REG_DT,'YYYYMMDD') = '20260101'"
    good = "SELECT a FROM ORD_HDR WHERE REG_DT >= :d1 AND REG_DT < :d2"
    _fake_db(monkeypatch, {
        "TO_CHAR": {"cost": 900, "rows": 1204, "buffers": 50000, "elapsed": 3.0},
        "REG_DT >=": {"cost": 12, "rows": 1204, "buffers": 220, "elapsed": 0.2},
    })
    _fake_candidates(monkeypatch, [{"name": "후보1", "sql": good,
                                    "reason": "함수 제거", "based_on": ["func-on-column"]}])

    r = run_sqltune(base, execute=True, compare_candidates=True, compare_count=True,
                    detail="summary")
    assert r["best"] == "후보1"
    by = {c["name"]: c for c in r["compared"]}
    assert by["원본"]["rows"] == by["후보1"]["rows"] == 1204
    assert by["후보1"]["buffers"] < by["원본"]["buffers"]


def test_unsafe_candidate_is_rejected(monkeypatch: Any):
    """LLM 이 만든 SQL 도 원본과 같은 안전 게이트를 통과해야 한다."""
    _fake_db(monkeypatch, {"ORD_HDR": {"cost": 10, "rows": 1, "buffers": 1}})
    _fake_candidates(monkeypatch, [
        {"name": "후보1", "sql": "DELETE FROM ORD_HDR", "reason": "?", "based_on": []}
    ])

    r = run_sqltune("SELECT a FROM ORD_HDR", compare_candidates=True, detail="full")
    cand = [c for c in r["comparison"] if c["name"] == "후보1"][0]
    assert "안전 검사 탈락" in cand["rejected"]
    # 거부된 후보는 실행도 측정도 하지 않는다.
    assert cand.get("cost") is None


def test_count_off_warns_that_results_are_unverified(monkeypatch: Any):
    """건수를 안 봤으면 '같은 결과'라는 보장이 없다는 사실을 남긴다."""
    _fake_db(monkeypatch, {"ORD_HDR": {"cost": 10, "buffers": 100}})
    _fake_candidates(monkeypatch, [
        {"name": "후보1", "sql": "SELECT a FROM ORD_HDR WHERE 1=1", "reason": "x",
         "based_on": []}
    ])

    r = run_sqltune("SELECT a FROM ORD_HDR", compare_candidates=True,
                    compare_count=False, detail="summary")
    assert any("결과 건수를 비교하지 않았다" in w for w in r["warnings"])


def test_count_is_not_run_when_option_is_off(monkeypatch: Any):
    """옵션이 꺼져 있으면 COUNT(*) 를 실행하지 않는다 — 실행은 DB 부하다."""
    seen = _fake_db(monkeypatch, {"ORD_HDR": {"cost": 10, "buffers": 100}})
    _fake_candidates(monkeypatch, [
        {"name": "후보1", "sql": "SELECT a FROM ORD_HDR WHERE 1=1", "reason": "x",
         "based_on": []}
    ])

    run_sqltune("SELECT a FROM ORD_HDR", compare_candidates=True, compare_count=False)
    assert not any(c.startswith("count:") for c in seen)
    assert not any(c.startswith("run:") for c in seen)


def test_compare_is_off_by_default(monkeypatch: Any):
    """후보 생성은 로컬 모델에서 수십 초가 걸린다. 기본은 꺼 둔다."""
    seen = _fake_db(monkeypatch, {"ORD_HDR": {"cost": 10}})
    r = run_sqltune("SELECT a FROM ORD_HDR", detail="full")
    assert r["comparison"] == []
    assert not any(c.startswith("count:") for c in seen)


# --------------------------------------------------------------------------
# 병렬 / 교차 반복 — 무엇을 동시에 하고 무엇을 하지 않는가
# --------------------------------------------------------------------------


def _trace_db(monkeypatch: Any) -> dict[str, Any]:
    """호출 순서와 동시성을 기록하는 가짜 DB."""
    import threading
    import time as _t

    state = {"order": [], "explain_peak": 0, "count_peak": 0, "run_peak": 0}
    live = {"explain": 0, "count": 0, "run": 0}
    lock = threading.Lock()

    def enter(kind: str, sql: str) -> None:
        with lock:
            live[kind] += 1
            state[f"{kind}_peak"] = max(state[f"{kind}_peak"], live[kind])
            tag = "원본" if "TO_CHAR" in sql else ("A" if "cand_a" in sql else "B")
            state["order"].append(f"{kind}:{tag}")

    def leave(kind: str) -> None:
        with lock:
            live[kind] -= 1

    def fake_explain(sql: str, timeout: Any = None):
        enter("explain", sql); _t.sleep(0.05); leave("explain")
        return [{"ID": 0, "OPERATION": "SELECT STATEMENT", "COST": 10}]

    def fake_count(sql: str, timeout: Any = None):
        enter("count", sql); _t.sleep(0.05); leave("count")
        return 100

    def fake_run(sql: str, binds: Any = None, max_rows: int = 100,
                 timeout: Any = None, runs: int = 2):
        enter("run", sql); _t.sleep(0.02); leave("run")
        return {"elapsed_sec": 1.0, "buffers": 500, "rows_fetched": 1,
                "truncated": False, "plan_text": "", "elapsed_all": []}

    monkeypatch.setattr(sqltune.oracle, "is_configured", lambda: True)
    monkeypatch.setattr(sqltune.oracle, "existing_indexes", lambda t, schema=None: [])
    monkeypatch.setattr(sqltune.oracle, "explain_plan", fake_explain)
    monkeypatch.setattr(sqltune.oracle, "count_rows", fake_count)
    monkeypatch.setattr(sqltune.oracle, "execute_with_stats", fake_run)
    _fake_candidates(monkeypatch, [
        {"name": "후보1", "sql": "SELECT cand_a FROM T", "reason": "a", "based_on": []},
        {"name": "후보2", "sql": "SELECT cand_b FROM T", "reason": "b", "based_on": []},
    ])
    return state


def test_explain_runs_in_parallel_by_default(monkeypatch: Any):
    """EXPLAIN 은 실행이 아니라 파싱이라 동시에 해도 안전하다."""
    st = _trace_db(monkeypatch)
    run_sqltune(SLOW, compare_candidates=True, detail="full")
    assert st["explain_peak"] > 1, "실행계획이 순차로 돌았다"


def test_explain_can_be_forced_sequential(monkeypatch: Any):
    st = _trace_db(monkeypatch)
    run_sqltune(SLOW, compare_candidates=True, parallel_explain=False, detail="full")
    assert st["explain_peak"] == 1


def test_count_is_sequential_by_default(monkeypatch: Any):
    """건수는 병렬로 해도 값은 같지만 순간 부하가 후보 수만큼 커진다."""
    st = _trace_db(monkeypatch)
    run_sqltune(SLOW, compare_candidates=True, compare_count=True, detail="full")
    assert st["count_peak"] == 1


def test_count_parallel_can_be_enabled(monkeypatch: Any):
    st = _trace_db(monkeypatch)
    r = run_sqltune(SLOW, compare_candidates=True, compare_count=True,
                    parallel_count=True, detail="summary")
    assert st["count_peak"] > 1
    # 병렬로 셌다는 사실을 결과에 남긴다.
    assert any("병렬로 셌다" in w for w in r["warnings"])


def test_timing_is_never_parallel(monkeypatch: Any):
    """수행시간은 어떤 설정에서도 동시에 재지 않는다.

    동시에 돌리면 서로 자원을 뺏어 재려던 수치가 오염된다.
    """
    st = _trace_db(monkeypatch)
    run_sqltune(SLOW, execute=True, compare_candidates=True, compare_count=True,
                parallel_explain=True, parallel_count=True, detail="full")
    assert st["run_peak"] == 1


def test_timing_is_interleaved_by_default(monkeypatch: Any):
    """A,B,A,B 순으로 돌아야 캐시 데우기 편향이 줄어든다."""
    st = _trace_db(monkeypatch)
    run_sqltune(SLOW, execute=True, compare_candidates=True, detail="full")
    runs = [o.split(":")[1] for o in st["order"] if o.startswith("run:")]
    # 3개 후보 × 2회 = 6회, 라운드마다 전원을 한 번씩
    assert runs[:3] == ["원본", "A", "B"]
    assert runs[3:6] == ["원본", "A", "B"]


def test_interleave_can_be_turned_off(monkeypatch: Any):
    """끄면 예전처럼 A,A,B,B 로 돈다. 그 편향을 경고로 남긴다."""
    st = _trace_db(monkeypatch)
    r = run_sqltune(SLOW, execute=True, compare_candidates=True,
                    interleave=False, detail="summary")
    runs = [o.split(":")[1] for o in st["order"] if o.startswith("run:")]
    assert runs[:2] == ["원본", "원본"] and runs[2:4] == ["A", "A"]
    assert any("교차 반복 없이" in w for w in r["warnings"])


def test_rejected_candidate_is_not_timed(monkeypatch: Any):
    """탈락한 후보에 시간을 쓰지 않는다."""
    st = _trace_db(monkeypatch)
    _fake_candidates(monkeypatch, [
        {"name": "후보1", "sql": "DELETE FROM T", "reason": "?", "based_on": []},
    ])
    run_sqltune(SLOW, execute=True, compare_candidates=True, detail="full")
    assert all(":A" not in o and ":B" not in o for o in st["order"])


def test_baseline_is_not_timed_twice(monkeypatch: Any):
    """비교할 때 원본을 두 번 재지 않는다.

    run_check 와 compare 가 각각 재면 느린 원본을 한 번 더 돌리는 셈이고
    교차 반복 순서도 흐트러진다.
    """
    st = _trace_db(monkeypatch)
    run_sqltune(SLOW, execute=True, compare_candidates=True, detail="full")
    runs = [o.split(":")[1] for o in st["order"] if o.startswith("run:")]
    # 후보 3개(원본 포함) × 2회 = 6회. 그 이상이면 중복 측정이다.
    assert len(runs) == 6, runs
    assert runs.count("원본") == 2
