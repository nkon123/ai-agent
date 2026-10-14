"""컬럼 영향도(agents/impact/column.py) 회귀 테스트.

samples/colimpact/ 는 TOTAL_AMT 가 IF_ORDER_TMP 와 ORD_HDR 양쪽에 있는 가짜 소스다.
'같은 이름 컬럼이 다른 테이블에도 있다'가 이 기능이 푸는 문제라 그렇게 만들었다.
LLM 은 가짜로 바꿔 끼운다 — 실제 모델 정확도는 README 의 실측을 볼 것.

실행:
    pytest -q
"""

from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path
from typing import Any

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from mcp.client.client import Client  # noqa: E402

from agents.impact import column  # noqa: E402
from agents.usage import agent as usage_agent  # noqa: E402
from mcp_server.tools import build_server  # noqa: E402

EVAL = str(Path(__file__).resolve().parents[1] / "samples" / "colimpact")


@pytest.fixture(autouse=True)
def rules_only(monkeypatch: Any) -> None:
    """기본은 규칙만. 결과 캐시는 테스트마다 비운다(앞 테스트의 결과가 새지 않게)."""
    monkeypatch.setattr(column, "USE_LLM", False)
    monkeypatch.setattr(usage_agent, "SOURCE_ROOTS", {"EVAL": EVAL})
    column._RESULTS.clear()


def _hits(table: str = "IF_ORDER_TMP", col: str = "TOTAL_AMT") -> dict[tuple[str, int], dict]:
    r = column.run_column_impact(table, col, "EVAL")
    return {(h["program"], h["line"]): h for h in r["hits"]}


class FakeLLM:
    """invoke_structured 대신. '>>' 줄을 보고 정해 둔 답을 준다."""

    def __init__(self, answers: dict[str, tuple[str, str]] | None = None, fail: bool = False):
        self.answers = answers or {}
        self.fail = fail
        self.prompts: list[str] = []

    def __call__(self, schema: Any, prompt: str, model: Any = None, reasoning: Any = None):
        self.prompts.append(prompt)
        if self.fail:
            raise ConnectionError("ollama down")
        target = next(line for line in prompt.splitlines() if line.startswith(">>"))
        kind, owner = next((v for k, v in self.answers.items() if k in target),
                           ("unknown", ""))
        return schema(reason="가짜 근거", kind=kind, owner_table=owner,
                      usage="select" if kind == "column" else "none"), ""


def _use_llm(monkeypatch: Any, fake: FakeLLM) -> None:
    monkeypatch.setattr(column, "USE_LLM", True)
    mod = type(sys)("core.llm")
    mod.invoke_structured = fake  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "core.llm", mod)


# --------------------------------------------------------------------------
# 이름 정리
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "table, col, want",
    [
        ("if_order_tmp", "total_amt", ("IF_ORDER_TMP", "TOTAL_AMT", "")),
        ("", "IF_ORDER_TMP.TOTAL_AMT", ("IF_ORDER_TMP", "TOTAL_AMT", "")),
        ("EMP.IF_ORDER_TMP", "T.TOTAL_AMT", ("IF_ORDER_TMP", "TOTAL_AMT", "EMP")),
    ],
)
def test_split_names(table: str, col: str, want: tuple[str, str, str]):
    assert column.split_names(table, col) == want


@pytest.mark.parametrize(
    "kind, owner, want",
    [
        ("column", "IF_ORDER_TMP", "yes"),
        ("column", "emp.if_order_tmp", "yes"),     # 스키마·대소문자는 떼고 비교
        ("column", "ORD_HDR", "no"),
        ("column", "", "unknown"),                 # 컬럼이라면서 테이블을 못 댔다
        ("variable", "", "no"),
        ("text", "", "no"),
        ("unknown", "IF_ORDER_TMP", "unknown"),
    ],
)
def test_verdict_is_decided_by_code_not_by_llm(kind: str, owner: str, want: str):
    """LLM 에게는 '어느 테이블의 컬럼인가'만 묻고 비교는 코드가 한다."""
    assert column.verdict_of(kind, owner, "IF_ORDER_TMP") == want


# --------------------------------------------------------------------------
# 찾기와 규칙 분류
# --------------------------------------------------------------------------


def test_every_hit_in_table_files_is_listed():
    """판정과 상관없이 테이블을 쓰는 파일에서 컬럼이 나온 줄은 전부 나온다."""
    hits = _hits()
    assert set(hits) == {
        ("ord_join.sql", 1), ("ord_join.sql", 4), ("ord_join.sql", 5), ("ord_join.sql", 10),
        ("ord_join.sql", 13), ("ord_join.sql", 20),
        ("ord_load.pc", 1), ("ord_load.pc", 4), ("ord_load.pc", 9), ("ord_load.pc", 14),
        ("ord_load.pc", 15), ("OrdDao.java", 6),
    }


def test_comment_is_no_by_rule_without_llm():
    h = _hits()[("ord_load.pc", 1)]
    assert h["place"] == "comment" and h["verdict"] == "no" and h["decided_by"] == "rule"


def test_rule_places():
    hits = _hits()
    assert hits[("ord_join.sql", 5)]["place"] == "same-stmt"
    # Java 는 SQL 이 문자열 안에 있다 — 문자열이 아니라 같은 문장으로 본다.
    assert hits[("OrdDao.java", 6)]["place"] == "same-stmt"
    # UPDATE ORD_HDR 문장에는 IF_ORDER_TMP 가 없다.
    assert hits[("ord_join.sql", 20)]["place"] == "other-code"
    assert hits[("ord_join.sql", 20)]["verdict"] == "unknown"


def test_string_only_hit_in_proc_is_unknown(tmp_path: Path, monkeypatch: Any):
    """Pro*C 문자열 안은 로그일 수도 동적 SQL 일 수도 있다 — 단정하지 않는다."""
    (tmp_path / "a.pc").write_text(
        'void f(void) {\n  EXEC SQL DELETE FROM IF_ORDER_TMP;\n'
        '  printf("TOTAL_AMT 오류");\n}\n', encoding="utf-8")
    monkeypatch.setattr(usage_agent, "SOURCE_ROOTS", {"T": str(tmp_path)})
    h = column.run_column_impact("IF_ORDER_TMP", "TOTAL_AMT", "T")["hits"][0]
    assert h["place"] == "string" and h["verdict"] == "unknown"


def test_file_mentioning_table_only_in_comment_is_skipped(tmp_path: Path, monkeypatch: Any):
    (tmp_path / "a.sql").write_text(
        "-- IF_ORDER_TMP 에서 옮겨 온다\nUPDATE ORD_HDR SET TOTAL_AMT = 0;\n", encoding="utf-8")
    monkeypatch.setattr(usage_agent, "SOURCE_ROOTS", {"T": str(tmp_path)})
    r = column.run_column_impact("IF_ORDER_TMP", "TOTAL_AMT", "T")
    assert r["table_files"] == 0 and r["rule"] == "no-table-hit" and not r["hits"]


def test_all_roots_are_searched_by_default(tmp_path: Path, monkeypatch: Any):
    """설정한 루트는 모두 검색 대상이다. 두 번째 루트에만 있는 사용처도 나와야 한다."""
    second = tmp_path / "mes"
    second.mkdir()
    (second / "mes_load.sql").write_text(
        "UPDATE IF_ORDER_TMP SET TOTAL_AMT = 0;\n", encoding="utf-8")
    monkeypatch.setattr(usage_agent, "SOURCE_ROOTS", {"EVAL": EVAL, "MES": str(second)})
    r = column.run_column_impact("IF_ORDER_TMP", "TOTAL_AMT", detail="summary")
    assert r["root"] == "EVAL, MES"
    full = column.run_column_impact("IF_ORDER_TMP", "TOTAL_AMT")
    assert {h["root"] for h in full["hits"]} == {"EVAL", "MES"}
    grid = column.column_grid("IF_ORDER_TMP", "TOTAL_AMT", "-")
    assert any(row["root"] == "MES" and row["program"] == "mes_load.sql"
               for row in grid["grid"]["rows"])
    # 루트가 여럿이면 챗봇 답의 위치에 루트를 붙인다(같은 파일 이름이 루트마다 있을 수 있다).
    assert any(p.startswith("[MES] mes_load.sql:1") for p in r["places"])


def test_unknown_root_is_not_empty_result():
    r = column.run_column_impact("IF_ORDER_TMP", "TOTAL_AMT", "NOPE", detail="summary")
    assert r["rule"] == "unknown-root" and r["warnings"]


# --------------------------------------------------------------------------
# LLM 판정
# --------------------------------------------------------------------------


def test_llm_judges_each_line_separately(monkeypatch: Any):
    """같은 문장 안에서도 줄마다 답이 다르다 — INSERT 목록은 ORD_HDR, SELECT 는 IF_ORDER_TMP."""
    fake = FakeLLM({"INSERT INTO ORD_HDR": ("column", "ORD_HDR"),
                    "T.TOTAL_AMT": ("column", "IF_ORDER_TMP")})
    _use_llm(monkeypatch, fake)
    hits = _hits()
    assert hits[("ord_join.sql", 4)]["verdict"] == "no"
    assert hits[("ord_join.sql", 5)]["verdict"] == "yes"
    assert hits[("ord_join.sql", 5)]["usage"] == "select"
    # 규칙(같은 문장 → 영향 있음)과 LLM 이 갈린 줄은 표시한다.
    assert hits[("ord_join.sql", 4)]["conflict"] is True
    # 주석은 LLM 에 보내지 않는다.
    assert not any("ord_load.pc" in p and ">>    1|" in p for p in fake.prompts)
    assert len(fake.prompts) == 10          # 12줄 중 주석 2줄 제외


def test_prompt_marks_target_line_inside_whole_statement(monkeypatch: Any):
    fake = FakeLLM()
    _use_llm(monkeypatch, fake)
    _hits()
    p = next(p for p in fake.prompts if ">>    4|" in p and "ord_join.sql" in p)
    assert "     5|   SELECT T.ORD_NO, T.TOTAL_AMT" in p and "FROM IF_ORDER_TMP T" in p


def test_llm_limit_keeps_rest_with_rule_verdict(monkeypatch: Any):
    fake = FakeLLM()
    _use_llm(monkeypatch, fake)
    monkeypatch.setattr(column, "COLUMN_IMPACT_MAX_LLM", 2)
    r = column.run_column_impact("IF_ORDER_TMP", "TOTAL_AMT", "EVAL")
    assert len(fake.prompts) == 2
    assert len(r["hits"]) == 12                         # 버리지 않는다
    assert any("상한" in w for w in r["warnings"])
    # 테이블과 같은 문장이 먼저 판정된다.
    judged = [h for h in r["hits"] if h["decided_by"] == "llm"]
    assert all(h["place"] == "same-stmt" for h in judged)


def test_llm_down_is_unknown_and_not_cached(monkeypatch: Any):
    fake = FakeLLM(fail=True)
    _use_llm(monkeypatch, fake)
    r = column.run_column_impact("IF_ORDER_TMP", "TOTAL_AMT", "EVAL")
    # 연달아 두 번 실패하면 나머지를 기다리지 않는다.
    assert len(fake.prompts) == 2
    judged = [h for h in r["hits"] if h["place"] != "comment"]
    assert all(h["verdict"] == "unknown" and h["decided_by"] == "llm-failed" for h in judged)
    assert any("LLM 판정 실패" in w for w in r["warnings"])
    # 실패한 결과를 캐시하면 Ollama 가 살아나도 30분 동안 '확인 필요'만 나온다.
    column.run_column_impact("IF_ORDER_TMP", "TOTAL_AMT", "EVAL")
    assert len(fake.prompts) == 4


def test_result_is_cached_for_the_grid(monkeypatch: Any):
    """툴 답 뒤에 [표로 보기] 가 같은 조사를 부른다. LLM 을 다시 돌리지 않는다."""
    fake = FakeLLM()
    _use_llm(monkeypatch, fake)
    column.run_column_impact("if_order_tmp", "total_amt", "", detail="summary")
    n = len(fake.prompts)
    column.column_grid("IF_ORDER_TMP", "TOTAL_AMT", "-")     # '' 와 '-' 는 같은 기본 루트
    assert len(fake.prompts) == n


# --------------------------------------------------------------------------
# 그리드와 MCP
# --------------------------------------------------------------------------


def test_grid_has_header_rows_and_tones(monkeypatch: Any):
    _use_llm(monkeypatch, FakeLLM({"T.TOTAL_AMT": ("column", "IF_ORDER_TMP"),
                                   "long TOTAL_AMT": ("variable", "")}))
    g = column.column_grid("IF_ORDER_TMP", "TOTAL_AMT", "EVAL")
    keys = [c["key"] for c in g["grid"]["columns"]]
    assert keys[:4] == ["verdict", "root", "program", "line"]
    rows = g["grid"]["rows"]
    assert len(rows) == 12 and all(set(keys) <= set(r) for r in rows)
    # 영향 있음 → 확인 필요 → 영향 없음 순서
    order = [r["verdict"] for r in rows]
    assert order == sorted(order, key=["영향 있음", "확인 필요", "영향 없음"].index)
    assert g["grid"]["filter_key"] == "verdict"
    assert g["grid"]["tones"]["영향 있음"] == "bad"


def _run(coro: Any) -> Any:
    return asyncio.run(coro)


async def _with_client(fn: Any) -> Any:
    async with Client(build_server()) as c:
        return await fn(c)


def test_mcp_tool_summary_and_grid_resource():
    async def call(c: Client) -> Any:
        tools = {t.name: t for t in (await c.list_tools()).tools}
        res = await c.call_tool("analyze_column_impact",
                                {"table": "IF_ORDER_TMP", "column": "TOTAL_AMT"})
        grid = await c.read_resource("impact://column/IF_ORDER_TMP/TOTAL_AMT/-")
        return tools, res, grid

    tools, res, grid = _run(_with_client(call))
    meta = tools["analyze_column_impact"].meta
    assert meta["view"] == "grid" and meta["detail_uri"] == "impact://column/{table}/{column}/{root}"
    assert tools["analyze_column_impact"].annotations.read_only_hint is True
    text = res.content[0].text
    assert "IF_ORDER_TMP.TOTAL_AMT 컬럼 영향도" in text and "12줄 찾음" in text
    assert "[표로 보기]" in text
    data = json.loads(grid.contents[0].text)
    assert data["title"] == "IF_ORDER_TMP.TOTAL_AMT 컬럼 영향도"
    assert len(data["grid"]["rows"]) == 12


def test_mcp_tool_unknown_root_says_unknown():
    async def call(c: Client) -> Any:
        return await c.call_tool("analyze_column_impact",
                                 {"table": "IF_ORDER_TMP", "column": "TOTAL_AMT", "root": "NOPE"})

    text = _run(_with_client(call)).content[0].text
    assert "확인 불가" in text and "없다는 뜻이 아니다" in text
