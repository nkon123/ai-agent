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
