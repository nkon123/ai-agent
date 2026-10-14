"""iferr — 인터페이스 오류 메일 → 키 추출 → DB 조회 → 영향 확인.

타 시스템에서 데이터를 받다 오류가 나면 인터페이스 키가 적힌 메일이 온다.
그 키로 테이블을 확인해 '어떤 데이터가 어떻게 영향을 받는지'를 정리한다.

    메일 수집 → 오류 판정·키 추출(LLM + 키워드 안전망) → DB 조회 → 영향 판정
    → (선택) LLM 요약

단독 실행:
    python agents/iferr/agent.py                       # 최근 24시간
    python agents/iferr/agent.py --hours 72
    python agents/iferr/agent.py --key IF_ORD_SEND     # 키를 직접 지정
    python agents/iferr/agent.py --subject "[IF 오류] 주문"   # 제목으로 메일 한 통 지정
    MAIL_BACKEND=eml python agents/iferr/agent.py      # 파일로 테스트

설계 원칙 (여기서 특히 중요하다):
    - 메일은 읽기만 한다. 회신·삭제·이동을 하지 않는다.
    - DB 는 SELECT 만. 바인드 변수만 쓴다. DML/DDL 은 만들지도 실행하지도
      않는다 — 조치가 필요하면 사람이 판단한다.
    - '오류가 없다'와 '확인하지 못했다'를 절대 같은 값으로 쓰지 않는다.
      메일을 못 읽었는데 '오류 없음'으로 보이면 장애를 놓친다.
    - 발신자 주소는 마스킹해서 남긴다.
"""

from __future__ import annotations

import re
import sys
from datetime import datetime
from pathlib import Path
from typing import Any, Literal, TypedDict

_ROOT = Path(__file__).resolve().parents[2]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from langgraph.graph import END, START, StateGraph  # noqa: E402

from config import (  # noqa: E402
    IFERR_KEY_PATTERNS,
    IFERR_KEY_PREFIXES,
    IFERR_LLM_BODY_CHARS,
    IFERR_LLM_MAX_MAILS,
    IFERR_MAIL_JUDGE,
    IFERR_MASTER_FIELDS,
    IFERR_MASTER_FILE,
    IFERR_MAX_ROWS,
    IFERR_SQL,
    IFERR_STATUS_COLUMNS,
    IFERR_TABLE_SQL,
    JUDGE_MODEL,
    MAIL_LOOKBACK_HOURS,
    MAIL_SUBJECT_KEYWORDS,
    MAIL_SUBJECT_LOOKBACK_HOURS,
    MAIL_SUBJECT_MATCH,
    MAIL_SUBJECT_STRIP_PREFIXES,
    USE_LLM,
)
from core import oracle  # noqa: E402
from core.cache import cached  # noqa: E402
from core.outlook import Mail, MailUnavailable, read_mails  # noqa: E402
from core.progress import notify  # noqa: E402
from core.sqlstmt import bare_table_name  # noqa: E402
from core.text import ident_pattern  # noqa: E402

Detail = Literal["full", "summary", "minimal"]

# 정규식은 매번 컴파일하지 않는다. 메일 수백 통 × 패턴 수만큼 돈다.
_COMPILED: list[tuple[str, re.Pattern[str]]] = [
    (name, re.compile(pat)) for name, pat in IFERR_KEY_PATTERNS
]


class IfErrState(TypedDict, total=False):
    hours: int
    only_key: str
    subject: str
    mails: list[Mail]
    mail_count: int
    cases: list[dict[str, Any]]
    decided_by: str
    rule: str
    warnings: list[str]


# --------------------------------------------------------------------------
# 키 추출 — 순수 함수라 테스트하기 쉽다
# --------------------------------------------------------------------------


def extract_keys(text: str) -> list[dict[str, str]]:
    """텍스트에서 인터페이스 키를 뽑는다. [{key, rule, evidence}]

    같은 키가 여러 번 나와도 한 번만 돌려준다(순서 유지).
    evidence 에는 매칭된 실제 조각을 남긴다 — 규칙명만으로는 왜 그 키가
    뽑혔는지 나중에 알 수 없다.
    """
    found: list[dict[str, str]] = []
    seen: set[str] = set()
    for rule, pattern in _COMPILED:
        for m in pattern.finditer(text or ""):
            key = (m.group(1) if m.groups() else m.group(0)).strip()
            if not key or key.upper() in seen:
                continue
            seen.add(key.upper())
            # 근거는 매칭 주변을 그대로. 앞뒤를 조금 붙여 문맥을 남긴다.
            start, end = max(0, m.start() - 20), min(len(text), m.end() + 20)
            found.append(
                {
                    "key": key,
                    "rule": rule,
                    "evidence": " ".join(text[start:end].split()),
                }
            )
    return found


# --------------------------------------------------------------------------
# 원인 컬럼 추출 — 메일의 오류 내용에서 '어느 테이블의 어느 컬럼이 문제인가'
#
# 사이즈 초과(ORA-12899)·NULL(ORA-01400)은 Oracle 메시지 형식이 정해져 있어
# 규칙으로 정확히 뽑힌다. 영문·한글 메시지 둘 다 본다. 형식 없이 사람이 쓴
# 메일은 LLM 이 컬럼 이름을 내고, 메일에 그 이름이 실제로 있을 때만 쓴다.
# --------------------------------------------------------------------------

_Q = r'"([^"]+)"'                     # "ERP" / "ORD_HDR" / "CUST_NM"
_QUALIFIED = rf"{_Q}(?:\.{_Q})?(?:\.{_Q})?"
_ISSUE_RULES: list[tuple[str, str, re.Pattern[str]]] = [
    # ORA-12899: value too large for column "ERP"."IF_ORDER_TMP"."ORD_NO" (actual: 25, maximum: 20)
    # ORA-12899: "ERP"."IF_ORDER_TMP"."ORD_NO" 열에 대한 값이 너무 큼(실제: 25, 최대값: 20)
    ("ora-12899", "size", re.compile(rf"ORA-12899\b[^\"\n]*{_QUALIFIED}", re.IGNORECASE)),
    # ORA-01400: cannot insert NULL into ("ERP"."ORD_HDR"."CUST_CD")
    # ORA-01400: NULL을 ("ERP"."ORD_HDR"."CUST_CD") 안에 삽입할 수 없습니다
    ("ora-01400", "null", re.compile(rf"ORA-01400\b[^\"\n]*\(?{_QUALIFIED}", re.IGNORECASE)),
]
# 길이: (actual: 25, maximum: 20) / (실제: 25, 최대값: 20)
_SIZES = re.compile(r"(?:actual|실제)\s*[:：]\s*(\d+)\D{1,12}?(?:maximum|최대값?)\s*[:：]\s*(\d+)",
                    re.IGNORECASE)
# 컬럼 이름 없이 오는 사이즈 계열 — '컬럼은 모른다'를 남긴다.
_ISSUE_NO_COLUMN = [
    ("ora-01401", "size", re.compile(r"ORA-01401\b", re.IGNORECASE)),
    ("ora-01438", "precision", re.compile(r"ORA-01438\b", re.IGNORECASE)),
]
ISSUE_LABEL = {"size": "길이 초과", "null": "NULL 불가", "precision": "숫자 자릿수 초과",
               "other": "컬럼 오류"}


def extract_issues(text: str) -> list[dict[str, str]]:
    """오류 내용에서 원인 컬럼을 뽑는다. [{kind, rule, schema, table, column, actual, max, evidence}]

    "A"."B"."C" 는 스키마.테이블.컬럼, "B"."C" 는 테이블.컬럼으로 본다. 컬럼 하나만
    따옴표로 오면 테이블은 비워 둔다 — 지어내지 않는다(호출부가 마스터의 도착
    테이블로 채운다). 같은 (테이블, 컬럼) 은 한 번만.
    """
    text = text or ""
    found: list[dict[str, str]] = []
    seen: set[tuple[str, str]] = set()
    for rule, kind, pat in _ISSUE_RULES:
        for m in pat.finditer(text):
            parts = [p for p in m.groups() if p]
            column = parts[-1]
            table = parts[-2] if len(parts) >= 2 else ""
            schema = parts[-3] if len(parts) >= 3 else ""
            key = (table.upper(), column.upper())
            if key in seen:
                continue
            seen.add(key)
            tail = text[m.end(): m.end() + 80]
            sizes = _SIZES.search(tail) if kind == "size" else None
            start, end = max(0, m.start() - 10), min(len(text), m.end() + 50)
            found.append({
                "kind": kind, "rule": rule, "schema": schema, "table": table,
                "column": column, "actual": sizes.group(1) if sizes else "",
                "max": sizes.group(2) if sizes else "",
                "evidence": " ".join(text[start:end].split()),
            })
    for rule, kind, pat in _ISSUE_NO_COLUMN:
        m = pat.search(text)
        if m and not any(f["kind"] == kind for f in found):
            start, end = max(0, m.start() - 10), min(len(text), m.end() + 60)
            found.append({"kind": kind, "rule": rule, "schema": "", "table": "", "column": "",
                          "actual": "", "max": "", "evidence": " ".join(text[start:end].split())})
    return found


def verify_llm_columns(text: str, columns: list[str]) -> list[dict[str, str]]:
    """LLM 이 낸 원인 컬럼 중 메일에 실제로 적힌 것만 issue 로 만든다.

    'ERP.ORD_HDR.CUST_NM' 처럼 점으로 이어 내면 마지막 조각이 컬럼, 그 앞이 테이블이다.
    지어낸 컬럼으로 영향도를 조사하면 엉뚱한 프로그램이 '영향 있음'으로 나온다.
    """
    out: list[dict[str, str]] = []
    seen: set[str] = set()
    for raw in columns:
        cand = str(raw).strip().strip("[](){}<>\"'`.,:;")
        parts = [p.strip('"') for p in cand.split(".") if p.strip('"')]
        if not parts:
            continue
        column, table = parts[-1], (parts[-2] if len(parts) >= 2 else "")
        if not _ID_SHAPE.fullmatch(column) or column.upper() in seen:
            continue
        m = ident_pattern(column, re.IGNORECASE).search(text)
        if not m:
            continue
        if table and not ident_pattern(table, re.IGNORECASE).search(text):
            table = ""                     # 테이블은 메일에 없다 — 버리고 마스터로 채운다
        seen.add(column.upper())
        start, end = max(0, m.start() - 30), min(len(text), m.end() + 30)
        out.append({"kind": "other", "rule": "llm", "schema": "", "table": table,
                    "column": m.group(0), "actual": "", "max": "",
                    "evidence": " ".join(text[start:end].split())})
    return out


def issue_text(i: dict[str, str]) -> str:
    """'IF_ORDER_TMP.ORD_NO 길이 초과(실제 25 > 최대 20)'."""
    where = ".".join(x for x in (i.get("table"), i.get("column")) if x) or "컬럼 미상"
    size = f"(실제 {i['actual']} > 최대 {i['max']})" if i.get("actual") and i.get("max") else ""
    return f"{where} {ISSUE_LABEL.get(i.get('kind', ''), '컬럼 오류')}{size}"


def normalize_subject(subject: str) -> str:
    """비교용으로 제목을 다듬는다. 회신·전달 머리말을 반복 제거한다.

    "RE: FW: (EAA) Alert Mail ..." 처럼 여러 번 붙는 경우가 있어
    한 번만 떼면 부족하다.

    기본 설정(MAIL_SUBJECT_STRIP_PREFIXES 가 비어 있음)에서는 아무것도
    떼지 않는다 — startswith 는 '문자 그대로 그 문구로 시작'을 뜻해야 한다.
    """
    s = (subject or "").strip()
    changed = True
    while changed:
        changed = False
        for p in MAIL_SUBJECT_STRIP_PREFIXES:
            p = p.strip()
            if p and s.upper().startswith(p.upper()):
                s = s[len(p) :].lstrip()
                changed = True
    return s


def match_info(mail: Mail) -> tuple[str, bool]:
    """(걸린 키워드, 머리말을 떼고서야 걸렸는가).

    두 번째 값을 남기는 이유: 머리말 제거를 켜 두면 "RE: FW: ..." 사본도
    걸리는데, 결과만 보면 원본 알림과 구별되지 않는다. 어느 쪽인지
    화면에 보여야 '왜 두 건으로 잡혔지'를 바로 안다.
    """
    raw = (mail.subject or "").strip()
    hit = _match_against(raw)
    if hit:
        return hit, False
    if MAIL_SUBJECT_STRIP_PREFIXES:
        stripped = normalize_subject(raw)
        if stripped != raw:
            hit = _match_against(stripped)
            if hit:
                return hit, True
    return "", False


def _match_against(subject: str) -> str:
    """제목 문자열 하나를 키워드들과 비교한다. 대소문자는 무시한다."""
    upper = subject.upper()
    mode = (MAIL_SUBJECT_MATCH or "contains").strip().lower()
    for k in MAIL_SUBJECT_KEYWORDS:
        k = k.strip()
        if not k:
            continue
        if mode == "startswith":
            if upper.startswith(k.upper()):
                return k
        elif mode == "regex":
            try:
                if re.search(k, subject, re.IGNORECASE):
                    return k
            except re.error:
                # 잘못된 정규식을 조용히 넘기면 '오류 메일 없음'으로 보인다.
                # 여기서는 건너뛰되 check_subject_rule() 이 따로 알려 준다.
                continue
        elif upper.find(k.upper()) >= 0:
            return k
    return ""


def matched_keyword(mail: Mail) -> str:
    """제목에 걸린 키워드를 돌려준다. 안 걸리면 빈 문자열.

    걸린 키워드를 남기는 이유: '왜 이 메일이 오류로 잡혔지'를 결과만
    보고는 알 수 없다. 판정에는 반드시 근거가 따라와야 한다(규칙 4-6).
    설정을 잘못 써서 엉뚱한 메일이 걸리는 경우가 실제로 있었다.

    비교 방식은 config.MAIL_SUBJECT_MATCH 로 정한다.
    대소문자는 어느 모드에서나 무시한다.
    """
    return match_info(mail)[0]


def check_subject_rule() -> list[str]:
    """제목 판정 설정이 쓸 만한지 점검한다. 문제 목록을 돌려준다.

    설정이 조용히 잘못되어 있으면 '오류 메일이 하나도 없다'로 보인다.
    그건 장애를 놓치는 가장 흔한 경로다.
    """
    problems: list[str] = []
    mode = (MAIL_SUBJECT_MATCH or "").strip().lower()
    if mode not in ("contains", "startswith", "regex"):
        problems.append(
            f"MAIL_SUBJECT_MATCH 값이 이상하다: {MAIL_SUBJECT_MATCH!r} "
            "(contains | startswith | regex)"
        )
    if not [k for k in MAIL_SUBJECT_KEYWORDS if k.strip()]:
        problems.append("MAIL_SUBJECT_KEYWORDS 가 비어 있어 어떤 메일도 대상이 아니다")
    for k in MAIL_SUBJECT_KEYWORDS:
        if len(k.strip()) == 1:
            problems.append(
                f"키워드 '{k}' 가 한 글자다 — 쉼표를 빠뜨렸을 수 있다 "
                '(("오류",) 처럼 쉼표를 붙일 것)'
            )
        if mode == "regex":
            try:
                re.compile(k)
            except re.error as e:
                problems.append(f"정규식이 잘못됐다: {k!r} — {e}")
    return problems


# "3일", "12시간" 같은 기간 표현. 단위별 시간 수.
_PERIOD_UNITS: tuple[tuple[str, int], ...] = (
    ("주", 24 * 7), ("week", 24 * 7), ("w", 24 * 7),
    ("일", 24), ("day", 24), ("d", 24),
    ("시간", 1), ("hour", 1), ("hr", 1), ("h", 1),
)


def parse_period(text: str, default_hours: int) -> tuple[int, str, str]:
    """기간 표현을 시간 수로. (시간, 표시문구, 경고)

    소형 모델에 시간 수를 계산시키면 "3일"을 3으로 넣는다. 실제로 그랬다.
    그래서 툴은 사용자가 말한 기간을 '그대로' 받고, 계산은 여기서 한다.

    못 알아들으면 조용히 기본값을 쓰지 않는다. 기본값을 쓰되 경고를 남겨
    "3일이라 했는데 왜 하루치지"를 알 수 있게 한다.
    """
    raw = (text or "").strip()
    if not raw:
        return default_hours, f"최근 {default_hours}시간", ""

    lowered = raw.lower().replace("최근", "").replace("지난", "").strip()

    now = datetime.now()
    if "오늘" in lowered or "today" in lowered:
        # 자정부터. 최소 1시간은 봐야 방금 온 메일이 빠지지 않는다.
        return max(1, now.hour + 1), "오늘", ""
    if "어제" in lowered or "yesterday" in lowered:
        return now.hour + 25, "어제부터", ""

    m = re.search(r"(\d+)\s*([A-Za-z가-힣]*)", lowered)
    if m:
        n = int(m.group(1))
        unit = m.group(2)
        for suffix, mult in _PERIOD_UNITS:
            if unit.startswith(suffix):
                hours = max(1, n * mult)
                return hours, f"최근 {n}{suffix}", ""
        if not unit:
            # 단위 없는 숫자는 시간으로 본다(예전 인자와 같은 뜻).
            return max(1, n), f"최근 {n}시간", ""

    return default_hours, f"최근 {default_hours}시간", (
        f"기간 '{raw}' 를 이해하지 못해 기본값({default_hours}시간)으로 조회했다 "
        "— 확인 필요"
    )


# 사용자가 말한 제목을 감싼 따옴표. 제목 자체의 [IF 오류] 같은 대괄호는 남긴다.
_SUBJECT_QUOTES = "\"'`“”‘’「」『』"


def subject_key(text: str) -> str:
    """제목 비교용. 대소문자·공백을 무시한다 — 사람은 "[IF 오류]" 를 "[IF오류]" 로도 쓴다."""
    return re.sub(r"\s+", "", (text or "").strip().strip(_SUBJECT_QUOTES)).casefold()


def pick_by_subject(mails: list[Mail], subject: str) -> tuple[list[Mail], list[str]]:
    """제목에 subject 가 들어간 메일 중 가장 최근 한 통. (고른 메일, 경고)

    '이 메일' 을 물었으니 한 통만 본다. 같은 제목이 여러 통이면(매일 오는 알림) 가장
    최근 것을 보고 그 사실을 알린다 — 어느 메일을 봤는지 모르면 답을 믿을 수 없다.
    """
    want = subject_key(subject)
    hits = [m for m in mails if want and want in subject_key(m.subject)]
    if len(hits) <= 1:
        return hits, []
    hits.sort(key=lambda m: m.received or datetime.min, reverse=True)
    top = hits[0]
    when = top.received.strftime("%m-%d %H:%M") if top.received else "시각 미상"
    return [top], [f"제목에 '{subject.strip()}' 이(가) 들어간 메일이 {len(hits)}통이라 가장 최근 "
                   f"1통({when} '{top.subject[:60]}')만 확인했다 — 다른 메일이면 제목을 더 "
                   "자세히 말할 것"]


def is_error_mail(mail: Mail) -> bool:
    """제목 키워드로 오류 메일을 고른다. 판정 기준을 한곳에 둔다."""
    return bool(matched_keyword(mail))


# --------------------------------------------------------------------------
# LLM 판정 — 메일이 오류 알림인가, 인터페이스 ID 는 무엇인가
#
# 키워드는 제목에 그 단어가 있어야만 잡는다. "전송 건 확인 부탁드립니다"처럼
# 오류라는 말 없이 오는 알림, 형식이 제각각인 ID 는 놓친다. LLM 은 그 빈틈을
# 메운다. 다만 LLM 이 규칙을 뒤집지는 않는다:
#   - 키워드가 잡은 메일을 LLM 이 아니라고 하면 빼지 않고 '확인 필요'로 남긴다.
#     누락은 오탐보다 나쁘다.
#   - LLM 이 낸 ID 는 메일에 그대로 적혀 있을 때만 쓴다. 소형 모델은 그럴듯한
#     ID 를 지어내고, 지어낸 키로 DB 를 조회하면 '데이터 없음'이 나와
#     장애가 없는 것처럼 보인다.
#   - LLM 이 실패하면 그 메일은 키워드로 판정하고 그 사실을 남긴다.
# --------------------------------------------------------------------------

# 사람이 회신·전달한 사본. 원본 알림의 중복이라 LLM 에 보내지 않는다 —
# 보내면 "RE: FW: 재고 연계 실패" 를 오류로 읽어 같은 장애가 두 번 잡힌다.
# 사본까지 보려면 MAIL_SUBJECT_STRIP_PREFIXES 를 설정한다(그때는 보낸다).
_REPLY_COPY = re.compile(r"^\s*(RE|FW|FWD|회신|전달|답장)\s*:", re.IGNORECASE)

# ID 로 받아 줄 모양. 공백·한글이 섞이면 문장 조각이지 ID 가 아니다.
_ID_SHAPE = re.compile(r"[A-Za-z0-9][A-Za-z0-9_\-.]{1,59}")


def llm_judge_enabled() -> bool:
    """이번 실행에서 LLM 판정을 쓰는가. USE_LLM=false 면 설정과 무관하게 끈다."""
    return USE_LLM and (IFERR_MAIL_JUDGE or "").strip().lower() == "llm"


def is_reply_copy(subject: str) -> bool:
    return not MAIL_SUBJECT_STRIP_PREFIXES and bool(_REPLY_COPY.match(subject or ""))


def _llm_prompt(mail: Mail) -> str:
    body = (mail.body or "").strip()
    cut = len(body) > IFERR_LLM_BODY_CHARS
    body = body[:IFERR_LLM_BODY_CHARS] + ("\n…(이하 생략)" if cut else "")
    hint = (
        f"이 회사의 인터페이스 ID 는 대개 {', '.join(IFERR_KEY_PREFIXES)} 로 시작한다. "
        if IFERR_KEY_PREFIXES
        else ""
    )
    return (
        "너는 시스템 연계(인터페이스) 장애 알림 메일을 분류한다.\n"
        "1) is_error: 이 메일이 인터페이스·연계·전송·배치 처리가 실패했거나 오류가 났음을 "
        "알리는 메일이면 true. 정상 처리 결과 보고, 일정 안내, 설정 문의, 단순 공지는 false.\n"
        "2) interface_ids: 메일에 적힌 인터페이스 ID 를 적힌 글자 그대로 옮겨라. "
        f"{hint}"
        "테이블 이름, 오류 코드(ORA-01400 등), 건수, 날짜, 사람 이름은 ID 가 아니다. "
        "ID 가 없으면 빈 목록. 메일에 없는 ID 를 만들지 마라.\n"
        "3) cause_columns: 오류 원인으로 적힌 DB 컬럼 이름(예: 길이 초과·NULL 불가가 난 컬럼)을 "
        "적힌 글자 그대로. 테이블이 함께 적혀 있으면 '테이블.컬럼'. 없으면 빈 목록.\n"
        "4) reason: 판단 근거를 한 문장으로.\n\n"
        f"제목: {mail.subject}\n"
        f"본문:\n{body}"
    )


@cached(ttl=3600, maxsize=512, key=lambda mail: (mail.source, mail.subject, JUDGE_MODEL))
def llm_judge_mail(mail: Mail) -> dict[str, Any]:
    """LLM 판정 하나. {is_error, interface_ids, reason, note}. 실패하면 예외.

    같은 메일은 내용이 바뀌지 않으므로 길게 캐시한다. 기간을 바꿔 다시
    물을 때마다 수십 통을 다시 판정하면 답이 몇 분씩 늦어진다.
    """
    from pydantic import BaseModel, Field

    from core.llm import invoke_structured

    class MailVerdict(BaseModel):
        is_error: bool = Field(description="인터페이스 처리 실패·오류를 알리는 메일인가")
        interface_ids: list[str] = Field(description="메일에 적힌 인터페이스 ID, 적힌 그대로")
        cause_columns: list[str] = Field(description="오류 원인으로 적힌 DB 컬럼, 적힌 그대로")
        reason: str = Field(description="판단 근거 한 문장")

    v, note = invoke_structured(MailVerdict, _llm_prompt(mail))
    return {
        "is_error": bool(v.is_error),
        "interface_ids": [str(x) for x in (v.interface_ids or [])][:20],
        "cause_columns": [str(x) for x in (v.cause_columns or [])][:10],
        "reason": str(v.reason or "").strip()[:200],
        "note": note,
    }


def verify_llm_keys(text: str, ids: list[str]) -> tuple[list[dict[str, str]], list[str]]:
    """LLM 이 낸 ID 중 메일에 실제로 있는 것만. (통과 [{key, rule, evidence}], 버린 것)

    식별자 경계로 찾는다 — 본문에 ABCIF0001234_TMP 만 있는데 LLM 이
    ABCIF0001234 를 내면 버린다. 잘린 키로 조회하면 엉뚱한 행을 집는다.
    키는 메일에 적힌 표기를 쓴다(LLM 이 대소문자를 바꿔 내는 경우가 있다).
    """
    kept: list[dict[str, str]] = []
    rejected: list[str] = []
    seen: set[str] = set()
    for raw in ids:
        cand = str(raw).strip().strip("[](){}<>\"'`.,:;")
        if not cand or cand.upper() in seen:
            continue
        m = ident_pattern(cand, re.IGNORECASE).search(text) if _ID_SHAPE.fullmatch(cand) else None
        if not m:
            rejected.append(cand[:60])
            continue
        seen.add(cand.upper())
        start, end = max(0, m.start() - 20), min(len(text), m.end() + 20)
        kept.append({"key": m.group(0), "rule": "llm",
                     "evidence": " ".join(text[start:end].split())})
    return kept, rejected


def judge_mails(
    mails: list[Mail], force_error: bool = False
) -> tuple[list[dict[str, Any]], list[str]]:
    """메일마다 오류 여부와 키를 정한다. 통합 툴과 단계 툴이 같이 쓴다.

    돌려주는 판정 하나:
        mail, is_error, matched(걸린 키워드), via_prefix,
        judged_by   rule | llm | rule+llm
        keys        [{key, rule, evidence}]  — rule 은 정규식 규칙명 또는 llm
        llm         {is_error, reason} | None (LLM 을 안 썼거나 실패)
        conflict    키워드와 LLM 이 다르게 봤다 — 사람이 볼 것

    force_error 는 사용자가 제목으로 집어 '이 메일 오류 확인해줘' 한 경우다. 키워드·LLM 이
    오류가 아니라고 봐도 키와 원인 컬럼을 뽑는다(judged_by=user) — 사람이 오류라고 했다.
    """
    use_llm = llm_judge_enabled()
    warnings: list[str] = []
    out: list[dict[str, Any]] = []
    asked = 0
    skipped_for_cap = 0
    failures: list[str] = []
    dropped: list[str] = []

    targets = [m for m in mails if not is_reply_copy(m.subject)] if use_llm else []
    total = min(len(targets), IFERR_LLM_MAX_MAILS)

    for mail in mails:
        hit, via_prefix = match_info(mail)
        text = f"{mail.subject}\n{mail.body}"
        verdict: dict[str, Any] | None = None

        if use_llm and not is_reply_copy(mail.subject):
            if asked >= IFERR_LLM_MAX_MAILS:
                skipped_for_cap += 1
            else:
                asked += 1
                # 메일 한 통에 수 초가 걸린다. 몇 번째인지 알려야 기다릴 수 있다.
                notify(f"메일 판정 중 {asked}/{total} ({mail.subject[:24]})")
                try:
                    verdict = llm_judge_mail(mail)
                except Exception as e:
                    failures.append(f"{type(e).__name__}: {e}")
                    verdict = None
                if verdict and verdict.get("note") and verdict["note"] not in warnings:
                    warnings.append(verdict["note"])

        llm_error = bool(verdict and verdict["is_error"])
        is_error = bool(hit) or llm_error
        if verdict is None:
            judged_by = "rule"
        elif hit and llm_error:
            judged_by = "rule+llm"
        elif llm_error:
            judged_by = "llm"
        else:
            judged_by = "rule"          # 키워드만 걸렸거나 둘 다 아니다
        conflict = bool(verdict) and bool(hit) and not llm_error
        if force_error and not is_error:
            is_error, judged_by = True, "user"
            warnings.append(f"'{mail.subject[:60]}' 은(는) 제목 키워드"
                            + ("·LLM" if verdict else "") + " 로는 오류 메일이 아니었다 — "
                            "지정한 메일이라 오류로 보고 조사했다")

        keys: list[dict[str, str]] = []
        issues: list[dict[str, str]] = []
        if is_error:
            # 원인 컬럼: Oracle 메시지 규칙이 먼저, 컬럼을 못 뽑았을 때만 LLM 답을 쓴다.
            issues = extract_issues(text)
            if verdict and not any(i["column"] for i in issues):
                issues += verify_llm_columns(text, verdict.get("cause_columns") or [])
            # 오류 메시지가 테이블·컬럼이라고 밝힌 이름은 인터페이스 키가 아니다.
            # 인터페이스 테이블은 IF_ 로 시작하는 일이 흔해 if-prefix 규칙이
            # "ERP"."IF_ORDER_TMP" 를 키로 뽑았다(실측) — 없는 IFID 로 조회하게 된다.
            not_keys = {n.upper() for i in issues for n in (i["schema"], i["table"], i["column"]) if n}
            keys = [k for k in extract_keys(text) if k["key"].upper() not in not_keys]
            if verdict:
                llm_keys, rejected = verify_llm_keys(text, verdict["interface_ids"])
                llm_keys = [k for k in llm_keys if k["key"].upper() not in not_keys]
                dropped += rejected
                have = {k["key"].upper(): k for k in keys}
                for k in llm_keys:
                    if k["key"].upper() in have:
                        # 정규식과 LLM 이 같은 키를 냈다 — 근거가 둘이다.
                        have[k["key"].upper()]["rule"] += "+llm"
                    else:
                        keys.append(k)

        out.append({
            "mail": mail,
            "is_error": is_error,
            "matched": hit,
            "via_prefix": via_prefix,
            "judged_by": judged_by,
            "keys": keys,
            "issues": issues,
            "llm": None if verdict is None else
                   {"is_error": verdict["is_error"], "reason": verdict["reason"]},
            "conflict": conflict,
        })

    if failures:
        warnings.append(
            f"LLM 판정 실패 {len(failures)}통 — 그 메일은 키워드로만 판정했다, "
            f"확인 필요: {failures[0]}"
        )
    if skipped_for_cap:
        warnings.append(
            f"메일이 많아 {skipped_for_cap}통은 키워드로만 판정했다 "
            f"(IFERR_LLM_MAX_MAILS={IFERR_LLM_MAX_MAILS}) — 기간을 좁히면 전부 LLM 이 본다"
        )
    if dropped:
        warnings.append(
            f"LLM 이 낸 ID 중 메일에 없는 것 {len(dropped)}개를 버렸다: "
            f"{', '.join(dropped[:3])}"
        )
    for j in out:
        if j["conflict"]:
            warnings.append(
                f"키워드는 오류로, LLM 은 오류 아님으로 봤다 — 확인 필요: "
                f"{j['mail'].subject[:60]} (LLM: {j['llm']['reason']})"
            )
    return out, warnings


# --------------------------------------------------------------------------
# 노드 1 — 메일 수집
# --------------------------------------------------------------------------


def collect(state: IfErrState) -> IfErrState:
    """메일을 읽는다. 못 읽으면 그 사실을 결과에 남긴다.

    빈 목록을 돌려주고 끝내면 '오류 메일 없음'으로 읽힌다. 메일함에
    접근하지 못한 것과 오류가 없는 것은 완전히 다른 상황이다.
    """
    if state.get("only_key"):
        # 키를 직접 준 경우 메일을 읽지 않는다. 이미 아는 키를 확인만 할 때
        # 사서함을 훑을 이유가 없다.
        return {"mails": [], "mail_count": 0, "warnings": []}

    try:
        mails = read_mails(since_hours=state.get("hours"))
    except MailUnavailable as e:
        return {
            "mails": [],
            "mail_count": 0,
            "decided_by": "fallback",
            "rule": "mail-unavailable",
            "warnings": [f"메일을 읽지 못했다 — 확인 필요: {e}"],
        }
    subject = (state.get("subject") or "").strip()
    if subject:
        picked, warnings = pick_by_subject(mails, subject)
        if not picked:
            # '그 메일에 오류 없음' 이 아니다. 메일을 못 찾은 것이다.
            return {
                "mails": [], "mail_count": 0, "decided_by": "rule", "rule": "subject-not-found",
                "warnings": [f"제목에 '{subject}' 이(가) 들어간 메일을 최근 {state.get('hours')}시간 "
                             f"메일 {len(mails)}통에서 찾지 못했다 — 확인 필요 "
                             "(제목 철자나 기간을 확인할 것)"],
            }
        return {"mails": picked, "mail_count": len(picked), "warnings": warnings}
    return {"mails": mails, "mail_count": len(mails), "warnings": []}


# --------------------------------------------------------------------------
# 노드 2 — 키 추출
# --------------------------------------------------------------------------


def find_keys(state: IfErrState) -> IfErrState:
    """오류 메일에서 인터페이스 키를 모은다. 키 하나당 case 하나."""
    if state.get("rule") == "mail-unavailable":
        return {}

    if state.get("only_key"):
        return {
            "cases": [
                {
                    "key": state["only_key"],
                    "found_by": "argument",
                    "evidence": "사용자가 직접 지정",
                    "mails": [],
                    "issues": [],
                }
            ]
        }

    cases: dict[str, dict[str, Any]] = {}
    unmatched: list[str] = []

    # 오류 판정과 키 추출은 judge_mails 한 곳에서 한다(키워드 또는 LLM).
    # 제목과 본문 양쪽에서 찾는다. 본문 표에 키가 있는 형식이 흔하다.
    judged, judge_warnings = judge_mails(state.get("mails") or [],
                                         force_error=bool(state.get("subject")))
    for j in judged:
        if not j["is_error"]:
            continue
        mail = j["mail"]
        if not j["keys"]:
            # 오류 메일인데 키를 못 뽑았다 — 조용히 버리면 장애를 놓친다.
            unmatched.append(mail.subject[:80])
            continue
        for h in j["keys"]:
            case = cases.setdefault(
                h["key"].upper(),
                {
                    "key": h["key"],
                    "found_by": h["rule"],
                    "evidence": h["evidence"],
                    # 이 메일을 무엇이 오류로 봤는가 — 키를 무엇이 뽑았는가와 별개다.
                    "judged_by": j["judged_by"],
                    "mails": [],
                    "issues": [],
                },
            )
            # 원인 컬럼은 키에 붙인다. 같은 키의 메일이 여러 통이어도 (테이블, 컬럼) 은 한 번.
            have = {(i["table"].upper(), i["column"].upper(), i["kind"]) for i in case["issues"]}
            for i in j["issues"]:
                if (i["table"].upper(), i["column"].upper(), i["kind"]) not in have:
                    case["issues"].append(i)
                    have.add((i["table"].upper(), i["column"].upper(), i["kind"]))
            case["mails"].append(
                {
                    "subject": mail.subject,
                    "received": mail.received.isoformat() if mail.received else "",
                    # 발신자 주소를 그대로 남기지 않는다.
                    "sender": mail.sender_masked,
                    "judged_by": j["judged_by"],
                    "llm_reason": (j["llm"] or {}).get("reason", ""),
                }
            )

    warnings = list(state.get("warnings") or []) + judge_warnings
    if unmatched:
        warnings.append(
            f"키를 뽑지 못한 오류 메일 {len(unmatched)}건 — 확인 필요 "
            f"(예: {unmatched[0]})"
        )
    return {"cases": list(cases.values()), "warnings": warnings}


# --------------------------------------------------------------------------
# 노드 3 — DB 조회
# --------------------------------------------------------------------------


@cached(ttl=60, maxsize=128, key=lambda name, sql, key: (name, key))
def _query_cached(name: str, sql: str, key: str) -> list[dict[str, Any]]:
    """같은 키를 여러 메일이 물고 오는 경우가 흔해 짧게 캐시한다.

    캐시 키에서 sql 본문을 빼는 이유: 비싼 것은 DB 왕복이고, sql 은
    설정에서 오는 고정 문자열이라 (name, key) 로 충분하다. TTL 을 60초로
    짧게 둔 것은 장애 대응 중에 상태가 바뀌기 때문이다 — 오래된 값을
    보여 주면 '이미 재처리됐는데 실패로 보이는' 사고가 난다.
    """
    rows = oracle.query(sql, {"if_key": key})
    return rows[:IFERR_MAX_ROWS]


_MASTER_LOGICAL = ("id", "src_sys", "tar_sys", "src_table", "tar_table", "sch_day", "sch_h", "sch_m")


def _master_col(name: str) -> str:
    """논리 이름(id, tar_table …) → 조회 결과에서 찾을 컬럼 이름(대문자).

    설정한 컬럼 이름이 있으면 그것, 없으면 논리 이름 그대로 — 파일 마스터는
    컬럼 설정 없이도 논리 이름으로 쓸 수 있다.
    """
    return str(IFERR_MASTER_FIELDS.get(name) or name).upper()


def master_file_rows() -> list[dict[str, Any]]:
    """IFERR_MASTER_FILE(JSON 배열)의 행. 키는 _master_col 로 바꿔 DB 행과 같은 모양으로.

    파일을 못 읽으면 예외 — 조용히 빈 목록을 주면 '마스터에 없음'으로 읽힌다.
    """
    import json

    data = json.loads(Path(IFERR_MASTER_FILE).read_text(encoding="utf-8"))
    if not isinstance(data, list):
        raise ValueError("마스터 파일은 행(객체)의 배열이어야 한다")
    return [{_master_col(str(k)) if str(k) in _MASTER_LOGICAL else str(k).upper(): v
             for k, v in row.items()} for row in data if isinstance(row, dict)]


def _lookup_file(cases: list[dict[str, Any]], warnings: list[str]) -> IfErrState:
    """마스터를 파일에서 찾는다(데모·오프라인). DB 는 보지 않는다."""
    try:
        rows = master_file_rows()
    except Exception as e:
        for c in cases:
            c["db"] = {"status": "unknown", "rule": "master-file-failed", "rows": {},
                       "source": "file"}
        warnings.append(f"마스터 파일을 읽지 못했다 — 확인 필요: {type(e).__name__}: {e}")
        return {"cases": cases, "warnings": warnings}
    id_col = _master_col("id")
    for c in cases:
        hit = [r for r in rows if str(r.get(id_col, "")).upper() == c["key"].upper()]
        c["db"] = ({"status": "found", "rule": "rows-found", "rows": {"header": hit},
                    "source": "file"} if hit else
                   {"status": "missing", "rule": "no-rows", "rows": {"header": []},
                    "source": "file"})
    return {"cases": cases, "warnings": warnings}


def lookup(state: IfErrState) -> IfErrState:
    """키별로 설정된 SQL 을 돌린다.

    SQL 이 설정되지 않았거나 DB 설정이 없으면 '확인 불가'로 남긴다.
    '영향 없음'으로 처리하지 않는다. IFERR_MASTER_FILE 이 있으면 파일만 본다.
    """
    cases = state.get("cases") or []
    if not cases:
        return {}

    if IFERR_MASTER_FILE.strip():
        return _lookup_file(cases, list(state.get("warnings") or []))

    configured = {k: v for k, v in IFERR_SQL.items() if v.strip()}
    warnings = list(state.get("warnings") or [])

    if not configured:
        for c in cases:
            c["db"] = {"status": "unknown", "rule": "sql-not-configured", "rows": {}}
        warnings.append(
            "조회 SQL 이 설정되지 않았다 — 확인 필요 "
            "(config_local.py 에 IFERR_MASTER_TABLE 과 IFERR_MASTER_FIELDS 를 "
            "적으면 SQL 이 자동으로 만들어진다)"
        )
        return {"cases": cases, "warnings": warnings}

    if not oracle.is_configured():
        for c in cases:
            c["db"] = {"status": "unknown", "rule": "db-not-configured", "rows": {}}
        warnings.append("Oracle 설정이 비어 있다 — 확인 필요 (config.ORACLE_DSN)")
        return {"cases": cases, "warnings": warnings}

    for c in cases:
        rows: dict[str, list[dict[str, Any]]] = {}
        errors: list[str] = []
        for name, sql in configured.items():
            try:
                rows[name] = _query_cached(name, sql, c["key"])
            except Exception as e:
                # DB 오류를 조용히 넘기면 '데이터 없음'과 구분되지 않는다.
                errors.append(f"{name}: {type(e).__name__}: {e}")
        if errors:
            c["db"] = {"status": "unknown", "rule": "query-failed",
                       "rows": rows, "errors": errors}
            warnings.append(f"[{c['key']}] 조회 실패 — 확인 필요: {errors[0]}")
        elif any(rows.values()):
            c["db"] = {"status": "found", "rule": "rows-found", "rows": rows}
        else:
            # 진짜로 행이 없는 경우. 이건 '없다'가 맞다.
            c["db"] = {"status": "missing", "rule": "no-rows", "rows": rows}
    return {"cases": cases, "warnings": warnings}


# --------------------------------------------------------------------------
# 노드 4 — 영향 판정
# --------------------------------------------------------------------------


def assess(state: IfErrState) -> IfErrState:
    """조회 결과로 영향을 정리한다. 규칙 기반이며 근거를 함께 남긴다.

    스키마를 모르는 상태에서도 동작해야 하므로, 상태 컬럼 후보
    (config.IFERR_STATUS_COLUMNS)가 있으면 값별로 집계하고 없으면
    행 수만 센다. 스키마가 확정되면 여기를 구체화한다.
    """
    for c in state.get("cases") or []:
        db = c.get("db") or {}
        rows: dict[str, list[dict[str, Any]]] = db.get("rows") or {}

        if db.get("status") == "unknown":
            c["impact"] = "확인 불가"
            c["decided_by"] = "fallback"
            c["rule"] = db.get("rule", "unknown")
            continue

        if db.get("status") == "missing":
            c["impact"] = "해당 키의 데이터가 테이블에 없다"
            c["decided_by"] = "rule"
            c["rule"] = "no-rows"
            continue

        counts = {name: len(r) for name, r in rows.items() if r}
        status_summary = _summarize_status(rows)

        # 마스터에서 '무엇이 어디로 가는 인터페이스인가'를 먼저 뽑는다.
        # 이게 곧 영향 범위다 — 실패하면 타겟 테이블에 데이터가 안 들어간다.
        flows = _master_summary(rows.get("header") or [])
        parts: list[str] = []
        if flows:
            parts.append(" / ".join(flow_text(f) for f in flows[:3]))
            if len(flows) > 3:
                parts.append(f"외 {len(flows) - 3}건")
        # header 는 스케줄 때문에 행이 여러 개로 나온다. 경로를 이미 보여
        # 줬으면 행 수는 중복이라 빼고, 매핑이 없어 경로를 못 만든 경우에만
        # 원 행 수를 보여 준다.
        parts += [
            f"{name} {n}건"
            for name, n in counts.items()
            if not (name == "header" and flows)
        ]
        if status_summary:
            parts.append("상태 " + ", ".join(f"{k}={v}" for k, v in status_summary.items()))

        c["impact"] = " / ".join(parts)
        c["decided_by"] = "rule"
        c["rule"] = "rows-found"
        c["counts"] = counts
        c["status_summary"] = status_summary
        c["flows"] = flows
    return {"cases": state.get("cases") or []}


def _fmt_time(hour: str, minute: str) -> str:
    """'8', '5' → '08:05'. 숫자가 아니면 원문을 그대로 둔다.

    사이트에 따라 시/분에 '*' 같은 값이 들어가기도 한다. 억지로 숫자로
    바꾸다 예외를 내는 것보다 원문을 보여 주는 편이 낫다.
    """
    h, m = (hour or "").strip(), (minute or "").strip()
    if h.isdigit() and m.isdigit():
        return f"{int(h):02d}:{int(m):02d}"
    return ":".join(x for x in (h, m) if x) or ""


def _schedule_text(entries: list[tuple[str, str, str]]) -> str:
    """(일, 시, 분) 목록을 사람이 읽는 한 줄로 만든다.

        [("*","8","30"), ("*","12","0")]  → "매일 08:30, 12:00"
        [("5","3","15")]                  → "5일 03:15"
        섞이면                              → "매일 08:30 / 5일 03:15"

    같은 인터페이스가 하루 여러 번 도는 경우가 흔해, 행을 그대로 나열하면
    화면과 LLM 컨텍스트가 금방 지저분해진다. 일자로 묶어 시각만 나열한다.
    """
    by_day: dict[str, list[str]] = {}
    for day, hour, minute in entries:
        day = (day or "").strip()
        label = "매일" if day in ("*", "") else f"{day}일"
        t = _fmt_time(hour, minute)
        times = by_day.setdefault(label, [])
        if t and t not in times:      # 같은 시각이 중복으로 들어오기도 한다
            times.append(t)

    parts: list[str] = []
    # '매일'을 먼저, 그 다음 일자 순. 보는 사람이 기대하는 순서다.
    for label in sorted(by_day, key=lambda x: (x != "매일", x)):
        times = sorted(by_day[label])
        parts.append(f"{label} {', '.join(times)}" if times else label)
    return " / ".join(parts)


def _master_summary(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """마스터 행에서 '무엇이 어디로 가는가'와 스케줄을 뽑는다.

    같은 IFID 가 스케줄 때문에 여러 행으로 나오므로 IFID 로 묶는다.
    묶지 않으면 같은 인터페이스가 여러 건으로 보여 '몇 개가 깨진 거지'를
    셀 수 없다.

    컬럼 이름은 사이트마다 달라 config.IFERR_MASTER_FIELDS 로 매핑한다.
    조회 결과의 키는 대문자로 오므로 대소문자를 맞춰 찾는다.
    """
    groups: dict[str, dict[str, Any]] = {}
    order: list[str] = []

    for row in rows:
        upper = {str(k).upper(): v for k, v in row.items()}
        item = {name: str(upper.get(_master_col(name), "") or "") for name in _MASTER_LOGICAL}
        if not any(item.values()):
            continue

        key = item.get("id") or f"__row{len(order)}"
        if key not in groups:
            groups[key] = {
                "id": item.get("id", ""),
                "src_sys": item.get("src_sys", ""),
                "tar_sys": item.get("tar_sys", ""),
                "src_table": item.get("src_table", ""),
                "tar_table": item.get("tar_table", ""),
                "rows": 0,
                "_sched": [],
            }
            order.append(key)

        g = groups[key]
        g["rows"] += 1
        sched = (item.get("sch_day", ""), item.get("sch_h", ""), item.get("sch_m", ""))
        if any(x.strip() for x in sched):
            g["_sched"].append(sched)

    out: list[dict[str, Any]] = []
    for key in order:
        g = groups[key]
        g["schedule"] = _schedule_text(g.pop("_sched"))
        out.append(g)
    return out


def system_table(system: str, table: str) -> str:
    """'[ERP] TB_ORDER'. 시스템은 괄호로, 테이블 이름에 점으로 붙이지 않는다.

    'ERP.TB_ORDER' 로 보이면 모델이 그 문자열을 테이블 이름으로 옮겨 적어
    영향도 조사가 'ERP.EMP.TB_ORDER' 를 찾았다(마스터 값에 스키마가 붙어 있으면
    더 길어진다). 시스템은 테이블 이름의 일부가 아니다.
    """
    system, table = (system or "").strip(), (table or "").strip()
    if system and table:
        return f"[{system}] {table}"
    return table or (f"[{system}]" if system else "")


def flow_text(m: dict[str, Any]) -> str:
    """'출발 → 도착 (스케줄)'을 한 줄로. 영향 범위를 바로 읽을 수 있게."""
    src = system_table(m.get("src_sys", ""), m.get("src_table", ""))
    tar = system_table(m.get("tar_sys", ""), m.get("tar_table", ""))
    flow = f"{src} → {tar}" if src and tar else (src or tar or "경로 정보 없음")
    sched = m.get("schedule")
    return f"{flow} ({sched})" if sched else flow


def _summarize_status(rows: dict[str, list[dict[str, Any]]]) -> dict[str, int]:
    """상태 컬럼 후보를 찾아 값별로 센다. 없으면 빈 dict."""
    wanted = {c.strip().upper() for c in IFERR_STATUS_COLUMNS if c.strip()}
    summary: dict[str, int] = {}
    for row_list in rows.values():
        for row in row_list:
            for col, val in row.items():
                if col.upper() in wanted and val is not None:
                    summary[str(val)] = summary.get(str(val), 0) + 1
    return summary


# --------------------------------------------------------------------------
# 노드 5 — LLM 요약 (선택)
# --------------------------------------------------------------------------


def explain(state: IfErrState) -> IfErrState:
    """USE_LLM 이 False 면 아무것도 하지 않는다.

    판정은 규칙이 끝냈다. LLM 은 사람이 읽기 좋게 묶어 주기만 한다.
    판정을 LLM 에 맡기면 같은 상황에 다른 답이 나오고 근거도 남지 않는다.
    """
    cases = state.get("cases") or []
    if not USE_LLM or not cases:
        return {}

    warnings = list(state.get("warnings") or [])
    try:
        from core.llm import get_llm

        lines = "\n".join(
            f"- {c['key']}: {c.get('impact','')} (메일 {len(c.get('mails') or [])}건)"
            for c in cases[:10]
        )
        prompt = (
            "아래는 인터페이스 오류 확인 결과다. 무엇이 문제이고 무엇을 먼저 "
            "봐야 하는지 두 문장 이내로 요약해라. 목록에 없는 내용은 말하지 마라. "
            "'확인 불가'는 '문제 없음'이 아니다.\n" + lines
        )
        return {"comment": str(get_llm().invoke(prompt).content).strip(),
                "warnings": warnings}
    except Exception as e:
        warnings.append(f"LLM 호출 실패 — 확인 필요: {type(e).__name__}: {e}")
        return {"comment": "", "warnings": warnings}


@cached(ttl=3600, maxsize=1, key=lambda: "graph")
def _graph():
    g = StateGraph(IfErrState)
    for name, fn in (
        ("collect", collect), ("find_keys", find_keys),
        ("lookup", lookup), ("assess", assess), ("explain", explain),
    ):
        g.add_node(name, fn)
    g.add_edge(START, "collect")
    g.add_edge("collect", "find_keys")
    g.add_edge("find_keys", "lookup")
    g.add_edge("lookup", "assess")
    g.add_edge("assess", "explain")
    g.add_edge("explain", END)
    return g.compile()


def run_iferr(
    hours: int | None = None, key: str = "", detail: Detail = "full", subject: str = ""
) -> dict[str, Any]:
    """진입점.

    subject 를 주면 제목에 그 문구가 들어간 메일 한 통(가장 최근)만 본다. 기간을 안 주면
    MAIL_SUBJECT_LOOKBACK_HOURS 만큼 거슬러 찾는다. key 를 같이 주면 그 메일의 키 중
    그 키만 남긴다 — 메일을 건너뛰지 않는다(원인 컬럼은 메일에 있다).

        full    : 조회된 행까지 전부 (화면용, MCP 리소스로 나간다)
        summary : 키별 한 줄 (LLM 컨텍스트에 들어간다)
        minimal : 키 목록과 상태만

    full 을 챗봇 툴에서 돌려주면 조회 행이 매 턴 컨텍스트를 먹는다.
    """
    subject = (subject or "").strip()
    if subject and hours is None:
        hours = MAIL_SUBJECT_LOOKBACK_HOURS
    state: IfErrState = _graph().invoke(
        {"hours": hours, "only_key": "" if subject else key.strip(), "subject": subject}
    )
    cases = state.get("cases") or []
    warnings = list(state.get("warnings") or [])
    if subject and key.strip() and cases:
        mine = [c for c in cases if c["key"].upper() == key.strip().upper()]
        if mine:
            cases = mine
        else:
            warnings.append(f"지정한 메일에 키 {key.strip()} 이(가) 없어 메일의 키 전부를 보였다 "
                            f"({', '.join(c['key'] for c in cases)})")

    if detail == "minimal":
        return {
            "mail_count": state.get("mail_count", 0),
            "cases": [
                {"key": c["key"], "status": (c.get("db") or {}).get("status", "unknown")}
                for c in cases
            ],
            "warnings": warnings,
        }

    if detail == "summary":
        return {
            "mail_count": state.get("mail_count", 0),
            "case_count": len(cases),
            "cases": [
                {
                    "key": c["key"],
                    "status": (c.get("db") or {}).get("status", "unknown"),
                    "impact": c.get("impact", ""),
                    "rule": c.get("rule", ""),
                    # 메일을 무엇이 오류로 봤고 키를 무엇이 뽑았는가. 오판을 되짚는 근거다.
                    "judged_by": c.get("judged_by", ""),
                    "found_by": c.get("found_by", ""),
                    "flows": c.get("flows") or [],
                    "issues": c.get("issues") or [],
                    "mail_count": len(c.get("mails") or []),
                }
                for c in cases[:10]
            ],
            "more_cases": max(0, len(cases) - 10),
            "comment": state.get("comment") or "",
            "warnings": warnings,
        }

    return {
        "mail_count": state.get("mail_count", 0),
        "case_count": len(cases),
        "cases": cases,
        "comment": state.get("comment") or "",
        "warnings": warnings,
    }


# --------------------------------------------------------------------------
# 단계별 진입점 — MCP 에서 기능별 툴로 노출한다
#
# run_iferr() 가 전체를 한 번에 도는 통합 경로이고, 아래 둘은 그 중간
# 단계만 따로 부르는 경로다. 같은 함수를 재사용하므로 통합 실행과
# 단계별 실행의 결과가 어긋나지 않는다.
# --------------------------------------------------------------------------


def list_mails(hours: int | None = None, detail: Detail = "full") -> dict[str, Any]:
    """메일만 읽는다. DB 는 건드리지 않는다.

    '메일이 오긴 왔는지' 부터 확인할 때 쓴다. 발신자는 마스킹된다.
    """
    state = collect({"hours": hours})
    if state.get("rule") == "mail-unavailable":
        # 실패 경로도 성공 경로와 '같은 모양'이어야 한다.
        # 키를 빠뜨리면 호출부가 KeyError 로 죽고, 그 에러가 진짜 원인
        # (Outlook 연결 실패·폴더 못 찾음)을 가려 버린다. 실제로 겪었다.
        return {
            "mail_count": 0,
            "error_count": 0,
            "mails": [],
            "warnings": state.get("warnings") or [],
        }

    rows: list[dict[str, Any]] = []
    judged, judge_warnings = judge_mails(state.get("mails") or [])
    for j in judged:
        m = j["mail"]
        rows.append(
            {
                "subject": m.subject,
                "received": m.received.isoformat() if m.received else "",
                "sender": m.sender_masked,
                "is_error": j["is_error"],
                # 어떤 키워드에 걸렸는지 남긴다. 오탐 원인을 여기서 찾는다.
                "matched": j["matched"],
                # 머리말(RE:/FW:)을 떼고서야 걸린 사본인가
                "via_prefix": j["via_prefix"],
                # 무엇이 오류로 봤는가(rule | llm | rule+llm)와 LLM 의 근거
                "judged_by": j["judged_by"],
                "llm_reason": (j["llm"] or {}).get("reason", ""),
                "conflict": j["conflict"],
                "keys": [k["key"] for k in j["keys"]],
            }
        )

    if detail != "full":
        # 본문은 애초에 담지 않았지만, 요약에서는 제목도 잘라 컨텍스트를 아낀다.
        rows = [{**r, "subject": r["subject"][:60]} for r in rows[:20]]

    return {
        "mail_count": len(state.get("mails") or []),
        "error_count": sum(1 for r in rows if r["is_error"]),
        "mails": rows,
        "warnings": (state.get("warnings") or []) + judge_warnings,
    }


def lookup_key(key: str, detail: Detail = "full") -> dict[str, Any]:
    """메일을 읽지 않고 키 하나만 DB 에서 확인한다.

    이미 키를 아는 경우(사람이 메일을 보고 왔거나, 다른 툴이 뽑아 준 경우)
    사서함을 훑을 이유가 없다.
    """
    return run_iferr(key=key, detail=detail)


def interface_tables(key: str) -> dict[str, Any]:
    """IFID 하나가 참조하는 테이블 — 마스터의 출발·도착 테이블. 메일은 읽지 않는다.

    "이 인터페이스가 어떤 테이블을 쓰나"만 물을 때 쓴다. 오류 확인과 달리
    요약용 LLM 을 부르지 않는다 — 마스터 한 번 조회로 끝나는 질문에 수십 초를
    쓸 이유가 없다. 조회·판정은 통합 경로와 같은 노드(find_keys → lookup →
    assess)를 그대로 써서 두 툴의 답이 어긋나지 않는다.

    돌려주는 것:
        status   found | missing | unknown  — '없다'와 '확인 못 했다'를 구분한다
        tables   [{table, role(source|target), system, interface}]  중복 제거
        flows    마스터 경로(출발 → 도착, 스케줄)
    """
    key = (key or "").strip()
    if not key:
        return {"key": "", "status": "unknown", "rule": "no-key", "tables": [],
                "flows": [], "warnings": ["IFID 가 비어 있다"]}

    state: IfErrState = {"only_key": key}
    for node in (find_keys, lookup, assess):
        state = {**state, **node(state)}
    case = (state.get("cases") or [{}])[0]
    status = (case.get("db") or {}).get("status", "unknown")
    flows = case.get("flows") or []

    tables: list[dict[str, str]] = []
    seen: set[tuple[str, str]] = set()
    for f in flows:
        for role, sys_key, table_key in (
            ("source", "src_sys", "src_table"), ("target", "tar_sys", "tar_table"),
        ):
            table = (f.get(table_key) or "").strip()
            if not table or (role, table.upper()) in seen:
                continue
            seen.add((role, table.upper()))
            tables.append({"table": table, "role": role,
                           "system": f.get(sys_key) or "", "interface": f.get("id") or key})

    warnings = list(state.get("warnings") or [])
    if status == "found" and not tables:
        # 행은 있는데 테이블을 못 뽑았다 — '테이블 없음'으로 보이면 안 된다.
        warnings.append(
            "마스터에 행은 있지만 테이블 이름을 찾지 못했다 — 확인 필요 "
            "(IFERR_MASTER_FIELDS 의 src_table / tar_table 매핑)"
        )
    return {"key": key, "status": status, "rule": case.get("rule", ""),
            "tables": tables, "flows": flows, "warnings": warnings}


# 오라클 테이블 이름. 바인드로 넘기므로 안전 검사가 아니라 '테이블 이름으로 볼 수
# 있는가' 검사다 — 문장째 들어온 것을 조회해 '없음'으로 답하지 않게.
_TABLE_NAME = re.compile(r"^[A-Z][A-Z0-9_$#]{0,127}$")


def clean_table_name(raw: str) -> tuple[str, str]:
    """사용자·모델이 준 테이블 이름 → (대문자 이름, 떼어 낸 앞부분).

    '[ERP] TB_ORDER'(화면 표기), 'EMP.TB_ORDER'(스키마), 'tb_order 테이블' 처럼
    들어온다. 시스템 표기와 뒤에 붙은 말을 떼고 스키마는 bare_table_name 으로 뗀다.
    """
    s = re.sub(r"^\s*\[[^\]]*\]\s*", "", raw or "")
    s = (s.split() or [""])[0]
    name, qualifier = bare_table_name(s)
    return name.upper(), qualifier


def table_interfaces(table: str) -> dict[str, Any]:
    """테이블 하나를 출발·도착으로 쓰는 인터페이스 — 마스터를 거꾸로 찾는다.

    find_interface_tables(IFID → 테이블)의 반대 방향이다. 메일·LLM 은 부르지 않는다.
    마스터 행을 IFID 로 묶는 것은 정방향과 같은 _master_summary 를 쓴다.

    돌려주는 것:
        status      found | missing | unknown  — '없다'와 '확인 못 했다'를 구분한다
        interfaces  [{id, roles[source|target], src_sys, src_table, tar_sys,
                      tar_table, schedule}]  도착(데이터가 들어오는 쪽)이 먼저
    """
    name, qualifier = clean_table_name(table)
    out: dict[str, Any] = {"table": name, "qualifier": qualifier, "status": "unknown",
                           "rule": "", "decided_by": "fallback", "interfaces": [],
                           "warnings": []}
    if not name:
        return {**out, "rule": "no-table", "warnings": ["테이블 이름이 비어 있다"]}
    if not _TABLE_NAME.match(name):
        return {**out, "rule": "bad-name",
                "warnings": [f"'{name}' 은(는) 테이블 이름으로 볼 수 없다 — 확인 필요"]}
    if IFERR_MASTER_FILE.strip():
        # 데모·오프라인 — 파일 마스터를 거꾸로 거른다. 이름 비교는 아래 공통 부분이 한다.
        try:
            src, tar = _master_col("src_table"), _master_col("tar_table")
            rows = [r for r in master_file_rows()
                    if name in (bare_table_name(str(r.get(src) or ""))[0].upper(),
                                bare_table_name(str(r.get(tar) or ""))[0].upper())]
        except Exception as e:
            return {**out, "rule": "master-file-failed",
                    "warnings": [f"마스터 파일을 읽지 못했다 — 확인 필요: {type(e).__name__}: {e}"]}
    elif not IFERR_TABLE_SQL.strip():
        return {**out, "rule": "sql-not-configured", "warnings": [
            "테이블로 인터페이스를 찾는 SQL 이 없다 — 확인 필요 (config_local.py 의 "
            "IFERR_MASTER_TABLE 과 IFERR_MASTER_FIELDS 의 src_table/tar_table, "
            "또는 IFERR_TABLE_SQL)"]}
    elif not oracle.is_configured():
        return {**out, "rule": "db-not-configured",
                "warnings": ["Oracle 설정이 비어 있다 — 확인 필요 (config.ORACLE_DSN)"]}
    else:
        try:
            rows = oracle.query(IFERR_TABLE_SQL, {"tname": name, "dotname": "." + name})
        except Exception as e:
            # 조회 실패를 빈 결과로 넘기면 '쓰는 인터페이스 없음'으로 읽힌다.
            return {**out, "rule": "query-failed",
                    "warnings": [f"마스터 조회 실패 — 확인 필요: {type(e).__name__}: {e}"]}

    warnings: list[str] = []
    if len(rows) > IFERR_MAX_ROWS:
        warnings.append(f"마스터 행이 {IFERR_MAX_ROWS}개를 넘어 앞부분만 봤다 — 더 있을 수 있다")
        rows = rows[:IFERR_MAX_ROWS]

    interfaces: list[dict[str, Any]] = []
    unmatched = 0
    for f in _master_summary(rows):
        # SQL 이 이미 걸렀지만 직접 쓴 SQL 은 넓게 잡을 수 있다. 어느 쪽인지 다시 본다.
        roles = [role for role, col in (("target", "tar_table"), ("source", "src_table"))
                 if bare_table_name(f.get(col) or "")[0].upper() == name]
        if not roles:
            unmatched += 1
            continue
        interfaces.append({"id": f["id"], "roles": roles, "src_sys": f["src_sys"],
                           "src_table": f["src_table"], "tar_sys": f["tar_sys"],
                           "tar_table": f["tar_table"], "schedule": f.get("schedule", "")})
    if unmatched:
        warnings.append(f"조회된 인터페이스 {unmatched}건은 출발·도착 테이블이 "
                        f"{name} 과(와) 달라 뺐다 — IFERR_TABLE_SQL 확인")
    # 도착(이 테이블에 데이터를 넣는 쪽)이 먼저다 — 테이블이 비었을 때 볼 곳이다.
    interfaces.sort(key=lambda i: ("target" not in i["roles"], i["id"]))

    if interfaces:
        return {**out, "status": "found", "rule": "rows-found", "decided_by": "rule",
                "interfaces": interfaces, "warnings": warnings}
    if unmatched:
        # 행은 왔는데 이름이 안 맞았다 — '없다'고 단정할 수 없다.
        return {**out, "rule": "name-mismatch", "warnings": warnings}
    # 마스터에 이 테이블을 쓰는 행이 정말 없다. 이건 '없다'가 맞다.
    return {**out, "status": "missing", "rule": "no-rows", "decided_by": "rule",
            "warnings": warnings}


def doctor(hours: int | None = None) -> list[tuple[str, bool, str]]:
    """설정부터 DB 조회까지 순서대로 점검한다. [(단계, 성공, 메시지)]

    단계마다 따로 실행하면 어디까지 됐는지 매번 다시 확인해야 한다.
    한 번에 훑고, 실패한 단계에서 무엇을 해야 하는지 알려 준다.
    앞 단계가 실패해도 뒤 단계를 건너뛰지 않는다 — 문제가 두 개일 수 있다.
    """
    steps: list[tuple[str, bool, str]] = []

    # 1. 제목 판정 설정
    problems = check_subject_rule()
    steps.append(
        (
            "제목 판정 설정",
            not problems,
            f"[{MAIL_SUBJECT_MATCH}] {', '.join(MAIL_SUBJECT_KEYWORDS) or '(없음)'}"
            + ("" if not problems else " — " + "; ".join(problems)),
        )
    )

    # 1-2. LLM 판정을 쓰면 모델이 살아 있는지. 죽어 있으면 키워드로만 돌아간다
    #      — 동작은 하지만 LLM 이 메우던 빈틈이 다시 생긴다.
    if llm_judge_enabled():
        from core.llm import check_ollama

        ok, msg = check_ollama(JUDGE_MODEL)
        steps.append(("LLM 메일 판정", ok, msg + ("" if ok else "  → 키워드로만 판정된다")))

    # 2. 메일 읽기
    mails = list_mails(hours=hours)
    mail_ok = not (not mails["mails"] and mails["warnings"])
    steps.append(
        (
            "메일 읽기",
            mail_ok,
            f"{mails['mail_count']}통 중 오류 {mails['error_count']}통"
            if mail_ok
            else "; ".join(mails["warnings"]) + "  → --folders 로 폴더 확인",
        )
    )

    # 3. 키 추출
    keys = [k for m in mails["mails"] for k in m["keys"]]
    unmatched = [m for m in mails["mails"] if m["is_error"] and not m["keys"]]
    steps.append(
        (
            "키 추출",
            bool(keys) or not mails["mails"],
            (f"{len(set(keys))}개: {', '.join(sorted(set(keys))[:5])}" if keys else "")
            + (
                f"  (키 못 뽑은 오류 메일 {len(unmatched)}통"
                f" → --dump 2 로 본문 확인)"
                if unmatched
                else ""
            )
            + ("" if keys or not mails["mails"] else "키 접두어 설정 확인 "
               "(IFERR_KEY_PREFIXES)"),
        )
    )

    # 4. DB 접속
    from core.oracle import check_connection

    db_ok, db_msg = check_connection()
    steps.append(("DB 접속", db_ok, db_msg))

    # 5. 마스터 조회 — 실제 키로 한 건 돌려 본다
    if not [v for v in IFERR_SQL.values() if v.strip()]:
        steps.append(
            ("마스터 조회", False, "IFERR_MASTER_TABLE / IFERR_MASTER_FIELDS 미설정")
        )
    elif not db_ok:
        steps.append(("마스터 조회", False, "DB 접속이 안 되어 확인 불가"))
    else:
        probe = sorted(set(keys))[0] if keys else ""
        if not probe:
            steps.append(("마스터 조회", False, "확인할 키가 없다(메일에서 못 뽑음)"))
        else:
            case = lookup_key(probe, detail="summary")["cases"][0]
            steps.append(
                (
                    "마스터 조회",
                    case["status"] == "found",
                    f"{probe}: {case['status']} — {case['impact']}",
                )
            )
    return steps


if __name__ == "__main__":
    import argparse
    import json

    ap = argparse.ArgumentParser(description="인터페이스 오류 메일 확인")
    ap.add_argument("--hours", type=int, default=None, help="몇 시간 전까지 볼지")
    ap.add_argument("--key", default="", help="메일을 읽지 않고 이 키만 확인")
    ap.add_argument("--subject", default="", metavar="TEXT",
                    help="제목에 이 문구가 들어간 메일 한 통(가장 최근)만 확인")
    ap.add_argument("--tables", default="", metavar="IFID",
                    help="이 IFID 가 참조하는 테이블(출발·도착)만 본다. 메일을 읽지 않는다")
    ap.add_argument("--ifs", default="", metavar="TABLE",
                    help="이 테이블을 출발·도착으로 쓰는 인터페이스. 메일을 읽지 않는다")
    ap.add_argument("--detail", choices=["full", "summary", "minimal"], default="full")
    # 아래 셋은 진단용이다. DB 를 건드리지 않는다.
    ap.add_argument("--folders", action="store_true",
                    help="Outlook 폴더 목록 (MAIL_FOLDER 에 넣을 값 찾기)")
    ap.add_argument("--mails", action="store_true",
                    help="메일 목록만 본다. DB 조회를 하지 않는다")
    ap.add_argument("--dump", type=int, metavar="N", default=0,
                    help="최근 오류 메일 N통의 본문을 out/ 에 저장(정규식 확인용)")
    ap.add_argument("--test-key", default="", metavar="TEXT",
                    help="붙여넣은 제목/본문에서 키가 뽑히는지 바로 확인")
    ap.add_argument("--doctor", action="store_true",
                    help="설정부터 DB 조회까지 한 번에 점검")
    ap.add_argument("--check-db", action="store_true", help="Oracle 접속 확인")
    ap.add_argument("--find-column", default="", metavar="NAME",
                    help="이름에 이 조각이 들어간 테이블·컬럼 찾기 (예: EAI)")
    ap.add_argument("--find-key", default="", metavar="VALUE",
                    help="이 값이 들어 있는 테이블·컬럼 찾기 (예: EAIIF0001234)")
    ap.add_argument("--like", default="IF", metavar="NAME",
                    help="--find-key 가 뒤질 컬럼 이름 조각 (기본 IF)")
    ap.add_argument("--max-tables", type=int, default=60, metavar="N",
                    help="--find-key 가 확인할 컬럼 수 상한 (기본 60)")
    args = ap.parse_args()

    # ---- 진단 모드 ---------------------------------------------------------
    if args.test_key:
        # 메일함도 DB 도 건드리지 않는다. 패턴만 확인한다.
        print(f"키 접두어: {', '.join(IFERR_KEY_PREFIXES) or '(없음)'}")
        print(f"패턴 {len(IFERR_KEY_PATTERNS)}개\n")
        hits = extract_keys(args.test_key)
        if not hits:
            print("키를 찾지 못했다.")
            print(
                "  - 접두어 방식이면 config_local.py 에 "
                'IFERR_KEY_PREFIXES = ("EAIIF",) 처럼 추가\n'
                "  - 라벨 방식이면 IFERR_KEY_PATTERNS 에 정규식 추가"
            )
            raise SystemExit(1)
        for h in hits:
            print(f"  {h['key']:<24} (규칙 {h['rule']})")
            print(f"    근거: {h['evidence']}")
        raise SystemExit(0)

    if args.doctor:
        print(f"[점검] 최근 {args.hours or '설정값'}시간\n")
        results = doctor(hours=args.hours)
        for name, ok, msg in results:
            print(f"  {'OK  ' if ok else '실패'}  {name:<14} {msg}")
        failed = [n for n, ok, _ in results if not ok]
        print()
        if failed:
            print(f"확인 필요: {', '.join(failed)}")
            print("  자세히: agents/iferr/README.md 의 '증상별 확인 지점'")
        else:
            print("전부 정상 — python app/app.py 로 챗봇을 띄워도 된다")
        raise SystemExit(1 if failed else 0)

    if args.tables:
        t = interface_tables(args.tables)
        print(f"{t['key']}: {t['status']} (근거 {t['rule'] or '-'})")
        for row in t["tables"]:
            role = "출발" if row["role"] == "source" else "도착"
            print(f"  {role}: {system_table(row['system'], row['table'])}"
                  f"  (IFID {row['interface']})")
        for w in t["warnings"]:
            print(f"  [확인 필요] {w}")
        raise SystemExit(0 if t["status"] == "found" and t["tables"] else 1)

    if args.ifs:
        t = table_interfaces(args.ifs)
        print(f"{t['table']}: {t['status']} (근거 {t['rule'] or '-'})")
        for i in t["interfaces"]:
            role = "/".join("도착" if r == "target" else "출발" for r in i["roles"])
            print(f"  [{role}] {i['id']}  {flow_text(i)}")
        for w in t["warnings"]:
            print(f"  [확인 필요] {w}")
        raise SystemExit(0 if t["status"] == "found" else 1)

    if args.check_db:
        from core.oracle import check_connection

        ok, msg = check_connection()
        print(("OK   " if ok else "실패 ") + msg)
        raise SystemExit(0 if ok else 1)

    if args.find_column:
        from core.oracle import find_columns

        try:
            rows = find_columns(args.find_column)
        except Exception as e:
            print(f"[조회 실패] {type(e).__name__}: {e}")
            raise SystemExit(1)
        if not rows:
            print(f"'{args.find_column}' 가 이름에 들어간 테이블·컬럼이 없다.")
            print("  ORACLE_SCHEMA 가 맞는지 확인할 것(접속 계정과 다른 경우가 흔하다)")
            raise SystemExit(1)
        print(f"{'테이블':<32}{'컬럼':<28}타입")
        print("─" * 76)
        for r in rows:
            print(
                f"{r['TABLE_NAME']:<32}{r['COLUMN_NAME']:<28}"
                f"{r['DATA_TYPE']}({r['DATA_LENGTH']})"
            )
        raise SystemExit(0)

    if args.find_key:
        from core.oracle import find_value

        print(f"'{args.find_key}' 를 이름에 '{args.like}' 가 든 문자형 컬럼에서 찾는다...")
        try:
            hits = find_value(
                args.find_key, name_like=args.like, max_tables=args.max_tables
            )
        except Exception as e:
            print(f"[조회 실패] {type(e).__name__}: {e}")
            raise SystemExit(1)
        found = [h for h in hits if h.get("count")]
        if not found:
            print("찾지 못했다.")
            print(f"  - --like 를 넓혀 볼 것 (예: --like EAI, --like KEY)")
            print("  - ORACLE_SCHEMA 가 맞는지 확인할 것")
        for h in found:
            print(f"  {h['table']}.{h['column']}  {h['count']}건")
        errors = [h for h in hits if h.get("error")]
        if errors:
            # 권한 없는 테이블을 조용히 넘기면 '없다'로 오해한다.
            print(f"\n[확인 필요] 조회하지 못한 컬럼 {len(errors)}개 (권한 등)")
            print(f"  예: {errors[0]['table']}.{errors[0]['column']} — {errors[0]['error']}")
        raise SystemExit(0 if found else 1)

    if args.folders:
        from core.outlook import list_folders

        try:
            for path in list_folders():
                print(path)
        except Exception as e:
            print(f"[실패] {e}")
            raise SystemExit(1)
        raise SystemExit(0)

    if args.mails or args.dump:
        r = list_mails(hours=args.hours)

        # 메일을 못 읽었으면 표를 그리기 전에 원인부터 보여 준다.
        # 빈 표를 먼저 그리면 '오류 메일이 없구나'로 읽힌다.
        if not r["mails"] and r["warnings"]:
            print("[메일을 읽지 못했다]")
            for w in r["warnings"]:
                print(f"  {w}")
            print(
                "\n확인 순서:\n"
                "  1) python agents/iferr/agent.py --folders   # 폴더 이름 확인\n"
                "  2) config_local.py 의 MAIL_FOLDER 를 그 경로로\n"
                "  3) Outlook 이 실행 중인지, pip install pywin32 했는지"
            )
            raise SystemExit(1)

        print(f"메일 {r['mail_count']}통 (오류로 분류 {r['error_count']}통)")
        print(
            "판정 방식: "
            + (f"LLM ({JUDGE_MODEL}) + 키워드 안전망" if llm_judge_enabled() else "키워드만")
        )
        print(
            f"오류 키워드: [{MAIL_SUBJECT_MATCH}] "
            f"{', '.join(MAIL_SUBJECT_KEYWORDS) or '(없음)'}"
        )
        print(
            "  머리말 제거: "
            + (", ".join(MAIL_SUBJECT_STRIP_PREFIXES) or "안 함 (RE:/FW: 사본 제외)")
        )
        for problem in check_subject_rule():
            print(f"  [설정 확인] {problem}")
        print()
        print(f"{'오류':<5}{'판정':<10}{'수신시각':<18}{'걸린키워드':<16}{'키':<22}제목")
        print("─" * 110)
        for m in r["mails"]:
            mark = "  O  " if m["is_error"] else "  .  "
            # 누가 오류로 봤는가. '!' 는 키워드와 LLM 이 갈린 건 — 사람이 볼 것.
            by = (m.get("judged_by") or "rule") if m["is_error"] else "-"
            by = (by + "!") if m.get("conflict") else by
            keys = ", ".join(m["keys"]) or ("-" if not m["is_error"] else "못찾음")
            # 걸린 키워드를 함께 보여 준다. 엉뚱한 메일이 O 로 찍히면
            # 여기를 보고 바로 원인을 안다(예: 한 글자짜리 '오').
            # 컬럼 폭을 넘는 값은 잘라 정렬을 유지한다.
            # "(EAA) Alert Mail" 처럼 긴 키워드에서 표가 밀린다.
            hit = m["matched"] or "-"
            if m.get("via_prefix"):
                hit += "(전달)"      # 원본이 아니라 사본임을 표시
            hit = hit if len(hit) <= 14 else hit[:13] + "…"
            print(
                f"{mark}{by:<10}{m['received'][:16]:<18}{hit:<16}"
                f"{keys:<22}{m['subject'][:40]}"
            )
            if m.get("llm_reason") and (m["is_error"] or m.get("conflict")):
                print(f"{'':<15}└ LLM: {m['llm_reason'][:80]}")
        for w in r["warnings"]:
            print(f"\n[확인 필요] {w}")

        if args.dump:
            # 본문에는 개인정보가 있을 수 있다. out/ 은 .gitignore 에 있어
            # 커밋되지 않지만, 외부로 보낼 때는 반드시 내용을 확인할 것.
            from core.outlook import MailUnavailable, read_mails

            out_dir = _ROOT / "out"
            out_dir.mkdir(exist_ok=True)
            saved = 0
            try:
                mails = read_mails(since_hours=args.hours)
            except MailUnavailable as e:
                print(f"[메일을 읽지 못했다] {e}")
                raise SystemExit(1)
            # 표와 같은 판정으로 고른다. 표에서 O 인데 저장이 안 되면 헷갈린다.
            judged, _ = judge_mails(mails)
            for mail in (j["mail"] for j in judged if j["is_error"]):
                if saved >= args.dump:
                    continue
                path = out_dir / f"mail_{saved + 1}.txt"
                path.write_text(
                    f"제목: {mail.subject}\n발신: {mail.sender_masked}\n"
                    f"수신: {mail.received}\n{'─' * 60}\n{mail.body}",
                    encoding="utf-8",
                )
                print(f"저장: {path}")
                saved += 1
            print(
                "\n본문을 보고 config_local.py 의 IFERR_KEY_PATTERNS 를 맞출 것. "
                "(개인정보가 있을 수 있으니 외부 공유 전 확인)"
            )
        raise SystemExit(0)

    # ---- 통합 실행 ---------------------------------------------------------
    result = run_iferr(hours=args.hours, key=args.key, detail=args.detail, subject=args.subject)
    print(json.dumps(result, ensure_ascii=False, indent=2, default=str))

    # 규칙별 집계. 무엇이 어떤 근거로 판정됐는지 콘솔에도 남긴다.
    counts: dict[str, int] = {}
    for c in result.get("cases", []):
        k = f"{c.get('decided_by', '-')}/{c.get('rule', '-')}"
        counts[k] = counts.get(k, 0) + 1
    print("\n[규칙별 집계]")
    for k, v in sorted(counts.items()):
        print(f"  {k:<28} {v:>4}건")
    if result.get("warnings"):
        print("\n[확인 필요]")
        for w in result["warnings"]:
            print(f"  - {w}")
