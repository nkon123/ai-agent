"""사내 PC 전용 설정. 이 파일을 config_local.py 로 복사해서 쓴다.

    copy config_local.example.py config_local.py     # Windows
    cp   config_local.example.py config_local.py     # Linux/Mac

config_local.py 는 .gitignore 에 있어 git 이 추적하지 않는다.
그래서 사내 PC 에서 값을 고쳐도 git pull 이 충돌 없이 된다.
이 예제 파일(config_local.example.py)은 추적되므로 여기에 실제 값을
적지 말 것 — 특히 비밀번호.

우선순위: 환경변수 > config_local.py > config.py 기본값

여기에 적는 이름은 config.py 에 있는 이름과 정확히 같아야 한다.
오타가 나면 기동 시 stderr 로 경고가 뜨고 describe() 에도 나온다.
필요한 값만 남기고 나머지는 지우면 된다.
"""

# --------------------------------------------------------------------------
# 소스 루트 — usage 에이전트가 뒤질 경로
# Windows 경로는 반드시 r"..." 로 쓸 것. \t, \n 이 해석되어 깨진다.
# --------------------------------------------------------------------------
SOURCE_ROOTS = {
    "ERP": r"D:\src\erp",
    "MES": r"D:\src\mes",
}

# 확장자 → 언어. 기본값에 .pc(Pro*C) / .prc(프로시저) / .xml(UI 쿼리)이
# 들어 있다. 사내에서 다른 확장자를 쓰면 여기서 바꾼다.
# 여기 없는 확장자는 아예 스캔하지 않는다.
# SOURCE_LANG_BY_SUFFIX = {
#     ".pc": "pro", ".prc": "plsql", ".xml": "xml", ".sql": "sql",
# }

# --------------------------------------------------------------------------
# Ollama
# --------------------------------------------------------------------------
# OLLAMA_HOST = "http://localhost:11434"
# CHAT_MODEL = "gemma4:e2b"
# JUDGE_MODEL = "gemma4:e2b"
# USE_LLM = True

# --------------------------------------------------------------------------
# Oracle — 비밀번호가 들어가므로 이 파일은 절대 커밋하지 말 것
# --------------------------------------------------------------------------
ORACLE_DSN = "dbhost:1521/ORCL"
ORACLE_USER = "erp_read"
ORACLE_PASSWORD = "여기에 비밀번호"
# 접속 계정과 테이블 소유자가 다르면 지정한다(읽기 전용 계정에서 흔하다).
# ORACLE_SCHEMA = "ERP"

# DPY-3010 (thin 모드가 서버 버전을 지원하지 않음, 대개 12.1 미만) 이 나면
# thick 모드가 필요하다. 자동 전환을 시도하므로 보통은 그대로 두면 되고,
# Oracle Client 를 못 찾을 때만 폴더를 지정한다.
# ORACLE_THICK_MODE = True
# ORACLE_CLIENT_LIB_DIR = r"C:\oracle\instantclient_21_13"

# --------------------------------------------------------------------------
# Outlook — iferr 에이전트
# --------------------------------------------------------------------------
MAIL_BACKEND = "com"
# Outlook 규칙으로 오류 메일만 모아 둔 하위 폴더를 지정하면 훨씬 빠르다.
# 폴더 이름을 추측하지 말 것. 아래 명령으로 나온 경로를 그대로 복사한다.
#     python agents\iferr\agent.py --folders
MAIL_FOLDER = r"받은 편지함\인터페이스"
MAIL_LOOKBACK_HOURS = 24

# 메일 제목에서 오류 메일을 고르는 키워드
# 항목이 하나여도 쉼표를 반드시 붙일 것. ("오류") 는 튜플이 아니라
# 문자열이라 "오", "류" 한 글자씩 비교하게 되어 '오전', '오더' 같은
# 제목이 전부 오류로 잡힌다. (config 가 자동 보정하고 경고도 하지만,
# 애초에 쉼표를 붙이는 편이 낫다.)
#
# 비교 방식 — 대소문자는 어느 모드에서나 무시한다.
#   contains   : 제목 어디에든 있으면 (기본)
#   startswith : 제목이 그 문구로 시작할 때만
#   regex      : 정규식
#
# 발신 시스템이 고정 머리말을 붙이는 경우 startswith 가 안전하다.
# "문의: (EAA) Alert Mail 설정 관련" 같은 메일이 걸리지 않는다.
MAIL_SUBJECT_MATCH = "startswith"
MAIL_SUBJECT_KEYWORDS = ("(EAA) Alert Mail",)

# 기본은 제목을 '있는 그대로' 비교한다. 즉 "RE: FW: (EAA) Alert Mail ..." 은
# 걸리지 않는다 — 시스템이 보낸 원본이 아니라 사람이 주고받은 사본이라
# 대개 중복이기 때문이다.
# 전달·회신된 알림까지 잡아야 하면 머리말을 넣는다. 걸린 건에는 (전달)
# 표시가 붙어 원본과 구별된다.
# MAIL_SUBJECT_STRIP_PREFIXES = ("RE:", "FW:", "FWD:", "답장:", "회신:", "전달:")

# --------------------------------------------------------------------------
# 인터페이스 키 추출
# --------------------------------------------------------------------------
# ID 가 "고정 접두어 + 숫자" 형태면 접두어만 적으면 된다. 정규식은
# 자동으로 만들어진다(경계 처리 포함).
#     EAIIF0001234 → IFERR_KEY_PREFIXES = ("EAIIF",)
IFERR_KEY_PREFIXES = ("EAIIF",)

# 형태가 다르면 정규식을 직접 넣는다. (규칙이름, 정규식)이며 그룹 1이 키다.
# 잘 되는지는 아래 명령으로 바로 확인할 수 있다.
#     python agents\iferr\agent.py --test-key "메일 제목이나 본문 붙여넣기"
# IFERR_KEY_PATTERNS = (
#     ("if-id-labeled", r"(?i)\bIF[_\-]?ID\s*[:=]\s*([A-Za-z0-9_\-]{3,40})"),
#     ("our-format",    r"연계번호\s*[:=]\s*([A-Z0-9]{8,})"),
# )

# --------------------------------------------------------------------------
# 조회 SQL — 바인드 변수 이름은 :if_key 로 고정이다.
# SELECT 만 넣을 것. 문자열 결합 금지(주입 위험).
# --------------------------------------------------------------------------
# 인터페이스 정의 마스터 테이블. 이름만 적으면 조회 SQL 이 자동으로
# 만들어진다 — SQL 을 쓸 필요가 없다.
IFERR_MASTER_TABLE = "IF_MST"

# 그 테이블의 컬럼 이름. id 는 필수(메일에서 뽑은 키와 비교할 컬럼).
# 나머지는 있는 것만 적는다. 빈 값은 SELECT 목록에서 빠진다.
IFERR_MASTER_FIELDS = {
    "id": "IFID",              # 인터페이스 ID
    "src_sys": "SRCSYS",       # 소스 시스템
    "tar_sys": "TARSYS",       # 타겟 시스템
    "src_table": "SRCTNAME",   # 소스 테이블
    "tar_table": "TARTNAME",   # 타겟 테이블
    # 스케줄. 하루 여러 번 도는 인터페이스는 행이 여러 개로 나오는데,
    # IFID 로 묶어 "매일 08:30, 12:00, 18:00" 처럼 한 줄로 합쳐 보여준다.
    "sch_day": "SCH_DAY",      # 일자 (매일이면 *)
    "sch_h": "SCH_H",          # 시
    "sch_m": "SCH_M",          # 분
}

# 자동 생성되는 SQL (참고)
#     SELECT IFID, SRCSYS, TARSYS, SRCTNAME, TARTNAME, SCH_DAY, SCH_H, SCH_M
#       FROM {schema}IF_MST
#      WHERE IFID = :if_key

# 자동 생성으로 부족하면 SQL 을 직접 쓴다. 이쪽이 우선한다.
# 바인드 이름은 :if_key 고정, {schema} 는 ORACLE_SCHEMA 로 치환된다.
# SELECT 만 넣을 것.
# IFERR_SQL = {
#     "header": "SELECT ... FROM {schema}IF_MST WHERE IFID = :if_key",
#     "detail": "SELECT ... FROM {schema}IF_LOG WHERE IFID = :if_key",
#     "impact": "",
# }


# --------------------------------------------------------------------------
# SQL 튜닝 (sqltune)
# --------------------------------------------------------------------------
# 사내 튜닝 기준이 따로 있으면 그 문서를 가리킨다.
# 규칙 엔진과 LLM 프롬프트가 같은 문서를 본다.
# SQLTUNE_RULES_FILE = r"D:\standards\oracle_tuning.md"

# 쿼리를 실제로 실행해 비교할 것인가. 기본 False — 실행은 곧 운영 DB 부하다.
# CLI 는 --run 으로 그때만 켠다. MCP 툴은 이 값과 무관하게 실행하지 않는다.
# SQLTUNE_EXECUTE = False
# SQLTUNE_MAX_ROWS = 100
# SQLTUNE_TIMEOUT_SEC = 60
# SQLTUNE_RUNS = 2

# 개선 후보를 만들어 원본과 비교할 것인가 (LLM 호출). CLI 는 --compare.
# SQLTUNE_COMPARE = False
# SQLTUNE_CANDIDATES = 2

# 결과 건수까지 비교할 것인가. 원본·후보를 각각 COUNT(*) 로 감싸 실행하므로
# DB 는 원본만큼 일한다. CLI 는 --count. 켜면 건수가 다른 후보는 탈락한다.
# SQLTUNE_COMPARE_COUNT = False
