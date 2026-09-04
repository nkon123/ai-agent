"""impact 에이전트와 SQL 문장 추출 회귀 테스트.

여기 케이스는 '테이블 이름이 나온 줄 하나만 봐서는 틀린다'는 것을 보여준다.

실행:
    pytest -q
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from agents.impact import run_impact  # noqa: E402
from agents.impact import agent as impact  # noqa: E402
from core.sqlstmt import classify, masks_strings, statement_at  # noqa: E402

PROC = """void proc(void)
{
    /* IF_ORDER_TMP 에서 읽는다; 주석 안 세미콜론 */
    EXEC SQL
        SELECT ORD_NO, QTY
          INTO :ord_no, :qty
          FROM IF_ORDER_TMP
         WHERE STATUS = 'N';

    EXEC SQL
        INSERT INTO ORD_HDR (ORD_NO, QTY)
        VALUES (:ord_no, :qty);
}
"""


# --------------------------------------------------------------------------
# 문장 잘라내기
# --------------------------------------------------------------------------


def test_statement_expands_to_whole_sql():
    """테이블이 나온 줄 하나가 아니라 문장 전체가 나와야 한다."""
    st = statement_at(PROC, 7, "pro")
    assert st.start_line == 5 and st.end_line == 8 and st.complete
    assert "SELECT ORD_NO" in st.sql and "WHERE STATUS" in st.sql
    # 다음 문장까지 삼키면 안 된다.
    assert "INSERT" not in st.sql


def test_statement_stops_at_previous_semicolon():
    """앞 문장이 이 문장에 딸려 오면 안 된다."""
    st = statement_at(PROC, 12, "pro")
    assert "INSERT INTO ORD_HDR" in st.sql
    assert "SELECT" not in st.sql


def test_semicolon_in_comment_does_not_break_boundary():
    """주석 안 세미콜론에서 끊기면 문장이 조각난다."""
    st = statement_at(PROC, 7, "pro")
    assert st.start_line == 5      # 주석 줄(3)에서 끊기지 않았다


def test_incomplete_statement_is_flagged():
    """세미콜론을 못 찾으면 그 사실을 남긴다. 잘린 조각으로 판단하면 틀린다."""
    st = statement_at("SELECT * FROM T\n  WHERE A = 1\n", 1, "sql")
    assert st.complete is False


def test_statement_has_a_length_limit():
    """세미콜론이 없는 파일에서 파일 전체를 한 문장으로 물고 오면 안 된다."""
    text = "\n".join(f"line {i}" for i in range(500))
    st = statement_at(text, 250, "sql", max_lines=20)
    assert st.end_line - st.start_line < 20


def test_masks_strings_only_for_inline_sql_langs():
    """java 는 SQL 이 문자열 안에 있어 지우면 SQL 이 사라진다."""
    assert masks_strings("pro") and masks_strings("sql")
    assert not masks_strings("java") and not masks_strings("js")


# --------------------------------------------------------------------------
# 읽기/쓰기 판정
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "sql,role,rule",
    [
        ("SELECT A FROM IF_T WHERE X=1;", "read", "from"),
        ("SELECT A FROM B JOIN IF_T ON B.K=IF_T.K;", "read", "join"),
        ("INSERT INTO IF_T (A) VALUES (1);", "write", "insert-into"),
        ("UPDATE IF_T SET A=1;", "write", "update-target"),
        ("DELETE FROM IF_T WHERE A=1;", "write", "delete-from"),
        ("MERGE INTO IF_T USING S ON (1=1);", "write", "merge-into"),
    ],
)
def test_classify_roles(sql: str, role: str, rule: str):
    info = classify(sql, "IF_T")
    assert info.role == role and info.rule == rule
    assert info.evidence


def test_insert_select_counts_source_as_read():
    """INSERT INTO A SELECT FROM T 에서 T 는 읽기다.

    문장 종류(INSERT)만 보고 쓰기로 판정하면 틀린다.
    """
    info = classify("INSERT INTO ORD_HDR SELECT * FROM IF_T;", "IF_T")
    assert info.role == "read"
    assert classify("INSERT INTO ORD_HDR SELECT * FROM IF_T;", "ORD_HDR").role == "write"


def test_name_only_is_unknown_not_no():
    """이름은 있는데 역할을 못 정하면 '아니다'가 아니라 '모른다'다."""
    info = classify("EXEC SOME_PROC(IF_T_ID);", "IF_T_ID")
    assert info.role == "unknown" and info.rule == "name-only"


def test_similar_table_name_is_not_a_hit():
    """IF_T 를 찾을 때 IF_T_BAK 이 걸리면 안 된다."""
    assert classify("SELECT * FROM IF_T_BAK;", "IF_T").rule == "no-hit"


# --------------------------------------------------------------------------
# 에이전트
# --------------------------------------------------------------------------


def test_impact_finds_reads_and_writes():
    r = run_impact("IF_ORDER_TMP", root="SAMPLE")
    roles = {(Path(s["file"]).name, s["start_line"]): s["role"] for s in r["statements"]}
    assert roles[("order_load.pc", 8)] == "read"     # SELECT ... FROM
    assert roles[("order_load.pc", 18)] == "write"   # DELETE FROM
    assert r["read_count"] >= 1 and r["write_count"] >= 1


def test_impact_reads_sql_inside_java_strings():
    """Java 는 SQL 이 문자열 안에 있다. 문자열을 지우면 통째로 놓친다."""
    r = run_impact("IF_ORDER_TMP", root="SAMPLE")
    java = [s for s in r["statements"] if s["file"].endswith(".java")]
    assert {s["role"] for s in java} == {"read", "write"}


def test_impact_skips_comment_mentions():
    """주석 속 테이블 언급은 사용처가 아니다(order_load.pc 6행)."""
    r = run_impact("IF_ORDER_TMP", root="SAMPLE")
    pc = [s for s in r["statements"] if s["file"].endswith("order_load.pc")]
    assert all(s["start_line"] != 6 for s in pc)
    assert len(pc) == 2      # SELECT 와 DELETE 뿐이다


def test_impact_unknown_table_is_not_an_error():
    r = run_impact("NO_SUCH_TABLE", root="SAMPLE")
    assert r["rule"] == "no-hit"
    assert r["write_count"] == r["read_count"] == 0


def test_impact_unknown_root_warns():
    r = run_impact("IF_ORDER_TMP", root="NOPE")
    assert r["rule"] == "unknown-root"
    assert any("확인 필요" in w for w in r["warnings"])


def test_impact_summary_omits_sql():
    """SQL 전문은 LLM 컨텍스트에 넣지 않는다."""
    s = run_impact("IF_ORDER_TMP", root="SAMPLE", detail="summary")
    assert "statements" not in s and s["places"]
    assert all("sql" not in p for p in s["places"])


def test_llm_conflict_is_reported(monkeypatch: Any):
    """규칙과 LLM 이 어긋나면 조용히 한쪽을 고르지 않는다."""

    class FakeVerdict:
        uses_table = False
        role = "unknown"
        note = "주석으로 보인다"

    monkeypatch.setattr(impact, "USE_LLM", True)
    # invoke_structured 는 (결과, 경고문) 을 돌려준다.
    monkeypatch.setitem(sys.modules, "core.llm", type(sys)("core.llm"))
    sys.modules["core.llm"].invoke_structured = (
        lambda schema, prompt, model=None: (FakeVerdict(), "")
    )

    state = {
        "table": "IF_ORDER_TMP",
        "statements": [
            {"file": "a.c", "start_line": 1, "role": "read", "sql": "SELECT 1",
             "decided_by": "rule"}
        ],
        "warnings": [],
    }
    out = impact.verify(state)
    assert out["statements"][0]["conflict"] is True
    assert any("확인 필요" in w for w in out["warnings"])


# --------------------------------------------------------------------------
# 실제 소스 유형 — Pro*C(.pc) / 프로시저(.prc) / UI 쿼리(.xml)
# --------------------------------------------------------------------------

XML_QUERY = """<mapper namespace="order">
  <!-- IF_T 를 읽는다 (주석) -->
  <select id="selectList" resultType="map">
    SELECT A, B
      FROM IF_T
     WHERE STATUS = #{status}
  </select>

  <update id="updateStatus">
    UPDATE IF_T SET STATUS = #{status}
  </update>
</mapper>
"""


def test_xml_statement_uses_tag_boundaries():
    """UI 쿼리 XML 의 SQL 에는 세미콜론이 없다.

    세미콜론만 찾으면 파일 전체를 한 덩어리로 물고 온다.
    """
    st = statement_at(XML_QUERY, 5, "xml")
    assert st.complete
    assert st.start_line == 3 and st.end_line == 7      # <select> ~ </select>
    assert "SELECT A, B" in st.sql
    assert "UPDATE" not in st.sql                        # 다음 쿼리를 삼키지 않는다


def test_xml_tag_name_does_not_decide_kind():
    """<update> 안의 SELECT 를 update 로 분류하면 안 된다.

    태그 이름 'select'/'update' 가 SQL 키워드처럼 보이므로 태그를 지운 뒤
    판정한다.
    """
    st = statement_at(XML_QUERY, 10, "xml")
    info = classify(st.sql, "IF_T", "xml")
    assert info.kind == "update" and info.role == "write"

    sel = classify(statement_at(XML_QUERY, 5, "xml").sql, "IF_T", "xml")
    assert sel.kind == "select" and sel.role == "read"


def test_xml_comment_mention_is_not_a_usage():
    r = run_impact("IF_ORDER_TMP", root="SAMPLE")
    xml = [s for s in r["statements"] if s["file"].endswith(".xml")]
    assert len(xml) == 2 and all(s["start_line"] != 4 for s in xml)


def test_procedure_insert_select_is_read_for_source_table():
    """INSERT INTO A SELECT FROM T — T 는 읽기, A 는 쓰기다.

    실제 적재 프로시저에서 가장 흔한 형태다. 문장 종류만 보면 틀린다.
    """
    r = run_impact("IF_ORDER_TMP", root="SAMPLE")
    prc = {s["start_line"]: s for s in r["statements"] if s["file"].endswith(".prc")}
    assert prc[4]["kind"] == "insert" and prc[4]["role"] == "read"
    assert prc[9]["role"] == "write"

    target = run_impact("ORD_HDR", root="SAMPLE")
    prc_t = {s["start_line"]: s for s in target["statements"]
             if s["file"].endswith(".prc")}
    assert prc_t[4]["role"] == "write"


def test_all_three_source_types_are_scanned():
    """.pc / .prc / .xml 이 모두 스캔 대상이어야 한다."""
    r = run_impact("IF_ORDER_TMP", root="SAMPLE")
    suffixes = {Path(s["file"]).suffix for s in r["statements"]}
    assert {".pc", ".prc", ".xml"} <= suffixes


# --------------------------------------------------------------------------
# 대소문자 — SQL 식별자는 대소문자를 가리지 않는다
# --------------------------------------------------------------------------

MIXED_CASE = """void a(void) {
    EXEC SQL SELECT ord_no INTO :n FROM if_order_tmp WHERE status = 'N';
    EXEC SQL delete from If_Order_Tmp where ord_no = :n;
}
"""


@pytest.fixture
def mixed_case_root(tmp_path: Path, monkeypatch: Any) -> str:
    """대소문자가 뒤섞인 소스 하나를 가진 루트."""
    (tmp_path / "mixed.pc").write_text(MIXED_CASE, encoding="utf-8")
    monkeypatch.setattr(impact, "SOURCE_ROOTS", {"CASE": str(tmp_path)})
    from agents.usage.agent import scan_files

    scan_files.cache.clear()
    return "CASE"


@pytest.mark.parametrize("written", ["IF_ORDER_TMP", "if_order_tmp", "If_Order_Tmp"])
def test_table_lookup_ignores_case(written: str, mixed_case_root: str):
    """FROM if_order_tmp 와 FROM IF_ORDER_TMP 는 같은 테이블이다.

    대소문자를 따지면 소스마다 표기가 달라 통째로 놓친다.
    """
    r = run_impact(written, root=mixed_case_root)
    assert r["read_count"] == 1 and r["write_count"] == 1


def test_classify_ignores_case():
    """읽기/쓰기 판정도 대소문자를 가리지 않는다.

    여기만 가리면 같은 문장이 규칙에 따라 다르게 판정된다.
    """
    assert classify("delete from If_Order_Tmp where a=1;", "IF_ORDER_TMP").role == "write"
    assert classify("select * from IF_ORDER_TMP;", "if_order_tmp").role == "read"
    assert classify("exec p(if_order_tmp_id);", "IF_ORDER_TMP_ID").rule == "name-only"


def test_structured_retry_note_is_kept(monkeypatch: Any):
    """사고를 끄고 다시 받았으면 그 사실이 결과에 남아야 한다.

    두 번째 시도의 답은 첫 시도와 품질이 다를 수 있다.
    """

    class FakeVerdict:
        uses_table = True
        role = "read"
        note = "읽는다"

    monkeypatch.setattr(impact, "USE_LLM", True)
    monkeypatch.setitem(sys.modules, "core.llm", type(sys)("core.llm"))
    sys.modules["core.llm"].invoke_structured = (
        lambda schema, prompt, model=None: (FakeVerdict(), "사고 과정을 껐다")
    )

    out = impact.verify({
        "table": "T",
        "statements": [{"file": "a.c", "start_line": 1, "role": "read",
                        "sql": "SELECT 1", "decided_by": "rule"}],
        "warnings": [],
    })
    assert any("사고 과정을 껐다" in w for w in out["warnings"])
