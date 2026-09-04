"""usage 에이전트 회귀 테스트.

여기 케이스는 전부 samples/src/ 에 심어 둔 함정이다. 실제로 오탐이
났던 형태들이므로 통과한다고 지우지 말 것.

실행:
    pytest -q
"""

from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import pytest  # noqa: E402
from mcp.client.client import Client  # noqa: E402

from agents.usage.agent import run_usage, scan_files  # noqa: E402
from mcp_server.tools import build_server  # noqa: E402

ROOT = "SAMPLE"


def _names(result: dict[str, Any]) -> set[str]:
    return {Path(h["file"]).name for h in result["hits"]}


def _lines(result: dict[str, Any], filename: str) -> set[int]:
    return {h["line"] for h in result["hits"] if Path(h["file"]).name == filename}


# --------------------------------------------------------------------------
# 무엇을 사용처로 세지 않는가
# --------------------------------------------------------------------------


def test_comment_mention_is_not_a_usage():
    """주석에 이름이 적혀 있다고 사용처는 아니다.

    erp_calc.c 1행과 order_pkg.sql 1행이 그 형태다.
    """
    r = run_usage("TOTAL_AMT", root=ROOT)
    assert 1 not in _lines(r, "erp_calc.c")
    assert 1 not in _lines(r, "order_pkg.sql")


def test_string_literal_mention_is_not_a_usage():
    """문자열 안의 이름도 사용처가 아니다.

    C 의 "/* TOTAL_AMT */" 와 SQL 의 'TOTAL_AMT is not a hit' 두 형태.
    strip_comments 는 문자열을 보존하므로 mask_strings=True 가 필요하다.
    """
    r = run_usage("TOTAL_AMT", root=ROOT)
    assert 16 not in _lines(r, "erp_calc.c")
    assert 3 not in _lines(r, "order_pkg.sql")


def test_real_usages_are_found():
    """선언·대입·반환·UPDATE 는 사용처다."""
    r = run_usage("TOTAL_AMT", root=ROOT)
    assert _lines(r, "erp_calc.c") == {5, 10, 11}
    assert _lines(r, "order_pkg.sql") == {5}
    assert r["used"] == "yes" and r["rule"] == "ident-found"


def test_cp949_file_is_scanned():
    """사내 파일은 cp949 인 경우가 흔하다. 못 읽으면 통째로 누락된다."""
    assert "legacy_cp949.c" in _names(run_usage("TOTAL_AMT", root=ROOT))


def test_identifier_boundary_is_respected():
    """IF_A 는 A 의 사용처가 아니다(그 반대도 아니다)."""
    assert run_usage("A", root=ROOT)["used"] == "no"
    assert _lines(run_usage("IF_A", root=ROOT), "erp_calc.c") == {4}


def test_evidence_is_the_original_line():
    """근거는 실제 매칭된 줄이어야 한다. 규칙명만으로는 오판을 못 잡는다.

    매칭은 주석을 지운 본문에서 하지만(줄 번호가 보존된다), 보여 주는
    것은 원문 줄이다.
    """
    r = run_usage("TOTAL_AMT", root=ROOT)
    assert r["evidence"].startswith("erp_calc.c:5")
    assert "long TOTAL_AMT;" in r["evidence"]


# --------------------------------------------------------------------------
# '없다'와 '모른다'는 다른 값이다
# --------------------------------------------------------------------------


def test_no_hit_is_not_unknown():
    r = run_usage("NO_SUCH_IDENT", root=ROOT)
    assert r["used"] == "no" and r["rule"] == "no-hit"
    assert not r["warnings"]


def test_unknown_root_is_flagged_for_review():
    """설정에 없는 루트는 '사용처 없음'이 아니라 '확인 필요'다."""
    r = run_usage("TOTAL_AMT", root="NOPE")
    assert r["used"] == "unknown" and r["rule"] == "unknown-root"
    assert any("확인 필요" in w for w in r["warnings"])


def test_empty_name_is_unknown():
    r = run_usage("", root=ROOT)
    assert r["used"] == "unknown" and r["rule"] == "empty-name"


# --------------------------------------------------------------------------
# detail — 로컬 LLM 은 컨텍스트가 비싸다
# --------------------------------------------------------------------------


def test_summary_omits_the_heavy_parts():
    """summary 는 LLM 컨텍스트에 들어간다. 매칭 줄이 들어가면 안 된다."""
    s = run_usage("TOTAL_AMT", root=ROOT, detail="summary")
    assert "hits" not in s and "root_path" not in s
    # 건수를 박아 두면 샘플을 추가할 때마다 깨진다. 성질만 확인한다.
    assert s["hit_count"] >= 5
    assert len(s["files"]) <= 5

    m = run_usage("TOTAL_AMT", root=ROOT, detail="minimal")
    assert set(m) == {"name", "used", "decided_by", "rule"}


# --------------------------------------------------------------------------
# 캐시 — 무엇이 비싼지에 따라 키를 정한다
# --------------------------------------------------------------------------


def test_scan_cache_key_ignores_identifier():
    """파일 스캔은 비싸고 '찾는 이름'과 무관하다.

    이름을 키에 넣으면 이름을 바꿀 때마다 트리를 다시 훑는다.
    """
    scan_files.cache.clear()
    run_usage("TOTAL_AMT", root=ROOT)
    after_first = scan_files.cache.stats()["misses"]

    run_usage("IF_A", root=ROOT)          # 이름만 다르다
    stats = scan_files.cache.stats()
    assert stats["misses"] == after_first  # 재스캔하지 않았다
    assert stats["hits"] >= 1


# --------------------------------------------------------------------------
# MCP 노출
# --------------------------------------------------------------------------


def _run(coro: Any) -> Any:
    return asyncio.run(coro)


async def _with_client(fn: Any) -> Any:
    async with Client(build_server()) as c:
        return await fn(c)


def test_mcp_tool_returns_summary_and_resource_returns_full():
    """툴은 요약, 리소스는 전체. 컨텍스트 절약 구조가 MCP 위에서도 지켜져야 한다."""

    async def call(c: Client) -> Any:
        return await c.call_tool("find_usage", {"name": "TOTAL_AMT", "root": ROOT})

    text = _run(_with_client(call)).content[0].text
    assert "곳에서 사용" in text
    assert "erp_calc.c" in text
    # 매칭 줄 원문은 컨텍스트에 넣지 않는다.
    assert "long TOTAL_AMT;" not in text

    async def read(c: Client) -> Any:
        return await c.read_resource(f"usage://detail/{ROOT}/TOTAL_AMT")

    data = json.loads(_run(_with_client(read)).contents[0].text)
    assert data["hit_count"] >= 5
    assert data["hits"][0]["text"] == "long TOTAL_AMT;"


def test_mcp_tool_says_unknown_not_none():
    """'확인 불가'가 '사용처 없음'으로 읽히면 안 된다."""

    async def call(c: Client) -> Any:
        return await c.call_tool("find_usage", {"name": "X", "root": "NOPE"})

    text = _run(_with_client(call)).content[0].text
    assert "확인 불가" in text and "확인 필요" in text


# --------------------------------------------------------------------------
# 대소문자 — 코드 식별자는 가리고, 다른 표기가 있으면 알려 준다
# --------------------------------------------------------------------------


@pytest.fixture
def mixed_root(tmp_path: Path, monkeypatch: Any) -> str:
    (tmp_path / "mixed.pc").write_text(
        "EXEC SQL SELECT a FROM if_order_tmp;\n", encoding="utf-8"
    )
    import agents.usage.agent as ua

    monkeypatch.setattr(ua, "SOURCE_ROOTS", {"CASE": str(tmp_path)})
    scan_files.cache.clear()
    return "CASE"


def test_identifier_search_is_case_sensitive_by_default(mixed_root: str):
    """코드의 식별자는 TOTAL_AMT 와 total_amt 가 서로 다른 것이다."""
    assert run_usage("IF_ORDER_TMP", root=mixed_root)["hit_count"] == 0


def test_case_only_difference_is_not_reported_as_absent(mixed_root: str):
    """대소문자만 다른 것이 있는데 '없다'고 하면 안 된다.

    '없다'와 '확인 필요'는 다른 값이다.
    """
    r = run_usage("IF_ORDER_TMP", root=mixed_root)
    assert r["used"] == "unknown" and r["rule"] == "case-mismatch"
    assert any("대소문자가 다른" in w for w in r["warnings"])
    assert "if_order_tmp" in r["evidence"]


def test_ignore_case_finds_it(mixed_root: str):
    r = run_usage("IF_ORDER_TMP", root=mixed_root, ignore_case=True)
    assert r["used"] == "yes" and r["hit_count"] == 1


def test_truly_absent_is_still_no(mixed_root: str):
    """진짜 없는 것은 '없다'가 맞다."""
    r = run_usage("NO_SUCH_NAME", root=mixed_root)
    assert r["used"] == "no" and r["rule"] == "no-hit"
