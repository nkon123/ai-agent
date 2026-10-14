"""컬럼 영향도 — 테이블의 컬럼 하나가 소스 어디에서 실제로 쓰이는가.

테이블 영향도(agent.py)가 '어느 프로그램이 이 테이블을 쓰나'라면, 이쪽은 한
단계 더 들어가 '그 프로그램에서 이 컬럼을 쓰나'다. 사람이 하던 순서 그대로다.

    테이블이 나오는 파일 → 그 파일에서 컬럼이 나온 줄 전부
    → 규칙으로 1차 분류(주석·문자열·같은 문장에 테이블·다른 코드)
    → 문장 단위로 LLM 이 '이 테이블의 컬럼으로 쓰였는가'를 판정 → 그리드

찾은 줄은 판정과 상관없이 전부 남긴다. 어느 프로그램에서 이름이 나왔는지는
사람이 다 봐야 하고, 실제 영향 여부만 판정으로 가른다.

테이블을 쓰는 SQL 문장 중 컬럼 이름이 없는 문장도 '테이블만 씀' 행으로 남긴다.
테이블 영향도에는 나오는데 컬럼 영향도에서 사라지면, 안 본 건지 안 쓰는 건지
구분이 안 된다. 컬럼 이름 없이 컬럼을 건드리는 문장은 규칙으로 가른다:
    INSERT INTO T VALUES/SELECT (컬럼 목록 없음)  → 영향 있음 (모든 컬럼에 넣는다)
    SELECT * / T.*                              → 확인 필요 (읽은 값을 쓰는지 모른다)
    그 밖                                        → 영향 없음 (이 문장은 컬럼을 안 쓴다)

판정(verdict)은 세 값이다 — '아니다'와 '모른다'를 섞지 않는다.
    yes      영향 있음   그 테이블의 컬럼으로 쓰였다
    no       영향 없음   주석이거나, 다른 테이블의 같은 이름 컬럼·변수다
    unknown  확인 필요   LLM 이 판단 못 함 / LLM 미확인 / LLM 실패

단독 실행:
    python agents/impact/column.py IF_ORDER_TMP TOTAL_AMT --root SAMPLE
    USE_LLM=false python agents/impact/column.py IF_ORDER_TMP TOTAL_AMT
"""

from __future__ import annotations

import re
import sys
from pathlib import Path
from typing import Any, Literal

_ROOT = Path(__file__).resolve().parents[2]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from agents.usage.agent import (  # noqa: E402
    LANG_BY_SUFFIX,
    resolve_roots,
    roots_label,
    scan_roots,
)
from config import (  # noqa: E402
    COLUMN_IMPACT_MAX_HITS,
    COLUMN_IMPACT_MAX_LLM,
    SOURCE_ROOTS,
    USE_LLM,
)
from core.cache import TTLCache  # noqa: E402
from core.progress import notify  # noqa: E402
from core.sqlstmt import (  # noqa: E402
    bare_table_name,
    clause_at,
    classify,
    insert_target,
    masks_strings,
    statement_at,
    table_refs,
)
from core.text import ident_pattern, read_text_with_encoding, strip_comments  # noqa: E402

Detail = Literal["full", "summary"]

VERDICT_LABEL = {"yes": "영향 있음", "no": "영향 없음", "unknown": "확인 필요"}
USAGE_LABEL = {
    "select": "조회", "where": "조건", "insert": "입력", "update": "수정",
    "join": "조인", "order_group": "정렬·그룹", "other": "기타", "none": "-",
}
# 규칙 1차 분류. 그리드의 '위치' 칸에 그대로 나간다.
PLACE_LABEL = {
    "comment": "주석", "string": "문자열", "same-stmt": "테이블과 같은 문장",
    "other-code": "다른 코드", "host-var": "호스트 변수",
    "table-only": "테이블만 씀(컬럼 없음)",
}
_XML_TAG = re.compile(r"<[^>]*>")


def split_names(table: str, column: str) -> tuple[str, str, str]:
    """(테이블, 컬럼, 떼어 낸 앞부분). 'IF_ORDER_TMP.TOTAL_AMT' 한 덩어리도 받는다.

    모델이 컬럼 자리에 '테이블.컬럼' 을 통째로 넣는 일이 흔하다. 컬럼은 마지막
    조각, 그 앞은 테이블이 비었을 때만 테이블로 쓴다. 테이블의 스키마·시스템은
    테이블 영향도와 같은 bare_table_name 으로 뗀다.
    """
    col = (column or "").strip()
    tab = (table or "").strip()
    if "." in col:
        head, col = col.rsplit(".", 1)
        if not tab:
            tab = head
    tab, qualifier = bare_table_name(tab)
    return tab.upper(), col.strip().strip('"').upper(), qualifier


# --------------------------------------------------------------------------
# 1단계 — 찾기와 규칙 분류
# --------------------------------------------------------------------------


def _place(nocomment_line: str, masked_line: str, pat: re.Pattern[str]) -> str:
    """이 줄의 컬럼 이름이 어디에 있는가: 주석 / 문자열 / 코드.

    세 사본을 비교한다 — 원문에만 있으면 주석, 주석만 지운 사본에는 있는데
    문자열까지 지운 사본에 없으면 문자열 안이다. java/js 는 SQL 이 문자열
    안에 있으므로 문자열을 지우지 않는다(masked == nocomment).
    """
    if pat.search(masked_line):
        return "code"
    if pat.search(nocomment_line):
        return "string"
    return "comment"


# 값을 받거나 넘기는 자리: Pro*C·PL/SQL :ord_no, MyBatis #{ordNo} / ${x}.
# :NEW.컬럼 / :OLD.컬럼(트리거)은 그 테이블의 컬럼이라 빼지 않는다.
_HOST_VAR = re.compile(r"(?<![\w.]):(?!(?:NEW|OLD)\.)[A-Za-z_][\w$#]*|[#$]\{[^}]*\}", re.IGNORECASE)
_QUALIFIED_BEFORE = re.compile(r'([A-Za-z_][\w$#]*)"?\s*\.\s*"?$')


def _blank(text: str, pat: re.Pattern[str]) -> str:
    """매치를 같은 길이의 공백으로 — 줄·위치가 원문과 맞아야 줄 번호를 그대로 쓴다."""
    return pat.sub(lambda m: re.sub(r"[^\n]", " ", m.group(0)), text)


def settle(body: str, line_idx: int, table: str, col_pat: re.Pattern[str]) -> dict[str, str] | None:
    """규칙으로 확정되는 줄은 여기서 정한다. 못 정하면 None(LLM 이 본다).

    body 는 주석·호스트 변수를 지운 문장, line_idx 는 문장 안 줄 위치다.
        T.ORD_NO / A.ORD_NO   별칭이 가리키는 테이블로 정한다(FROM/JOIN 에서 찾는다)
        INSERT INTO X (.., C) 괄호 안 목록은 X 의 컬럼이다
        테이블이 하나뿐인 문장  그 테이블의 컬럼이다
    실측에서 LLM 이 틀린 줄이 전부 여기 해당했다(별칭 T, 'B.C = A.C' 가 한 줄).
    """
    lines = body.splitlines()
    if not 0 <= line_idx < len(lines):
        return None
    line = lines[line_idx]
    occurrences = list(col_pat.finditer(line))
    if not occurrences:
        return None
    refs = table_refs(body)
    target = insert_target(body)
    offset = sum(len(x) + 1 for x in lines[:line_idx])
    owners: list[tuple[str | None, str, str, str]] = []     # (테이블, 절, 근거 조각, 규칙)
    for m in occurrences:
        clause = clause_at(body, offset + m.start())
        q = _QUALIFIED_BEFORE.search(line[: m.start()])
        if q:
            owners.append((refs.get(q.group(1).upper()), clause, f"{q.group(1)}.{m.group(0)}", "alias"))
        elif clause == "insert" and target:
            owners.append((target, clause, m.group(0), "insert-list"))
        elif refs and set(refs.values()) == {table}:
            owners.append((table, clause, m.group(0), "single-table"))
        elif target and refs and set(refs.values()) - {target} == {table}:
            # INSERT INTO ORD_HDR (..) SELECT ORD_NO FROM IF_ORDER_TMP — 목록 밖의 컬럼은
            # 읽어 오는 쪽(대상 테이블을 뺀 나머지 하나)의 것이다.
            owners.append((table, clause, m.group(0), "single-source"))
        else:
            owners.append((None, clause, m.group(0), ""))

    def why(owner: str, piece: str, rule: str) -> str:
        if rule == "alias":
            alias = piece.split(".")[0].upper()
            return (f"{piece} — {alias} 는 {owner}" if alias != owner else f"{piece} — 테이블 이름으로 지정")
        if rule == "insert-list":
            return f"INSERT INTO {owner} 의 컬럼 목록이다"
        if rule == "single-source":
            return f"INSERT 대상을 빼면 읽어 오는 테이블이 {owner} 하나뿐이다"
        return f"문장에 나오는 테이블이 {owner} 하나뿐이다"

    mine = [o for o in owners if o[0] == table]
    if mine:
        owner, clause, piece, rule = mine[0]
        usage = clause if clause in USAGE_LABEL and clause != "none" else "other"
        return {"verdict": "yes", "usage": usage, "rule": rule, "reason": why(owner, piece, rule)}
    if all(o[0] for o in owners):
        owner, _clause, piece, rule = owners[0]
        return {"verdict": "no", "usage": "none", "rule": f"{rule}-other",
                "reason": why(owner, piece, rule) + f" — {table} 의 컬럼이 아니다"}
    return None


# 컬럼 이름 없이 컬럼을 건드리는 모양. 문장 본문(주석·문자열 지움)에 쓴다.
_STAR = re.compile(r"(?is)\bSELECT\s+(?:DISTINCT\s+|ALL\s+)?(\*)|([A-Za-z_][\w$#]*)\s*\.\s*\*")
_INSERT_NO_LIST = r"(?is)\bINSERT\s+INTO\s+(?:\"?[A-Za-z_][\w$#]*\"?\.){{0,2}}\"?{t}\"?\s+(VALUES|SELECT|WITH)\b"


def table_only(sql: str, lang: str, table: str) -> dict[str, str]:
    """테이블은 쓰지만 컬럼 이름이 없는 문장의 판정. settle() 과 같은 모양을 돌려준다."""
    try:
        body = strip_comments(sql, lang, mask_strings=True)
    except ValueError:
        body = sql
    if lang == "xml":
        body = _blank(body, _XML_TAG)
    t = table.upper()
    if re.search(_INSERT_NO_LIST.format(t=re.escape(t)), body):
        return {"verdict": "yes", "usage": "insert", "rule": "insert-all-columns",
                "reason": "컬럼 목록 없이 INSERT 한다 — 모든 컬럼(이 컬럼 포함)에 값을 넣는다"}
    refs = table_refs(body)
    for m in _STAR.finditer(body):
        alias = (m.group(2) or "").upper()
        if not alias or refs.get(alias) == t:
            return {"verdict": "unknown", "usage": "select", "rule": "select-star",
                    "reason": f"{m.group(0).strip()} 로 컬럼을 이름 없이 읽는다 — "
                              "프로그램이 이 값을 쓰는지 확인 필요"}
    return {"verdict": "no", "usage": "none", "rule": "column-absent",
            "reason": "테이블을 쓰는 문장이지만 이 컬럼은 나오지 않는다"}


def find_hits(table: str, column: str, roots: list[tuple[str, str]]) -> dict[str, Any]:
    """테이블이 나오는 파일에서 컬럼이 나온 줄을 전부 찾아 규칙으로 분류한다.

    roots 의 모든 루트를 본다. 줄마다 어느 루트에서 나왔는지 남긴다.
    """
    rooted, root_warnings = scan_roots(roots)
    root_of = {f: label for label, f in rooted}
    files = [f for _, f in rooted]
    tab_pat = ident_pattern(table, re.IGNORECASE)
    # SQL 식별자는 대소문자를 가리지 않는다(테이블 영향도와 같다).
    col_pat = ident_pattern(column, re.IGNORECASE)
    hits: list[dict[str, Any]] = []
    unreadable: list[str] = []
    table_files = 0
    truncated = False

    for path in files:
        try:
            text, _enc = read_text_with_encoding(path)
        except OSError as e:
            unreadable.append(f"{path}: {e}")
            continue
        lang = LANG_BY_SUFFIX.get(Path(path).suffix.lower(), "c")
        try:
            nocomment = strip_comments(text, lang)
            masked = strip_comments(text, lang, mask_strings=masks_strings(lang))
        except ValueError:
            nocomment = masked = text
        # 주석에만 테이블이 나오는 파일은 그 테이블을 쓰는 프로그램이 아니다.
        if not tab_pat.search(masked):
            continue
        table_files += 1
        raw_lines = text.splitlines()
        # 테이블을 쓰는 문장 중 컬럼이 안 나오는 것 — 테이블 영향도와 같은 방식으로 자른다.
        seen: set[int] = set()
        for no, mk in enumerate(masked.splitlines(), start=1):
            if not tab_pat.search(mk):
                continue
            stmt = statement_at(text, no, lang)
            if stmt.start_line in seen:
                continue
            seen.add(stmt.start_line)
            if col_pat.search(stmt.sql):
                continue                    # 컬럼이 나온 줄은 아래에서 줄마다 본다
            if len(hits) >= COLUMN_IMPACT_MAX_HITS:
                truncated = True
                break
            info = classify(stmt.sql, table, lang)
            s = table_only(stmt.sql, lang, table)
            hits.append({
                "root": root_of[path], "file": path, "program": Path(path).name, "lang": lang,
                "line": no, "code": (raw_lines[no - 1] if no - 1 < len(raw_lines) else "").strip()[:300],
                "start_line": stmt.start_line, "end_line": stmt.end_line,
                "stmt_kind": info.kind, "table_role": info.role, "sql": stmt.sql,
                "complete": stmt.complete, "place": "table-only",
                "rule_verdict": s["verdict"], "rule": s["rule"], "settled": s,
            })
        if truncated or not col_pat.search(text):
            if truncated:
                break
            continue

        nc_lines = nocomment.splitlines()
        mk_lines = masked.splitlines()
        for no, raw in enumerate(raw_lines, start=1):
            if not col_pat.search(raw):
                continue
            if len(hits) >= COLUMN_IMPACT_MAX_HITS:
                truncated = True
                break
            nc = nc_lines[no - 1] if no - 1 < len(nc_lines) else ""
            mk = mk_lines[no - 1] if no - 1 < len(mk_lines) else ""
            where = _place(nc, mk, col_pat)
            hit: dict[str, Any] = {
                "root": root_of[path], "file": path, "program": Path(path).name, "lang": lang, "line": no,
                "code": raw.strip()[:300], "start_line": no, "end_line": no,
                "stmt_kind": "", "table_role": "", "sql": "",
            }
            if where == "comment":
                hit.update(place="comment", rule_verdict="no", rule="comment-only")
            elif where == "code" and not col_pat.search(_blank(mk, _HOST_VAR)):
                # INTO :ord_no / #{ordNo} — 값을 받는 변수 자리다. 대소문자를 가리지 않고
                # 찾으므로 :ord_no 가 ORD_NO 로 걸린다(실측에서 LLM 이 '영향 있음'으로 봤다).
                hit.update(place="host-var", rule_verdict="no", rule="host-variable")
            else:
                stmt = statement_at(text, no, lang)
                try:
                    body = strip_comments(stmt.sql, lang, mask_strings=masks_strings(lang))
                except ValueError:
                    body = stmt.sql
                if lang == "xml":
                    body = _blank(body, _XML_TAG)      # <select id=".."> 를 SQL 로 읽지 않게
                body = _blank(body, _HOST_VAR)
                same = bool(tab_pat.search(body))
                hit.update(start_line=stmt.start_line, end_line=stmt.end_line, sql=stmt.sql,
                           complete=stmt.complete)
                idx = no - stmt.start_line
                first = col_pat.search((body.splitlines() or [""])[idx]) \
                    if 0 <= idx < len(body.splitlines()) else None
                if first:
                    pos = sum(len(x) + 1 for x in body.splitlines()[:idx]) + first.start()
                    hit["clause"] = clause_at(body, pos)
                if where == "code" and same and stmt.complete:
                    s = settle(body, idx, table, col_pat)
                    if s:
                        info = classify(stmt.sql, table, lang)
                        hit.update(place="same-stmt", stmt_kind=info.kind, table_role=info.role,
                                   rule_verdict=s["verdict"], rule=s["rule"], settled=s)
                        hits.append(hit)
                        continue
                if where == "string":
                    # Pro*C·PL/SQL 의 문자열 안 — 로그 문구일 수도, 동적 SQL 일 수도 있다.
                    hit.update(place="string", rule_verdict="unknown", rule="in-string")
                elif same:
                    info = classify(stmt.sql, table, lang)
                    hit.update(place="same-stmt", stmt_kind=info.kind, table_role=info.role,
                               rule_verdict="yes" if stmt.complete else "unknown",
                               rule="same-statement" if stmt.complete else "partial-statement")
                else:
                    # 테이블을 쓰는 파일이지만 이 문장에는 테이블이 없다 — 다른 테이블의
                    # 같은 이름 컬럼이거나 변수일 가능성이 크다. 단정하지 않는다.
                    hit.update(place="other-code", rule_verdict="unknown", rule="other-statement")
            hits.append(hit)
        if truncated:
            break

    return {"files_scanned": len(files), "table_files": table_files, "hits": hits,
            "unreadable": unreadable, "truncated": truncated, "root_warnings": root_warnings}


# --------------------------------------------------------------------------
# 2단계 — LLM 판정 (문장 단위)
# --------------------------------------------------------------------------


def _numbered(h: dict[str, Any]) -> str:
    """문장에 줄 번호를 붙이고 판정할 줄에 '>>' 를 단다.

    같은 문장 안에서도 줄마다 답이 다르다 — INSERT INTO ORD_HDR (.., TOTAL_AMT)
    는 ORD_HDR 의 컬럼이고 바로 아래 SELECT .. TOTAL_AMT FROM IF_ORDER_TMP 는
    IF_ORDER_TMP 의 컬럼이다. 문장째 한 번에 물으면 두 줄이 같은 답을 받는다(실측).
    """
    lines = (h["sql"] or h["code"]).splitlines() or [h["code"]]
    out = []
    for i, text in enumerate(lines, start=h["start_line"]):
        mark = ">>" if i == h["line"] else "  "
        out.append(f"{mark}{i:5d}| {text}")
    return "\n".join(out)[:3500]


def _prompt(table: str, column: str, h: dict[str, Any]) -> str:
    where = {
        "same-stmt": f"이 문장에는 테이블 {table} 가 함께 나온다.",
        "other-code": f"이 문장에는 테이블 {table} 가 나오지 않는다.",
        "string": "판정할 줄의 컬럼 이름은 문자열 리터럴 안에 있다.",
    }.get(h["place"], "")
    # 규칙은 실측 오판에서 나왔다(qwen3.5:4b). 'INTO :amt' 를 보고 변수라며 no,
    # Java 문자열 속 SQL 을 '문자열일 뿐'이라며 no 로 답했다. yes/no 를 바로 물으면
    # 근거에는 'ORD_HDR 의 컬럼'이라 쓰고 답은 yes 로 냈다 — 그래서 '어느 테이블의
    # 컬럼인가'를 묻고 비교는 코드가 한다(Verdict 스키마 순서도 근거가 먼저다).
    return (
        f"'>>' 가 붙은 줄에 나온 {column} 이 무엇인지 판단해라.\n"
        "kind:\n"
        "- column: SQL 안의 테이블 컬럼. owner_table 에 그 컬럼이 속한 테이블 이름을 적는다.\n"
        "  'SELECT 컬럼 INTO :변수' 에서 :변수 는 값을 받는 자리일 뿐이고 컬럼은 그대로 컬럼이다.\n"
        "  Java·JS 문자열 안의 SQL 도 SQL 이다.\n"
        "  'INSERT INTO X (.., 컬럼)' 의 괄호 안 목록은 X 의 컬럼이다.\n"
        "  'UPDATE X SET 컬럼 = ..' 은 X 의 컬럼이다.\n"
        "  별칭이 붙어 있으면(B.컬럼) FROM/JOIN 에서 그 별칭이 어느 테이블인지 찾아 적는다.\n"
        "- variable: SQL 이 아닌 프로그램 변수(C 변수 선언·대입 등).\n"
        "- text: 로그·메시지 문구 같은 글자.\n"
        "- unknown: 근거가 부족하다. 추측하지 마라.\n"
        "usage(column 일 때): select(조회 목록), where(조건), insert(입력 값), update(SET), "
        "join(조인 조건), order_group(ORDER/GROUP BY), other. 아니면 none.\n\n"
        f"파일: {h['program']} ({h['lang']}). {where}\n\n{_numbered(h)}"
    )


def _usage(h: dict[str, Any], llm_usage: str) -> str:
    """쓰인 자리. SQL 절로 정한 것이 있으면 그것, 없으면 LLM 답."""
    clause = h.get("clause") or ""
    if clause in USAGE_LABEL and clause not in ("none", "other"):
        return clause
    return llm_usage if llm_usage in USAGE_LABEL and llm_usage != "none" else "other"


def verdict_of(kind: str, owner: str, table: str) -> str:
    """LLM 의 답(무엇인가, 어느 테이블인가)을 판정으로 바꾼다. 비교는 코드가 한다."""
    if kind in ("variable", "text"):
        return "no"
    if kind != "column":
        return "unknown"
    name = bare_table_name(owner or "")[0].strip().upper()
    if not name:
        return "unknown"        # 컬럼이라면서 테이블을 못 댔다
    return "yes" if name == table.upper() else "no"


def judge(table: str, column: str, hits: list[dict[str, Any]]) -> list[str]:
    """규칙으로 끝나지 않은 줄을 줄마다 LLM 이 판정한다. 경고 목록을 돌려준다.

    규칙으로 확정된 줄(주석·호스트 변수·별칭·INSERT 목록·테이블 하나뿐인 문장)은
    LLM 에 보내지 않는다. 나머지는 LLM 판정이 최종이다(규칙 추정은 그리드에 함께
    남긴다) — 같은 이름의 컬럼이 여러 테이블에 흔해 규칙만으로는 가를 수 없다.
    쓰인 자리(조회·조건·수정…)는 SQL 절로 정하고, 절을 모를 때만 LLM 답을 쓴다.
    """
    for h in hits:
        s = h.get("settled")
        if s:
            h.update(verdict=s["verdict"], usage=s["usage"], decided_by="rule", reason=s["reason"])
            continue
        h.update(verdict=h["rule_verdict"], usage="none", decided_by="rule",
                 reason={"comment-only": "주석 안에만 나온다",
                         "host-variable": "값을 받는 변수 자리(:변수, #{변수})다 — 컬럼이 아니다",
                         "same-statement": "테이블과 같은 SQL 문장에 나온다(LLM 미확인)",
                         "partial-statement": "문장 끝을 찾지 못한 조각이다",
                         "in-string": "문자열 안에 있다(LLM 미확인)",
                         "other-statement": "테이블이 없는 문장이다(LLM 미확인)"}[h["rule"]])
        if h["verdict"] == "yes":
            h["usage"] = _usage(h, "")
    if not USE_LLM:
        return []

    # 테이블과 같은 문장이 먼저다 — 상한에 걸려도 영향이 큰 쪽은 판정된다.
    order = sorted((h for h in hits
                    if h["place"] not in ("comment", "host-var") and not h.get("settled")),
                   key=lambda h: ({"same-stmt": 0, "string": 1}.get(h["place"], 2),
                                  h["file"], h["line"]))
    targets = order[:COLUMN_IMPACT_MAX_LLM]
    warnings: list[str] = []
    if len(order) > len(targets):
        warnings.append(f"LLM 판정 상한({COLUMN_IMPACT_MAX_LLM}줄)을 넘은 "
                        f"{len(order) - len(targets)}줄은 규칙 추정만 있다 — 확인 필요 "
                        "(COLUMN_IMPACT_MAX_LLM)")
    if not targets:
        return warnings

    from pydantic import BaseModel, Field

    from core.llm import invoke_structured

    class Verdict(BaseModel):
        # 순서가 중요하다 — 소형 모델은 앞 필드부터 채운다. 근거를 먼저 쓰게 한다.
        reason: str = Field(description="한 문장 근거")
        kind: Literal["column", "variable", "text", "unknown"] = Field(
            description="'>>' 줄의 이름이 무엇인가")
        owner_table: str = Field(description="column 이면 그 컬럼이 속한 테이블 이름, 아니면 빈 값")
        usage: Literal["select", "where", "insert", "update", "join", "order_group",
                       "other", "none"] = Field(description="쓰인 자리")

    failures = 0
    for idx, h in enumerate(targets, start=1):
        notify(f"컬럼 판정 중 {idx}/{len(targets)} ({h['program']}:{h['line']})")
        try:
            v, note = invoke_structured(Verdict, _prompt(table, column, h))
        except Exception as e:
            failures += 1
            h.update(verdict="unknown", decided_by="llm-failed",
                     reason=f"LLM 판정 실패: {type(e).__name__}")
            if failures >= 2 and failures == idx:
                # 처음부터 연달아 실패하면 연결 문제다. 남은 수십 건을 타임아웃까지 기다리지 않는다.
                warnings.append(f"LLM 판정 실패 — 확인 필요: {type(e).__name__}: {e}")
                for rest in targets[idx:]:
                    rest.update(verdict="unknown", decided_by="llm-failed",
                                reason="LLM 연결 실패로 판정하지 못했다")
                return warnings
            continue
        if note and note not in warnings:
            warnings.append(note)
        verdict = verdict_of(v.kind, v.owner_table, table)
        reason = v.reason.strip()[:200]
        conflict = h["rule_verdict"] != "unknown" and h["rule_verdict"] != verdict
        decided_by = "llm"
        if conflict and h["rule_verdict"] == "yes":
            # 규칙은 '그 테이블이 있는 문장'이라 영향 있음, LLM 은 아니라고 했다. LLM 이 규칙을
            # 뒤집지 않는다 — 영향 없음으로 두면 그 줄이 답에서 사라져 영향을 놓친다(사용자 지적:
            # 그리드 '규칙 추정' 은 영향 있음인데 판정·챗봇 답은 영향 없음). 사람이 볼 것으로 남긴다.
            owner = v.owner_table.strip()
            said = {"variable": "변수", "text": "글자"}.get(v.kind) or (
                f"{owner} 의 컬럼" if owner else "판단 못 함")
            verdict, decided_by = "unknown", "rule+llm"
            reason = (f"규칙은 영향 있음(테이블과 같은 문장), LLM 은 {said} — 둘이 갈려 확인 필요. "
                      f"LLM 근거: {reason}")[:300]
        h.update(verdict=verdict, usage=_usage(h, v.usage) if verdict == "yes" else "none",
                 decided_by=decided_by, reason=reason,
                 llm_kind=v.kind, llm_owner=v.owner_table.strip(), conflict=conflict)
    if failures:
        warnings.append(f"LLM 판정 실패 {failures}문장 — 확인 필요")
    return warnings


# --------------------------------------------------------------------------
# 진입점
# --------------------------------------------------------------------------


# 툴 답(요약)과 그리드(리소스)가 같은 결과를 쓰게 한다. 챗봇 답이 나온 뒤
# [표로 보기] 를 누르면 리소스가 같은 조사를 다시 부르는데, LLM 판정을 다시
# 돌리면 수십 초를 또 기다린다.
_RESULTS = TTLCache(ttl=1800, maxsize=16)


def _analyze(table: str, column: str, root: str = "") -> dict[str, Any]:
    """판정까지 끝낸 전체 결과. LLM 이 실패한 결과는 캐시하지 않는다 —
    Ollama 가 잠깐 죽었던 결과가 30분 동안 '확인 필요'로 남으면 안 된다."""
    key = split_names(table, column)[:2] + ((root or "").strip() or "-",)
    hit = _RESULTS.get(key)
    if hit is not None:
        return hit
    r = _analyze_uncached(table, column, root)
    if not any(h["decided_by"] == "llm-failed" for h in r["hits"]):
        _RESULTS.set(key, r)
    return r


def _analyze_uncached(table: str, column: str, root: str = "") -> dict[str, Any]:
    tab, col, qualifier = split_names(table, column)
    # 비우거나 '-' 면 설정된 루트 전부(usage·테이블 영향도와 같은 resolve_roots).
    roots, root_warn = resolve_roots(root)
    root_label = roots_label(roots) or (root or "").strip()
    out: dict[str, Any] = {
        "table": tab, "column": col, "qualifier": qualifier, "root": root_label,
        "files_scanned": 0, "table_files": 0, "hits": [], "warnings": [],
        "rule": "", "decided_by": "fallback",
    }
    if not tab or not col:
        return {**out, "rule": "empty-name", "warnings": ["테이블과 컬럼 이름이 모두 필요하다"]}
    if not roots:
        return {**out, "rule": "unknown-root", "warnings": root_warn}

    notify(f"{root_label} 소스에서 {tab} 을(를) 쓰는 파일 찾는 중")
    found = find_hits(tab, col, roots)
    hits = found["hits"]
    warnings: list[str] = list(found["root_warnings"])
    if found["unreadable"]:
        warnings.append(f"읽지 못한 파일 {len(found['unreadable'])}건 — 확인 필요")
    if found["truncated"]:
        warnings.append(f"찾은 줄이 {COLUMN_IMPACT_MAX_HITS}줄을 넘어 앞부분만 담았다 — "
                        "확인 필요 (COLUMN_IMPACT_MAX_HITS)")
    if hits:
        notify(f"{tab}.{col} — {len(hits)}줄 찾음, 판정 시작")
        warnings += judge(tab, col, hits)

    rank = {"yes": 0, "unknown": 1, "no": 2}
    hits.sort(key=lambda h: (rank[h["verdict"]], h["root"], h["file"], h["line"]))
    if not found["files_scanned"]:
        rule = "no-files"            # 루트별 '소스가 없다' 경고는 scan_roots 가 이미 냈다
    elif not found["table_files"]:
        rule = "no-table-hit"
    elif not any(h["place"] != "table-only" for h in hits):
        rule = "no-column-hit"       # 테이블을 쓰는 문장(table-only 행)은 있다
    else:
        rule = "hits-found"
    return {**out, "files_scanned": found["files_scanned"], "table_files": found["table_files"],
            "hits": hits, "warnings": warnings, "rule": rule,
            "decided_by": "llm" if any(h["decided_by"] in ("llm", "rule+llm") for h in hits) else "rule"}


def counts_of(hits: list[dict[str, Any]]) -> dict[str, int]:
    return {k: sum(1 for h in hits if h["verdict"] == k) for k in ("yes", "unknown", "no")}


def run_column_impact(table: str, column: str, root: str = "",
                      detail: Detail = "full") -> dict[str, Any]:
    """진입점.

        full    : 찾은 줄 전부와 문장 전문 (리소스·그리드용)
        summary : 판정별 건수와 영향 있는 곳 몇 줄 (챗봇 툴 답용)
    """
    r = _analyze(table, column, root)
    hits = r["hits"]
    base = {k: r[k] for k in ("table", "column", "qualifier", "root", "files_scanned",
                              "table_files", "rule", "decided_by", "warnings")}
    base["counts"] = counts_of(hits)
    # 컬럼이 나온 줄과, 테이블은 쓰지만 컬럼이 없는 문장을 따로 센다. 테이블 영향도에는
    # 나오는데 여기서 빠지면 '안 본 것'인지 '안 쓰는 것'인지 알 수 없다.
    only = [h for h in hits if h["place"] == "table-only"]
    base["table_only"] = len(only)
    base["table_only_counts"] = counts_of(only)
    base["programs"] = sorted({h["program"] for h in hits})
    if detail == "summary":
        # 루트가 여럿이면 같은 파일 이름이 루트마다 있을 수 있다. 위치에 루트를 붙인다.
        many = len({h["root"] for h in hits}) > 1

        def where(h: dict[str, Any]) -> str:
            return (f"[{h['root']}] " if many else "") + f"{h['program']}:{h['line']}"

        return {
            **base,
            "hit_count": len(hits) - len(only),
            "places": [
                f"{where(h)} {USAGE_LABEL[h['usage']]} — {h['code'][:80]}"
                for h in hits if h["verdict"] == "yes"
            ][:10],
            # 같은 내용을 조각으로. 화면에 위치·코드를 따로 그릴 때 쓴다(문자열을 다시 자르지 않게).
            "place_items": [
                {"where": where(h), "usage": USAGE_LABEL[h["usage"]], "code": h["code"][:80]}
                for h in hits if h["verdict"] == "yes"
            ][:10],
            "unknown_items": [
                {"where": where(h), "code": h["code"][:80],
                 "why": "규칙·LLM 갈림" if h["decided_by"] == "rule+llm" else
                        "LLM 실패" if h["decided_by"] == "llm-failed" else ""}
                for h in hits if h["verdict"] == "unknown"
            ][:10],
            "unknown_places": [where(h) for h in hits if h["verdict"] == "unknown"][:5],
        }
    return {**base, "hits": hits}


GRID_COLUMNS = [
    ("verdict", "판정"), ("root", "루트"), ("program", "프로그램"), ("line", "줄"), ("usage", "용도"),
    ("place", "위치"), ("stmt", "문장"), ("code", "코드"), ("reason", "근거"),
    ("decided_by", "판정 주체"), ("rule_verdict", "규칙 추정"), ("file", "경로"),
]


def column_grid(table: str, column: str, root: str = "") -> dict[str, Any]:
    """화면 그리드용. 표 머리와 행을 함께 준다 — 화면은 이 툴을 몰라도 그린다."""
    r = run_column_impact(table, column, root, detail="full")
    rows = [
        {
            "verdict": VERDICT_LABEL[h["verdict"]],
            "root": h["root"],
            "program": h["program"],
            "line": h["line"],
            "usage": USAGE_LABEL[h["usage"]],
            "place": PLACE_LABEL[h["place"]],
            "stmt": (f"{h['stmt_kind'] or '-'} {h['start_line']}~{h['end_line']}"
                     if h["place"] != "comment" else "-"),
            "code": h["code"],
            "reason": h["reason"],
            "decided_by": {"llm": "LLM", "rule": "규칙", "llm-failed": "LLM 실패",
                           "rule+llm": "규칙·LLM 갈림"}[h["decided_by"]],
            "rule_verdict": VERDICT_LABEL[h["rule_verdict"]]
            + (" ≠ LLM" if h.get("conflict") else ""),
            "file": h["file"],
        }
        for h in r["hits"]
    ]
    c = r["counts"]
    return {
        "title": f"{r['table']}.{r['column']} 컬럼 영향도",
        "subtitle": (f"{r['root']} · 테이블을 쓰는 파일 {r['table_files']}개 · 컬럼이 나온 줄 "
                     f"{len(rows) - r['table_only']}개 + 테이블만 쓰는 문장 {r['table_only']}개 "
                     f"(영향 있음 {c['yes']} / 확인 필요 {c['unknown']} / 영향 없음 {c['no']})"),
        "grid": {"columns": [{"key": k, "label": v} for k, v in GRID_COLUMNS], "rows": rows,
                 # 화면은 이 칸의 값으로 걸러 보기 버튼을 만들고 색을 입힌다.
                 "filter_key": "verdict",
                 "tones": {VERDICT_LABEL["yes"]: "bad", VERDICT_LABEL["unknown"]: "warn",
                           VERDICT_LABEL["no"]: "muted"}},
        "warnings": r["warnings"],
    }


if __name__ == "__main__":
    import argparse

    ap = argparse.ArgumentParser(description="컬럼 영향도 조사")
    ap.add_argument("table")
    ap.add_argument("column")
    ap.add_argument("--root", default="",
                    help=f"소스 루트 ({', '.join(SOURCE_ROOTS)}). 비우면 전부, 쉼표로 여러 개")
    args = ap.parse_args()

    g = column_grid(args.table, args.column, args.root)
    print(g["title"], "—", g["subtitle"])
    for row in g["grid"]["rows"]:
        print(f"  [{row['verdict']}] {row['program']}:{row['line']}  {row['usage']}  "
              f"{row['place']}  {row['code'][:60]}")
        print(f"      └ {row['decided_by']}: {row['reason']}")
    for w in g["warnings"]:
        print(f"  [확인 필요] {w}")
