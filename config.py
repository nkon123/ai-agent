"""공통 설정 — 이 저장소의 단일 진실 원천(single source of truth).

사용법:
    from config import OLLAMA_HOST, USE_LLM, describe

규칙:
    - core/, agents/, app/ 어디에서도 os.getenv 를 다시 부르지 말 것.
      설정이 이원화되면 한쪽만 고쳤을 때 서로 다른 값을 보게 된다.
      (실제로 앱은 새 경로를, 툴은 기본 경로를 보고 있어 같은 질문에
       다른 답이 나온 적이 있다.)
    - 기본값은 코드에 두어 환경변수 없이도 바로 실행 가능해야 한다.
      환경변수는 "덮어쓰기" 용도일 뿐이다.
    - Windows 경로 기본값은 반드시 raw 문자열 r"..." 로 쓸 것.
      "C:\temp\new" 처럼 쓰면 \t, \n 이 해석되어 조용히 깨진다.
"""

from __future__ import annotations

import os
from typing import Dict


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
MAIL_SUBJECT_KEYWORDS: tuple[str, ...] = tuple(
    _env_str("MAIL_SUBJECT_KEYWORDS", "오류,에러,실패,ERROR,FAIL,FAILED,EXCEPTION").split(",")
)

# --------------------------------------------------------------------------
# 인터페이스 오류 확인 (iferr 에이전트)
# --------------------------------------------------------------------------
# 메일에서 인터페이스 키를 뽑는 정규식. 그룹 1이 키다.
# 위에서부터 순서대로 시도하고, 맞는 것이 나오면 그 규칙 이름을 근거로 남긴다.
# 실제 메일 형식을 확인한 뒤 이 목록만 고치면 된다.
IFERR_KEY_PATTERNS: tuple[tuple[str, str], ...] = (
    ("if-id-labeled", r"(?i)\bIF[_\-]?ID\s*[:=]\s*([A-Za-z0-9_\-]{3,40})"),
    ("interface-ko", r"인터페이스\s*(?:ID|아이디|키|번호)\s*[:=]?\s*([A-Za-z0-9_\-]{3,40})"),
    ("tx-no-ko", r"(?:전문|거래|연계)\s*(?:번호|키)\s*[:=]?\s*([A-Za-z0-9_\-]{3,40})"),
    ("if-prefix", r"(?i)\b(IF[_\-]?[A-Z0-9]{2,}[_\-][A-Z0-9]{2,})\b"),
)

# 키로 조회할 SQL. 바인드 변수 이름은 :if_key 로 고정한다.
# 스키마가 확정되면 여기만 채우면 된다 — 에이전트 코드는 손대지 않는다.
# 문자열 결합 금지(주입 위험). 값은 반드시 바인드로 들어간다.
IFERR_SQL: Dict[str, str] = {
    # 인터페이스 1건의 헤더/상태
    "header": _env_str("IFERR_SQL_HEADER", ""),
    # 그 인터페이스가 실어 온 데이터 행
    "detail": _env_str("IFERR_SQL_DETAIL", ""),
    # 그 데이터가 영향을 주는 후속 대상
    "impact": _env_str("IFERR_SQL_IMPACT", ""),
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
MCP_SERVER_URL: str = _env_str(
    "MCP_SERVER_URL", f"http://{MCP_HTTP_HOST}:{MCP_HTTP_PORT}/mcp"
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
        f"  DB_TIMEOUT   : {DB_TIMEOUT_SEC}s",
        f"  MAIL         : {MAIL_BACKEND} / "
        + (MAIL_FOLDER or "(기본 받은 편지함)" if MAIL_BACKEND == "com" else MAIL_EML_DIR)
        + f" / 최근 {MAIL_LOOKBACK_HOURS}h",
        f"  IFERR_SQL    : "
        + (", ".join(k for k, v in IFERR_SQL.items() if v.strip()) or "(미설정 — 조회 불가)"),
        f"  MCP          : {MCP_PROTOCOL_VERSION} / {MCP_TRANSPORT}"
        + (f" → {MCP_SERVER_URL}" if MCP_TRANSPORT != "stdio" else " (python -m mcp_server)"),
        f"  SERVER       : http://{HOST}:{PORT} (debug={DEBUG})",
        "─" * 60,
    ]
    return "\n".join(lines)


if __name__ == "__main__":
    print(describe())
