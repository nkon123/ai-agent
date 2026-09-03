"""텍스트 유틸 — 인코딩 감지, 주석 제거, 식별자 정규식.

사용법:
    from core.text import read_text, strip_comments, ident_pattern

    src = read_text(path)
    body = strip_comments(src, "c")
    if ident_pattern("MY_VAR").search(body): ...

여기 있는 함수들은 전부 '한 번 데인 자리'다. 각 함정의 이유를
주석으로 남겨 두었으니 고치기 전에 먼저 읽을 것.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

# 사내 파일은 cp949 인 경우가 흔하다. utf-8 로만 열면 UnicodeDecodeError 로
# 파일 하나 때문에 스캔 전체가 멈춘다. 아래 순서로 시도한다.
ENCODINGS: tuple[str, ...] = ("utf-8", "utf-8-sig", "cp949")


def read_text(path: str | Path, encodings: tuple[str, ...] = ENCODINGS) -> str:
    """인코딩을 순서대로 시도해 텍스트를 읽는다.

    전부 실패하면 마지막 인코딩으로 errors='replace' 하여 읽는다.
    파일 하나를 못 읽었다고 스캔 전체를 중단시키지 않기 위해서다
    (누락은 오탐보다 나쁘다). 대신 깨진 문자는 U+FFFD 로 남아 눈에 띈다.
    """
    p = Path(path)
    raw = p.read_bytes()
    for enc in encodings:
        try:
            text = raw.decode(enc)
        except (UnicodeDecodeError, LookupError):
            continue
        # utf-8 은 BOM 이 붙은 파일도 성공시키면서 맨 앞에 U+FEFF 를 남긴다.
        # 이게 남으면 첫 토큰 매칭이 조용히 실패하므로 여기서 제거한다.
        return text.lstrip("﻿")
    return raw.decode(encodings[-1], errors="replace").lstrip("﻿")


def read_text_with_encoding(
    path: str | Path, encodings: tuple[str, ...] = ENCODINGS
) -> tuple[str, str]:
    """read_text 와 같되 성공한 인코딩 이름도 함께 돌려준다.

    판정 근거(evidence)에 '어떤 인코딩으로 읽었는지'를 남겨야
    깨진 결과의 원인을 추적할 수 있다.
    """
    p = Path(path)
    raw = p.read_bytes()
    for enc in encodings:
        try:
            return raw.decode(enc).lstrip("﻿"), enc
        except (UnicodeDecodeError, LookupError):
            continue
    return raw.decode(encodings[-1], errors="replace").lstrip("﻿"), (
        encodings[-1] + "+replace"
    )


# --------------------------------------------------------------------------
# 주석 제거
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class _Syntax:
    """언어별 주석/문자열 문법."""

    line: tuple[str, ...] = ()          # 줄 주석 시작 토큰
    block: tuple[tuple[str, str], ...] = ()   # (시작, 끝) 블록 주석
    quotes: tuple[str, ...] = ()        # 문자열 구분자 (긴 것부터)
    backslash_escape: bool = True       # \" 로 이스케이프하는가
    doubled_escape: bool = False        # '' 로 이스케이프하는가 (SQL)


_C_LIKE = _Syntax(
    line=("//",),
    block=(("/*", "*/"),),
    quotes=('"', "'"),
    backslash_escape=True,
)

SYNTAX: dict[str, _Syntax] = {
    "c": _C_LIKE,
    "cpp": _C_LIKE,
    "java": _C_LIKE,
    "cs": _C_LIKE,
    "js": _C_LIKE,
    "ts": _C_LIKE,
    "pro": _C_LIKE,   # Pro*C
    "sql": _Syntax(
        line=("--",),
        block=(("/*", "*/"),),
        quotes=("'",),
        backslash_escape=False,   # Oracle 은 백슬래시가 이스케이프가 아니다
        doubled_escape=True,      # '' 가 따옴표 한 개
    ),
    "plsql": _Syntax(
        line=("--",),
        block=(("/*", "*/"),),
        quotes=("'",),
        backslash_escape=False,
        doubled_escape=True,
    ),
    "python": _Syntax(
        line=("#",),
        block=(),
        quotes=('"""', "'''", '"', "'"),  # 긴 것 먼저 봐야 한다
        backslash_escape=True,
    ),
    "shell": _Syntax(line=("#",), quotes=('"', "'"), backslash_escape=True),
}


def strip_comments(text: str, lang: str = "c", mask_strings: bool = False) -> str:
    """주석을 제거한다. 문자열 리터럴 안의 내용은 기본적으로 건드리지 않는다.

    mask_strings=True 면 문자열 '내용'까지 공백으로 지운다(따옴표는 남긴다).
    코드에서 식별자 사용처를 찾을 때 필요하다 — 로그 문구나 SQL 문자열에
    적힌 이름은 사용처가 아닌데, 그냥 두면 오탐으로 잡힌다.
    주석 제거와 같은 한 번의 스캔에서 처리한다(두 번 훑을 이유가 없다).

    왜 상태 머신인가:
      '/*' 와 '*/' 의 개수를 세거나 정규식으로 지우는 방식은
        int r = a*/*c*/b;      → 포인터 연산과 주석 시작이 붙어 있음
        char *s = "/* x */";   → 문자열 안의 주석 토큰
      두 경우에서 반드시 깨진다. 실제로 겪는다. 왼쪽에서 오른쪽으로
      한 글자씩 훑으며 '지금 문자열 안인가'를 추적하는 방법만이 안전하다.

    블록 주석은 빈 문자열이 아니라 공백 한 칸으로 치환한다.
    int/*x*/y 가 inty 로 붙어 새 식별자가 생기는 것을 막기 위해서다.
    줄 주석은 개행을 남겨 줄 번호가 어긋나지 않게 한다.
    """
    syn = SYNTAX.get(lang.lower())
    if syn is None:
        raise ValueError(f"지원하지 않는 언어: {lang} (지원: {', '.join(SYNTAX)})")

    out: list[str] = []
    i, n = 0, len(text)

    while i < n:
        ch = text[i]

        # 1) 문자열 시작 — 문자열은 통째로 그대로 복사한다.
        quote = next((q for q in syn.quotes if text.startswith(q, i)), None)
        if quote:
            j = i + len(quote)
            while j < n:
                if syn.backslash_escape and text[j] == "\\":
                    j += 2          # 이스케이프된 문자는 종료 판정에서 제외
                    continue
                if text.startswith(quote, j):
                    if syn.doubled_escape and text.startswith(quote * 2, j):
                        j += 2 * len(quote)   # '' 는 닫는 따옴표가 아니다
                        continue
                    j += len(quote)
                    break
                j += 1
            else:
                j = n               # 닫히지 않은 문자열 — 끝까지가 문자열
            chunk = text[i:j]
            if mask_strings:
                # 따옴표와 줄 수는 남긴다. 줄 번호가 밀리면 근거(evidence)의
                # 위치 정보가 쓸모없어지고, 따옴표까지 지우면 뒤따르는
                # 코드가 문법적으로 이상해진다.
                inner = chunk[len(quote) : max(len(quote), len(chunk) - len(quote))]
                chunk = (
                    quote
                    + "".join("\n" if ch == "\n" else " " for ch in inner)
                    + (quote if len(chunk) > len(quote) else "")
                )
            out.append(chunk)
            i = j
            continue

        # 2) 줄 주석
        tok = next((t for t in syn.line if text.startswith(t, i)), None)
        if tok:
            j = text.find("\n", i)
            if j == -1:
                break               # 파일 끝까지 주석
            i = j                   # 개행 자체는 다음 루프에서 그대로 복사
            continue

        # 3) 블록 주석
        pair = next((p for p in syn.block if text.startswith(p[0], i)), None)
        if pair:
            start, end = pair
            j = text.find(end, i + len(start))
            chunk = text[i:] if j == -1 else text[i : j + len(end)]
            # 주석 안의 개행은 보존한다. 줄 번호가 밀리면 근거(evidence)의
            # 위치 정보가 쓸모없어진다.
            out.append(" " + "\n" * chunk.count("\n"))
            i = n if j == -1 else j + len(end)
            continue

        out.append(ch)
        i += 1

    return "".join(out)


# --------------------------------------------------------------------------
# 식별자 정규식
# --------------------------------------------------------------------------


def ident_pattern(name: str, flags: int = 0) -> re.Pattern[str]:
    r"""식별자 경계를 지키는 정규식을 만든다.

    \b 를 쓰지 말 것:
      정규식은 언더스코어를 '단어 문자'로 취급한다. 그래서 r"\bA\b" 는
      IF_A 안의 A 를 매치하지 않는다(경계가 아니므로). 반대로 A_1 에서
      A 를 찾고 싶지 않은데 찾아 버리는 경우도 생긴다.
      코드에서 식별자를 찾을 때는 언더스코어를 '단어의 일부'로 봐야
      하므로 경계를 직접 정의한다.
    """
    return re.compile(
        r"(?<![A-Za-z0-9_])" + re.escape(name) + r"(?![A-Za-z0-9_])", flags
    )


def find_ident(text: str, name: str) -> list[tuple[int, str]]:
    """식별자가 나오는 (줄번호, 줄내용) 목록. 근거(evidence) 수집용.

    판정 결과만 남기면 오판 원인을 못 찾는다(규칙 4-6).
    실제로 매치된 줄을 그대로 남긴다.
    """
    pat = ident_pattern(name)
    hits: list[tuple[int, str]] = []
    for no, line in enumerate(text.splitlines(), start=1):
        if pat.search(line):
            hits.append((no, line.strip()))
    return hits
