"""core/ 회귀 테스트.

여기 있는 케이스는 전부 '실제로 깨졌던 것'이다. 통과한다고 지우지 말 것.

실행:
    pytest -q
"""

from __future__ import annotations

import sys
import time
from pathlib import Path

import pytest

# 패키지로 설치하지 않으므로 저장소 루트를 경로에 넣는다.
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from core.cache import TTLCache, cached  # noqa: E402
from core.text import (  # noqa: E402
    find_ident,
    ident_pattern,
    read_text,
    read_text_with_encoding,
    strip_comments,
)


# --------------------------------------------------------------------------
# ident_pattern — \b 를 쓰면 깨지는 케이스
# --------------------------------------------------------------------------


def test_ident_pattern_underscore_boundary():
    """언더스코어는 식별자의 일부다. IF_A 안에서 A 를 찾으면 안 된다.

    정규식의 \\b 는 언더스코어를 단어 문자로 보므로 이 케이스를 못 잡는다.
    """
    pat = ident_pattern("A")
    assert not pat.search("IF_A")
    assert not pat.search("A_1")
    assert not pat.search("XA")
    assert pat.search("A")
    assert pat.search("if (A) {")
    assert pat.search("b = A;")


def test_ident_pattern_full_name_matches():
    assert ident_pattern("IF_A").search("IF_A")
    assert ident_pattern("IF_A").search("v = IF_A + 1;")
    assert not ident_pattern("IF_A").search("IF_AB")


def test_ident_pattern_escapes_metachars():
    """이름에 정규식 메타문자가 있어도 리터럴로 취급해야 한다."""
    assert ident_pattern("a.b").search("x = a.b;")
    assert not ident_pattern("a.b").search("x = axb;")


def test_find_ident_returns_evidence():
    """판정 근거로 쓸 실제 줄이 나와야 한다."""
    src = "int A;\nint IF_A;\nA = 1;\n"
    hits = find_ident(src, "A")
    assert [n for n, _ in hits] == [1, 3]
    assert hits[0][1] == "int A;"


# --------------------------------------------------------------------------
# strip_comments — 개수 세기/정규식으로는 못 잡는 케이스
# --------------------------------------------------------------------------


def test_strip_comments_pointer_arith():
    """a*/*c*/b 는 포인터 연산 뒤에 주석이 붙은 것이다.

    '/*' 개수 세기 방식은 여기서 반드시 깨진다.
    """
    out = strip_comments("int r = a*/*c*/b;", "c")
    assert "/*" not in out and "*/" not in out
    assert out.replace(" ", "") == "intr=a*b;"


def test_strip_comments_keeps_string_literal():
    """문자열 안의 주석 토큰은 주석이 아니다."""
    src = 'char *s = "/* x */";'
    assert strip_comments(src, "c") == src


def test_strip_comments_escaped_quote_in_string():
    r"""\" 는 문자열을 닫지 않는다. 여기서 상태가 어긋나면 뒤가 전부 망가진다."""
    src = 'char *s = "a\\"// not comment"; // real'
    out = strip_comments(src, "c")
    assert "not comment" in out
    assert "real" not in out


def test_strip_comments_line_and_block():
    src = "a = 1; // 주석\n/* 여러\n줄 */\nb = 2;\n"
    out = strip_comments(src, "c")
    assert "주석" not in out and "여러" not in out
    assert "a = 1;" in out and "b = 2;" in out
    # 줄 번호가 밀리면 근거의 위치 정보가 쓸모없어진다.
    assert out.count("\n") == src.count("\n")


def test_strip_comments_unterminated_block():
    """닫히지 않은 주석에서 예외로 죽지 않아야 한다."""
    assert "b" not in strip_comments("a = 1; /* 안 닫힘\nb = 2;", "c")


def test_strip_comments_sql_doubled_quote():
    """Oracle 에서 '' 는 따옴표 한 개다. 문자열이 여기서 닫히면 안 된다."""
    src = "s := 'it''s -- not a comment'; -- real"
    out = strip_comments(src, "sql")
    assert "not a comment" in out
    assert "real" not in out


def test_strip_comments_unknown_lang():
    with pytest.raises(ValueError):
        strip_comments("x", "cobol")


# --------------------------------------------------------------------------
# read_text — 사내 파일은 cp949 인 경우가 흔하다
# --------------------------------------------------------------------------


def test_read_text_cp949(tmp_path: Path):
    p = tmp_path / "a.c"
    p.write_bytes("int a; // 한글 주석\n".encode("cp949"))
    assert "한글 주석" in read_text(p)


def test_read_text_utf8_and_bom(tmp_path: Path):
    p = tmp_path / "u.txt"
    p.write_bytes("한글".encode("utf-8"))
    assert read_text(p) == "한글"

    b = tmp_path / "b.txt"
    b.write_bytes("﻿한글".encode("utf-8"))
    # BOM 이 남으면 첫 토큰 매칭이 조용히 실패한다.
    assert read_text(b) == "한글"


def test_read_text_reports_encoding(tmp_path: Path):
    p = tmp_path / "c.txt"
    p.write_bytes("가나다".encode("cp949"))
    text, enc = read_text_with_encoding(p)
    assert text == "가나다"
    assert enc == "cp949"


def test_read_text_never_raises_on_garbage(tmp_path: Path):
    """파일 하나를 못 읽었다고 스캔 전체가 멈추면 안 된다."""
    p = tmp_path / "bad.bin"
    p.write_bytes(b"\xff\xfe\x00\x01\x80\x81")
    assert isinstance(read_text(p), str)


# --------------------------------------------------------------------------
# cache
# --------------------------------------------------------------------------


def test_cache_hit_and_expiry():
    c = TTLCache(ttl=0.05, maxsize=10)
    c.set("k", 1)
    assert c.get("k") == 1
    time.sleep(0.08)
    assert c.get("k") is None


def test_cache_caches_none():
    """None 도 정상 값이다. 미스와 구분되어야 한다."""
    c = TTLCache(ttl=10, maxsize=10)
    c.set("k", None)
    assert c.get("k", "MISS") is None


def test_cache_lru_eviction():
    c = TTLCache(ttl=10, maxsize=2)
    c.set("a", 1)
    c.set("b", 2)
    c.get("a")          # a 를 최근 사용으로 올린다
    c.set("c", 3)       # 가장 오래 안 쓴 b 가 밀려난다
    assert c.get("a") == 1 and c.get("c") == 3 and c.get("b") is None


def test_cache_clear():
    c = TTLCache(ttl=10, maxsize=10)
    c.set("a", 1)
    c.clear()
    assert len(c) == 0 and c.stats()["hits"] == 0


def test_cached_decorator_key_ignores_cheap_arg():
    """비싼 부분과 무관한 인자는 캐시 키에서 빠져야 한다.

    빠지지 않으면 그 인자가 바뀔 때마다 비싼 스캔이 다시 돈다.
    """
    calls: list[tuple[str, str]] = []

    @cached(ttl=10, key=lambda root, name: root)
    def scan(root: str, name: str) -> str:
        calls.append((root, name))
        return root + ":" + name

    assert scan("/r", "A") == "/r:A"
    assert scan("/r", "B") == "/r:A"      # name 이 달라도 캐시 히트
    assert len(calls) == 1
    scan.cache.clear()
    assert scan("/r", "B") == "/r:B"
    assert len(calls) == 2
