"""공통 설정 — 이 저장소의 단일 진실 원천(single source of truth).

사용법:
    from config import OLLAMA_HOST, USE_LLM, describe

규칙:
    - core/, agents/, app/ 어디에서도 os.getenv 를 다시 부르지 말 것.
      설정이 이원화되면 한쪽만 고쳤을 때 서로 다른 값을 보게 된다.
      (실제로 앱은 새 경로를, 툴은 기본 경로를 보고 있어 같은 질문에
       다른 답이 나온 적이 있다.)
    - 기본값은 코드에 두어 환경변수 없이도 바로 실행 가능해야 한다.
    - Windows 경로 기본값은 반드시 raw 문자열 r"..." 로 쓸 것.
      "C:\temp\new" 처럼 쓰면 \t, \n 이 해석되어 조용히 깨진다.

이 파일을 직접 고치지 말 것 (중요):
    이 파일은 git 이 추적한다. 사내 PC 에서 값을 고치면 git pull 때마다
    충돌이 난다. 사내 값은 config_local.py 에 쓴다. 그 파일은 .gitignore
    에 있어 추적되지 않으므로 pull 이 깨끗하게 된다.

        copy config_local.example.py config_local.py    # 최초 1회
        # config_local.py 를 열어 사내 값만 적는다

    우선순위: 환경변수 > config_local.py > 이 파일의 기본값
    (환경변수를 위에 둔 이유: USE_LLM=false 처럼 한 번만 다르게 실행하는
     경우가 있어야 하기 때문이다. 항구적인 값은 config_local.py 에 쓴다.)
"""

from __future__ import annotations

import os
import re
import sys
from types import ModuleType
from typing import Any, Dict, Mapping


def _env_str(key: str, default: str) -> str:
    # 빈 문자열 환경변수는 "설정 안 함"으로 본다. 배치 스크립트에서
    # SET VAR= 로 지워둔 경우 기본값이 살아나야 하기 때문이다.
    v = os.getenv(key)
    return v if v else default


def _env_int(key: str, default: int) -> int:
    try:
        return int(_env_str(key, str(default)))
    except ValueError:
        return default


def _env_bool(key: str, default: bool) -> bool:
    v = os.getenv(key)
    if v is None or v == "":
        return default
    return v.strip().lower() in ("1", "true", "yes", "y", "on")


def _as_tuple(value: Any) -> tuple[str, ...]:
    """문자열/리스트/튜플 무엇이 오든 문자열 튜플로 만든다.

    ("오류") 는 튜플이 아니라 문자열이다(쉼표가 없다). 그대로 순회하면
    "오", "류" 한 글자씩 검사하게 되어 '오전', '오더' 같은 제목이 전부
    걸린다. 파이썬에서 가장 흔한 함정이고 눈으로는 잘 안 보인다.
    설정 파일은 사람이 손으로 쓰는 곳이라 여기서 받아 준다.
    """
    if isinstance(value, str):
        return tuple(v.strip() for v in value.split(",") if v.strip())
    return tuple(str(v).strip() for v in value if str(v).strip())


def _env_opt(key: str) -> str | None:
    """설정되지 않으면 None. DB 처럼 '없어도 되는' 값에 쓴다."""
    v = os.getenv(key)
    return v if v else None


# --------------------------------------------------------------------------
# 소스 루트 (라벨: 경로). 여러 개를 둘 수 있다.
# 코드 스캔 계열 에이전트가 여기를 기준으로 파일을 찾는다.
# --------------------------------------------------------------------------
SOURCE_ROOTS: Dict[str, str] = {
    "ERP": _env_str("SOURCE_ROOT_ERP", r"./src"),
    # 설치 직후 바로 돌려 볼 수 있도록 예제 소스를 하나 물려 둔다.
    # 사내 배포 시에는 지우거나 SOURCE_ROOT_ERP 로 실제 경로를 준다.
    "SAMPLE": _env_str("SOURCE_ROOT_SAMPLE", r"./samples/src"),
}

# --------------------------------------------------------------------------
# Ollama
# --------------------------------------------------------------------------
OLLAMA_HOST: str = _env_str("OLLAMA_HOST", "http://localhost:11434")

# 챗봇 본체가 쓰는 모델(툴 호출 담당)
CHAT_MODEL: str = _env_str("CHAT_MODEL", "gemma4:e2b")

# 에이전트 내부 판정용 모델. 챗 모델과 분리해 두면
# 판정만 더 작은/큰 모델로 바꾸기 쉽다.
JUDGE_MODEL: str = _env_str("JUDGE_MODEL", "gemma4:e2b")

# num_ctx 기본값(2048)은 긴 프롬프트를 '조용히' 잘라낸다.
# 잘린 줄 모르고 결과만 이상해지므로 반드시 명시한다.
# VRAM 6GB 노트북 기준으로 8192 가 안전한 상한이다.
NUM_CTX: int = _env_int("NUM_CTX", 8192)

# False 면 LLM 없이 규칙만으로 동작한다. 폐쇄망/Ollama 미기동 상황에서도
# 서버 기동과 CLI 실행이 가능해야 하므로 각 에이전트가 이 플래그를 존중한다.
USE_LLM: bool = _env_bool("USE_LLM", True)

# --------------------------------------------------------------------------
# Oracle (없으면 None. DB 를 쓰지 않는 에이전트는 영향받지 않아야 한다)
# --------------------------------------------------------------------------
ORACLE_DSN: str | None = _env_opt("ORACLE_DSN")
ORACLE_USER: str | None = _env_opt("ORACLE_USER")
ORACLE_PASSWORD: str | None = _env_opt("ORACLE_PASSWORD")
DB_TIMEOUT_SEC: int = _env_int("DB_TIMEOUT_SEC", 30)

# thick 모드(Oracle Client 라이브러리 사용).
# python-oracledb 는 기본이 thin 모드라 클라이언트 설치 없이 붙지만,
# 오래된 서버(대개 12.1 미만)는 지원하지 않아 DPY-3010 으로 거절당한다.
# 그때 thick 모드로 바꾸면 된다. False 로 두어도 DPY-3010 이 나면
# 자동으로 한 번 재시도하므로, 보통은 건드릴 필요가 없다.
ORACLE_THICK_MODE: bool = _env_bool("ORACLE_THICK_MODE", False)

# Oracle Client(Instant Client) 라이브러리 폴더.
# 비우면 PATH 와 레지스트리에서 찾는다. 사내 PC 에 이미 클라이언트가
# 깔려 있으면(SQL Developer, Toad 등) 대개 비워 두어도 된다.
#     ORACLE_CLIENT_LIB_DIR = r"C:\oracle\instantclient_21_13"
ORACLE_CLIENT_LIB_DIR: str = _env_str("ORACLE_CLIENT_LIB_DIR", "")

# 테이블이 있는 스키마(소유자). 비우면 접속 계정과 같다고 본다.
# 읽기 전용 계정으로 접속해 다른 스키마의 테이블을 보는 경우가 흔하다.
ORACLE_SCHEMA: str | None = _env_opt("ORACLE_SCHEMA")

# --------------------------------------------------------------------------
# Outlook 메일 (iferr 에이전트)
#
# 읽기 전용이다. 회신·삭제·이동은 하지 않는다(안전 규칙).
# --------------------------------------------------------------------------
# com : 로컬 Outlook 데스크톱 (Windows + pywin32). 폐쇄망 기본값
# eml : 폴더의 .eml 파일 (테스트·리눅스 개발 PC)
MAIL_BACKEND: str = _env_str("MAIL_BACKEND", "com")

# COM 백엔드에서 볼 폴더. 빈 값이면 기본 받은 편지함.
# Outlook 규칙으로 인터페이스 오류 메일만 모아 둔 하위 폴더를 지정하면
# 스캔 범위가 줄어 훨씬 빠르다.
MAIL_FOLDER: str = _env_str("MAIL_FOLDER", r"받은 편지함\인터페이스")

# eml 백엔드에서 읽을 폴더
MAIL_EML_DIR: str = _env_str("MAIL_EML_DIR", r"./samples/mail")

# 몇 시간 전까지 볼 것인가. 사내 사서함은 수만 통이라 기간을 좁히지 않으면
# 요청 하나가 몇 분씩 걸린다.
MAIL_LOOKBACK_HOURS: int = _env_int("MAIL_LOOKBACK_HOURS", 24)
MAIL_MAX_COUNT: int = _env_int("MAIL_MAX_COUNT", 200)

# 오류 메일을 고르는 제목 키워드. 하나라도 걸리면 대상으로 본다.
# 항목이 하나여도 쉼표를 붙일 것 — ("오류") 는 튜플이 아니라 문자열이다.
MAIL_SUBJECT_KEYWORDS: tuple[str, ...] = tuple(
    _env_str("MAIL_SUBJECT_KEYWORDS", "오류,에러,실패,ERROR,FAIL,FAILED,EXCEPTION").split(",")
)

# 제목을 어떻게 비교할 것인가. 대소문자는 어느 모드에서나 무시한다.
#   contains   : 제목 어디에든 있으면 (기본)
#   startswith : 제목이 그 문구로 시작할 때만.
#                "(EAA) Alert Mail" 처럼 발신 시스템이 고정 머리말을 붙이는
#                경우에 쓴다. 본문에 그 단어가 우연히 들어간 메일을 걸러낸다
#   regex      : 정규식으로 비교
MAIL_SUBJECT_MATCH: str = _env_str("MAIL_SUBJECT_MATCH", "contains")

# 제목 앞에 붙는 회신·전달 머리말. 비어 있으면(기본) 떼지 않는다.
#
# 기본이 '안 뗌'인 이유: startswith 는 "문자 그대로 그 문구로 시작"을
# 뜻해야 한다. 머리말을 떼면 "RE: FW: (EAA) Alert Mail ..." 도 걸리는데,
# 그건 시스템이 보낸 원본 알림이 아니라 사람이 주고받은 사본이라 대개
# 중복이다. 원본만 보는 편이 집계가 깨끗하다.
#
# 전달된 알림까지 잡아야 하면 여기에 머리말을 넣는다.
#     MAIL_SUBJECT_STRIP_PREFIXES = ("RE:", "FW:", "FWD:", "회신:", "전달:")
MAIL_SUBJECT_STRIP_PREFIXES: tuple[str, ...] = tuple(
    p for p in _env_str("MAIL_SUBJECT_STRIP_PREFIXES", "").split(",") if p.strip()
)

# --------------------------------------------------------------------------
# 인터페이스 오류 확인 (iferr 에이전트)
# --------------------------------------------------------------------------
# 메일에서 인터페이스 키를 뽑는 정규식. 그룹 1이 키다.
# 위에서부터 순서대로 시도하고, 맞는 것이 나오면 그 규칙 이름을 근거로 남긴다.
# 실제 메일 형식을 확인한 뒤 이 목록만 고치면 된다.
# 인터페이스 ID 접두어. 여기에 접두어만 적으면 "접두어 + 숫자" 패턴이
# 자동으로 만들어져 아래 IFERR_KEY_PATTERNS 앞에 붙는다.
# 정규식을 직접 쓸 필요가 없고, 사내 고유 접두어를 추적되는 파일에 남기지
# 않아도 된다(config_local.py 는 git 이 추적하지 않는다).
#
#     IFERR_KEY_PREFIXES = ("EAIIF",)     → EAIIF0001234 를 키로 뽑는다
IFERR_KEY_PREFIXES: tuple[str, ...] = tuple(
    p for p in _env_str("IFERR_KEY_PREFIXES", "").split(",") if p.strip()
)

IFERR_KEY_PATTERNS: tuple[tuple[str, str], ...] = (
    ("if-id-labeled", r"(?i)\bIF[_\-]?ID\s*[:=]\s*([A-Za-z0-9_\-]{3,40})"),
    ("interface-ko", r"인터페이스\s*(?:ID|아이디|키|번호)\s*[:=]?\s*([A-Za-z0-9_\-]{3,40})"),
    ("tx-no-ko", r"(?:전문|거래|연계)\s*(?:번호|키)\s*[:=]?\s*([A-Za-z0-9_\-]{3,40})"),
    ("if-prefix", r"(?i)\b(IF[_\-]?[A-Z0-9]{2,}[_\-][A-Z0-9]{2,})\b"),
)

# 키로 조회할 SQL. 바인드 변수 이름은 :if_key 로 고정한다.
# 문자열 결합 금지(주입 위험). 값은 반드시 바인드로 들어간다.
#
# {schema} 는 ORACLE_SCHEMA 로 치환된다(미설정이면 빈 문자열).
# 접속 계정과 테이블 소유자가 다를 때 SQL 을 고치지 않아도 되게 하려는 것이다.
IFERR_SQL: Dict[str, str] = {
    # 인터페이스 정의 마스터. 이 인터페이스가 무엇을 어디로 나르는지.
    "header": _env_str(
        "IFERR_SQL_HEADER",
        """
        SELECT IFID, SRCSYS, TARSYS, SRCTNAME, TARTNAME
          FROM {schema}IF_MST
         WHERE IFID = :if_key
        """,
    ),
    # 그 인터페이스가 실어 온 데이터 행 (이력/로그 테이블이 있으면 여기에)
    "detail": _env_str("IFERR_SQL_DETAIL", ""),
    # 그 데이터가 영향을 주는 후속 대상
    "impact": _env_str("IFERR_SQL_IMPACT", ""),
}

# 마스터 조회 결과에서 의미 있는 컬럼 이름.
# 사이트마다 이름이 다를 수 있어 매핑으로 둔다. 영향 문구를 만들 때 쓴다.
IFERR_MASTER_FIELDS: Dict[str, str] = {
    "id": _env_str("IFERR_FIELD_ID", "IFID"),
    "src_sys": _env_str("IFERR_FIELD_SRC_SYS", "SRCSYS"),
    "tar_sys": _env_str("IFERR_FIELD_TAR_SYS", "TARSYS"),
    "src_table": _env_str("IFERR_FIELD_SRC_TABLE", "SRCTNAME"),
    "tar_table": _env_str("IFERR_FIELD_TAR_TABLE", "TARTNAME"),
}

# 조회 결과에서 상태로 볼 컬럼 후보. 있으면 값별로 집계해 보여준다.
# 스키마를 몰라도 동작하게 하기 위한 장치다.
IFERR_STATUS_COLUMNS: tuple[str, ...] = tuple(
    _env_str(
        "IFERR_STATUS_COLUMNS", "STATUS,STS,PROC_STATUS,ERR_CD,ERROR_CODE,RESULT_CD"
    ).split(",")
)

# 한 번에 조회할 최대 행 수. 로컬 LLM 이 붙은 요청 하나가 수만 행을
# 끌어오면 챗봇이 통째로 멎는다.
IFERR_MAX_ROWS: int = _env_int("IFERR_MAX_ROWS", 200)

# --------------------------------------------------------------------------
# MCP (Model Context Protocol) — 스펙 리비전 2026-07-28
#
# 툴은 전부 MCP 서버(mcp_server/)가 노출하고, 챗봇은 MCP 클라이언트로 붙는다.
# 2026-07-28 리비전은 코어가 stateless 라 initialize 핸드셰이크와
# Mcp-Session-Id 가 없다. 요청 하나가 프로토콜 버전·클라이언트 정보·능력을
# _meta 에 실어 스스로 완결된다. 그래서 HTTP 모드에서 서버를 여러 벌 띄우고
# 앞에 로드밸런서를 둬도 세션 고정(sticky session)이 필요 없다.
# --------------------------------------------------------------------------
MCP_PROTOCOL_VERSION: str = "2026-07-28"

# stdio | streamable-http
# stdio 가 기본인 이유: 폐쇄망 단일 장비에서는 포트를 열 이유가 없고,
# 호스트가 서버 프로세스를 직접 띄우므로 인증 문제도 없다.
# 에이전트를 다른 장비로 뺄 때만 streamable-http 로 바꾼다.
MCP_TRANSPORT: str = _env_str("MCP_TRANSPORT", "stdio")

# streamable-http 로 띄울 때 서버가 바인딩할 주소
MCP_HTTP_HOST: str = _env_str("MCP_HTTP_HOST", "127.0.0.1")
MCP_HTTP_PORT: int = _env_int("MCP_HTTP_PORT", 8765)

# 클라이언트(챗봇)가 붙을 주소. streamable-http 일 때만 쓴다.
# 비워 두면 아래(_finalize)에서 HOST/PORT 로 조립한다. config_local.py 가
# 포트만 바꾼 경우에도 URL 이 따라가야 하므로 여기서 완성하지 않는다.
MCP_SERVER_URL: str = _env_str("MCP_SERVER_URL", "")

# 챗봇(로컬 LLM)에 노출할 툴 등급.
# 소형 모델은 툴을 순서대로 여러 개 부르지 못하므로 기본은 통합 툴만 준다.
# MCP 서버는 단계별 툴도 그대로 노출하므로 Claude Code·IDE 등 다른 호스트에서는
# 전부 쓸 수 있다. 큰 모델로 바꾸면 "combo,step" 으로 열면 된다.
CHAT_TOOL_TIERS: tuple[str, ...] = tuple(
    t.strip() for t in _env_str("CHAT_TOOL_TIERS", "combo").split(",") if t.strip()
)

# 툴 한 번 호출의 상한. 로컬 LLM 이 물고 늘어질 때 요청 스레드가
# 무한정 잡히는 것을 막는다.
MCP_TOOL_TIMEOUT_SEC: int = _env_int("MCP_TOOL_TIMEOUT_SEC", 120)

# MRTR(Multi Round-Trip Request) 최대 왕복 횟수.
# 서버가 resultType="input_required" 로 되물으면 클라이언트가 답을 채워
# 재요청한다. 무한 왕복을 막기 위해 상한을 둔다.
MCP_INPUT_REQUIRED_MAX_ROUNDS: int = _env_int("MCP_INPUT_REQUIRED_MAX_ROUNDS", 5)

# --------------------------------------------------------------------------
# 웹 서버
# --------------------------------------------------------------------------
HOST: str = _env_str("HOST", "127.0.0.1")
PORT: int = _env_int("PORT", 5000)
DEBUG: bool = _env_bool("DEBUG", False)


# --------------------------------------------------------------------------
# config_local.py 덮어쓰기
#
# 사내 PC 는 pull 만 한다. 추적되는 파일을 고치면 충돌이 나므로,
# 사내 값은 추적하지 않는 config_local.py 에 두고 여기서 덮어쓴다.
# --------------------------------------------------------------------------

# 어떤 값이 어디서 왔는지 남긴다. 설정 이원화 사고를 진단할 때 이게 없으면
# "왜 이 값이지"를 추적할 수 없다.
CONFIG_LOCAL_APPLIED: list[str] = []
CONFIG_LOCAL_UNKNOWN: list[str] = []
CONFIG_LOCAL_FILE: str = ""


def _merge_overrides(
    target: Dict[str, Any],
    overrides: Dict[str, Any],
    env: Mapping[str, str],
) -> tuple[list[str], list[str]]:
    """overrides 를 target 에 반영한다. (적용된 이름, 모르는 이름)

    - 같은 이름의 환경변수가 있으면 환경변수가 이긴다.
      한 번만 다르게 실행하는 경우(USE_LLM=false)를 막지 않기 위해서다.
    - target 에 없는 이름은 반영하지 않고 따로 돌려준다.
      ORACLE_DNS 처럼 오타를 내면 조용히 무시되어 "설정했는데 왜 안 되지"로
      한참 헤매게 된다. 이름이 틀렸다는 사실이 드러나야 한다.
    """
    applied: list[str] = []
    unknown: list[str] = []
    for name, value in overrides.items():
        if name.startswith("_") or isinstance(value, ModuleType) or callable(value):
            continue
        if name not in target:
            unknown.append(name)
            continue
        if env.get(name):        # 환경변수가 우선
            continue
        target[name] = value
        applied.append(name)
    return applied, unknown


def _load_local() -> None:
    """config_local.py 가 있으면 읽어 덮어쓴다. 없어도 정상 동작한다."""
    global CONFIG_LOCAL_FILE
    try:
        import config_local  # type: ignore
    except ImportError:
        return

    CONFIG_LOCAL_FILE = getattr(config_local, "__file__", "config_local.py") or ""
    applied, unknown = _merge_overrides(globals(), vars(config_local), os.environ)
    CONFIG_LOCAL_APPLIED[:] = sorted(applied)
    CONFIG_LOCAL_UNKNOWN[:] = sorted(unknown)

    if unknown:
        # stdout 에 쓰면 안 된다. stdio MCP 모드에서 stdout 은 프로토콜
        # 전용 채널이라 한 줄만 섞여도 클라이언트가 핸드셰이크에서 죽는다.
        print(
            f"[config] config_local.py 에 모르는 이름이 있다(오타 확인): "
            f"{', '.join(CONFIG_LOCAL_UNKNOWN)}",
            file=sys.stderr,
        )


_load_local()

# 목록형 설정은 덮어쓰기가 끝난 뒤에 정규화한다.
# config_local.py 에 ("오류") 처럼 쉼표를 빠뜨리면 문자열이 되는데,
# 그대로 두면 한 글자씩 비교해 엉뚱한 메일이 오류로 잡힌다.
CONFIG_NORMALIZED: list[str] = []
for _name in (
    "MAIL_SUBJECT_KEYWORDS",
    "MAIL_SUBJECT_STRIP_PREFIXES",
    "IFERR_STATUS_COLUMNS",
    "CHAT_TOOL_TIERS",
    "SOURCE_ROOTS",
):
    _value = globals()[_name]
    if _name == "SOURCE_ROOTS":
        continue          # dict 는 그대로 둔다
    if isinstance(_value, str):
        CONFIG_NORMALIZED.append(_name)
    globals()[_name] = _as_tuple(_value)

if CONFIG_NORMALIZED:
    print(
        "[config] 목록 설정이 문자열로 쓰여 있어 자동 변환했다(쉼표 확인): "
        f"{', '.join(CONFIG_NORMALIZED)}  예: (\"오류\",) 처럼 쉼표를 붙일 것",
        file=sys.stderr,
    )

# 접두어로부터 키 패턴을 만든다. 덮어쓰기가 끝난 뒤여야 config_local.py 의
# 접두어가 반영된다.
IFERR_KEY_PREFIXES = _as_tuple(IFERR_KEY_PREFIXES)
if IFERR_KEY_PREFIXES:
    IFERR_KEY_PATTERNS = (
        tuple(
            (
                f"prefix-{p.lower()}",
                # 식별자 경계를 직접 정의한다. \b 는 언더스코어를 단어 문자로
                # 보기 때문에 코드·로그에 섞인 ID 를 놓치거나 잘못 자른다.
                #
                # 뒤쪽 경계에 숫자까지 넣는 이유(중요): (?![A-Za-z_]) 로만
                # 두면 EAIIF0001234_TMP 에서 정규식이 되돌아가며 숫자를
                # 하나 뱉어 EAIIF000123 으로 '잘린 키'를 만든다. 잘린 키로
                # DB 를 조회하면 없는 행을 찾거나 엉뚱한 행을 집는다.
                # 못 찾는 것보다 나쁘다.
                rf"(?<![A-Za-z0-9_])({re.escape(p)}[0-9]+)(?![A-Za-z0-9_])",
            )
            for p in IFERR_KEY_PREFIXES
        )
        # 접두어 패턴을 앞에 둔다. 같은 키를 여러 규칙이 잡으면 먼저 걸린
        # 규칙 이름이 근거로 남으므로, 확실한 쪽이 앞에 와야 한다.
        + IFERR_KEY_PATTERNS
    )

# 파생값은 덮어쓰기가 끝난 뒤에 조립한다.
if not MCP_SERVER_URL:
    MCP_SERVER_URL = f"http://{MCP_HTTP_HOST}:{MCP_HTTP_PORT}/mcp"


def describe() -> str:
    """현재 설정을 사람이 읽을 수 있는 형태로. 서버 기동 시 출력한다.

    '어떤 설정으로 떠 있는지'가 화면에 남아야 설정 이원화 사고를
    빨리 알아챌 수 있다. 비밀번호는 값 대신 설정 여부만 보여준다.
    """
    roots = ", ".join(f"{k}={v}" for k, v in SOURCE_ROOTS.items()) or "(없음)"
    lines = [
        "─" * 60,
        " 설정 (config.py)",
        "─" * 60,
        f"  SOURCE_ROOTS : {roots}",
        f"  OLLAMA_HOST  : {OLLAMA_HOST}",
        f"  CHAT_MODEL   : {CHAT_MODEL}",
        f"  JUDGE_MODEL  : {JUDGE_MODEL}",
        f"  NUM_CTX      : {NUM_CTX}",
        f"  USE_LLM      : {USE_LLM}",
        f"  ORACLE_DSN   : {ORACLE_DSN or '(미설정 — DB 에이전트 비활성)'}",
        f"  ORACLE_USER  : {ORACLE_USER or '(미설정)'}",
        f"  ORACLE_PW    : {'설정됨' if ORACLE_PASSWORD else '(미설정)'}",
        f"  ORACLE_SCHEMA: {ORACLE_SCHEMA or '(접속 계정과 동일)'}",
        f"  ORACLE_MODE  : {'thick' if ORACLE_THICK_MODE else 'thin (필요시 자동 전환)'}"
        + (f" / {ORACLE_CLIENT_LIB_DIR}" if ORACLE_CLIENT_LIB_DIR else ""),
        f"  DB_TIMEOUT   : {DB_TIMEOUT_SEC}s",
        f"  오류 키워드   : {', '.join(MAIL_SUBJECT_KEYWORDS) or '(없음 — 전부 대상 아님)'}"
        + f"  [{MAIL_SUBJECT_MATCH}]",
        f"  MAIL         : {MAIL_BACKEND} / "
        + (MAIL_FOLDER or "(기본 받은 편지함)" if MAIL_BACKEND == "com" else MAIL_EML_DIR)
        + f" / 최근 {MAIL_LOOKBACK_HOURS}h",
        f"  키 접두어     : "
        + (", ".join(IFERR_KEY_PREFIXES) or "(없음 — 라벨 패턴만 사용)"),
        f"  IFERR_SQL    : "
        + (", ".join(k for k, v in IFERR_SQL.items() if v.strip()) or "(미설정 — 조회 불가)"),
        f"  MCP          : {MCP_PROTOCOL_VERSION} / {MCP_TRANSPORT}"
        + (f" → {MCP_SERVER_URL}" if MCP_TRANSPORT != "stdio" else " (python -m mcp_server)"),
        f"  SERVER       : http://{HOST}:{PORT} (debug={DEBUG})",
        f"  config_local : "
        + (
            f"{CONFIG_LOCAL_FILE} → {', '.join(CONFIG_LOCAL_APPLIED) or '(적용된 값 없음)'}"
            if CONFIG_LOCAL_FILE
            else "(없음 — 기본값 사용)"
        ),
        "─" * 60,
    ]
    return "\n".join(lines)


if __name__ == "__main__":
    print(describe())
