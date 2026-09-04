"""소스에서 '완결된 SQL 문장'을 잘라내고 성격을 판정한다.

사용법:
    from core.sqlstmt import statement_at, classify

    stmt = statement_at(source_text, line_no, lang="pro")
    info = classify(stmt.sql, "IF_ORDER_TMP")

왜 필요한가:
    테이블 이름이 나온 줄 하나만 봐서는 그 테이블이 읽히는지 쓰이는지,
    어떤 조건으로 쓰이는지 알 수 없다. INSERT 한 줄, FROM 절 한 줄만
    보고 판단하면 반드시 틀린다. 문장 단위로 잘라야 한다.

경계를 찾는 방법:
    주석과 문자열을 지운 사본에서 세미콜론을 찾는다. 원문에는 주석 안이나
    문자열 안에도 ';' 가 흔해서 그대로 세면 엉뚱한 곳에서 끊긴다.
    strip_comments 가 줄 수를 보존하므로, 지운 사본에서 찾은 줄 번호를
    원문에 그대로 대응시켜 원문을 잘라낼 수 있다.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from core.text import ident_pattern, strip_comments

# 문장 시작으로 볼 키워드. WITH 절로 시작하는 SELECT 도 흔하다.
_STMT_START = re.compile(
    r"(?i)(?<![A-Za-z0-9_])(SELECT|INSERT|UPDATE|DELETE|MERGE|WITH)(?![A-Za-z0-9_])"
)

# SQL 이 소스에 그대로 박히는 언어. 이쪽은 문자열을 지워도 SQL 이 남는다.
# 반대로 java/js 는 SQL 이 문자열 안에 있어 지우면 안 된다.
# xml(UI 쿼리)은 SQL 이 태그 사이 '텍스트'라 작은따옴표 리터럴만 지워진다.
_INLINE_SQL_LANGS = {"sql", "plsql", "pro", "c", "cpp", "xml"}

# XML 에서 쿼리 한 건을 감싸는 태그. MyBatis 계열과 사내 포맷을 함께 본다.
_XML_OPEN = re.compile(
    r"(?i)<\s*(select|insert|update|delete|merge|sql|statement|query|stmt)"
    r"(?![A-Za-z0-9_])[^>]*>"
)
_XML_CLOSE_FMT = r"(?i)</\s*{tag}\s*>"


def masks_strings(lang: str) -> bool:
    """이 언어에서 문자열을 지워도 되는가.

    java/js 는 SQL 이 문자열 안에 있어 지우면 SQL 자체가 사라진다.
    Pro*C/PL-SQL 은 SQL 이 코드에 그대로 박혀 있어 지워도 남는다.
    """
    return lang.lower() in _INLINE_SQL_LANGS


@dataclass(frozen=True)
class Statement:
    """잘라낸 문장 하나."""

    start_line: int      # 1부터
    end_line: int
    sql: str             # 원문 그대로 (주석 포함)
    complete: bool       # 세미콜론까지 확인했는가
    hit_line: int        # 테이블 이름이 나온 줄


def _xml_statement(
    scan_lines: list[str], orig_lines: list[str], i: int, max_lines: int, line_no: int
) -> Statement | None:
    """XML 쿼리는 세미콜론이 아니라 태그로 경계가 정해진다.

    UI 쿼리 XML 의 SQL 에는 끝에 세미콜론이 없는 경우가 대부분이다.
    세미콜론만 찾으면 파일 전체를 한 덩어리로 물고 온다.
    """
    start = None
    tag = ""
    for j in range(i, max(-1, i - max_lines), -1):
        m = _XML_OPEN.search(scan_lines[j])
        if m:
            start, tag = j, m.group(1)
            break
    if start is None:
        return None

    close = re.compile(_XML_CLOSE_FMT.format(tag=re.escape(tag)))
    end, complete = min(len(scan_lines) - 1, start + max_lines - 1), False
    for j in range(i, min(len(scan_lines), start + max_lines)):
        if close.search(scan_lines[j]):
            end, complete = j, True
            break

    return Statement(
        start_line=start + 1,
        end_line=end + 1,
        sql="\n".join(orig_lines[start : end + 1]),
        complete=complete,
        hit_line=line_no,
    )


def statement_at(
    text: str, line_no: int, lang: str = "c", max_lines: int = 120
) -> Statement:
    """line_no 를 포함하는 문장을 앞뒤로 확장해 잘라낸다.

    max_lines: 한 문장으로 볼 최대 길이. 세미콜론이 없는 파일(설정 파일,
    깨진 소스)에서 파일 전체를 한 문장으로 물고 오는 것을 막는다.
    """
    mask = lang.lower() in _INLINE_SQL_LANGS
    try:
        scan = strip_comments(text, lang, mask_strings=mask)
    except ValueError:
        scan = text          # 모르는 언어면 원문으로 (경계가 덜 정확해진다)

    scan_lines = scan.splitlines()
    orig_lines = text.splitlines()
    i = max(0, min(line_no - 1, len(scan_lines) - 1))

    if lang.lower() == "xml":
        found = _xml_statement(scan_lines, orig_lines, i, max_lines, line_no)
        if found is not None:
            return found
        # 태그를 못 찾으면 아래 세미콜론 방식으로 떨어진다.

    # 뒤로: 이전 문장의 끝(세미콜론) 다음 줄부터가 이 문장의 후보 시작이다.
    start = max(0, i - max_lines + 1)
    for j in range(i - 1, start - 1, -1):
        if ";" in scan_lines[j]:
            start = j + 1
            break

    # 그 구간에서 첫 SQL 키워드를 찾아 시작을 당긴다. 앞의 빈 줄이나
    # 일반 코드까지 문장에 넣으면 LLM 에게 보내는 조각이 지저분해진다.
    for j in range(start, i + 1):
        if _STMT_START.search(scan_lines[j]):
            start = j
            break

    # 앞으로: 세미콜론이 나오는 줄까지가 문장이다.
    end = min(len(scan_lines) - 1, i + max_lines - 1)
    complete = False
    for j in range(i, end + 1):
        if ";" in scan_lines[j]:
            end = j
            complete = True
            break

    # max_lines 는 문장 '전체' 길이 상한이다. 앞뒤로 각각 적용하면 두 배가 된다.
    end = min(end, start + max_lines - 1)

    return Statement(
        start_line=start + 1,
        end_line=end + 1,
        sql="\n".join(orig_lines[start : end + 1]),
        complete=complete,
        hit_line=line_no,
    )


@dataclass(frozen=True)
class StmtInfo:
    """문장 판정 결과. 근거를 함께 남긴다."""

    kind: str        # select | insert | update | delete | merge | unknown
    role: str        # read | write | unknown
    rule: str        # 어떤 규칙으로 판정했는가
    evidence: str    # 판단 근거가 된 실제 조각


def classify(sql: str, table: str, lang: str = "sql") -> StmtInfo:
    """문장이 그 테이블을 읽는지 쓰는지 판정한다.

    '어디에 나왔는가'로 정한다. INSERT INTO T / UPDATE T / DELETE FROM T /
    MERGE INTO T 면 쓰기, FROM·JOIN 뒤면 읽기다. 같은 문장에 여러 번
    나오면 쓰기를 우선한다 — 영향도 조사에서 쓰기가 더 중요하다.
    """
    try:
        body = strip_comments(sql, lang, mask_strings=False)
    except ValueError:
        body = sql
    if lang.lower() == "xml":
        # 태그를 지운다. <select id="..."> 의 'select' 를 SQL 키워드로 보면
        # <update> 안의 SELECT 를 update 로 잘못 분류한다.
        # <if test="...">, <isNotEmpty> 같은 동적 태그도 함께 사라진다.
        body = re.sub(r"<[^>]*>", " ", body)
    flat = " ".join(body.split())          # 줄바꿈을 없애야 구절이 이어진다
    t = re.escape(table)

    kind_m = _STMT_START.search(flat)
    kind = kind_m.group(1).lower() if kind_m else "unknown"
    if kind == "with":
        kind = "select"

    # 쓰기 판정을 먼저. 순서를 바꾸면 INSERT INTO A SELECT FROM T 에서
    # T 를 쓰기로 잘못 본다.
    write_patterns = (
        ("insert-into", rf"(?i)INSERT\s+INTO\s+{t}(?![A-Za-z0-9_])"),
        ("update-target", rf"(?i)UPDATE\s+{t}(?![A-Za-z0-9_])"),
        ("delete-from", rf"(?i)DELETE\s+FROM\s+{t}(?![A-Za-z0-9_])"),
        ("merge-into", rf"(?i)MERGE\s+INTO\s+{t}(?![A-Za-z0-9_])"),
    )
    for rule, pat in write_patterns:
        m = re.search(pat, flat)
        if m:
            return StmtInfo(kind=kind, role="write", rule=rule, evidence=m.group(0))

    read_patterns = (
        ("from", rf"(?i)FROM\s+{t}(?![A-Za-z0-9_])"),
        ("join", rf"(?i)JOIN\s+{t}(?![A-Za-z0-9_])"),
        ("using", rf"(?i)USING\s+{t}(?![A-Za-z0-9_])"),
    )
    for rule, pat in read_patterns:
        m = re.search(pat, flat)
        if m:
            return StmtInfo(kind=kind, role="read", rule=rule, evidence=m.group(0))

    # 이름은 있는데 역할을 못 정했다. '아니다'가 아니라 '모른다'로 남긴다.
    # 위의 read/write 패턴이 (?i) 인 것과 맞춘다 — 여기만 대소문자를
    # 가리면 같은 문장이 규칙에 따라 다르게 판정된다.
    hit = ident_pattern(table, re.IGNORECASE).search(flat)
    return StmtInfo(
        kind=kind,
        role="unknown",
        rule="name-only" if hit else "no-hit",
        evidence=(flat[max(0, hit.start() - 30) : hit.end() + 30] if hit else ""),
    )
