"""Oracle 연결·조회 헬퍼.

사용법:
    from core.oracle import get_conn, query

    rows = query("SELECT * FROM emp WHERE deptno = :dno", {"dno": 10})

    with get_conn() as conn:          # 여러 쿼리를 한 연결로 묶을 때
        ...

규칙:
    - SQL 은 반드시 바인드 변수를 쓴다. 문자열 결합 금지(주입 위험).
    - oracledb 미설치나 접속 실패는 명확한 예외로 던진다.
      조용히 None 을 돌려주면 호출부가 '결과 없음'과 구분하지 못한다.
    - DDL/DML 은 이 모듈에서 실행하지 않는다. 생성한 SQL 을 사람에게
      보여주고 사람이 실행한다(안전 규칙 4-8).
"""

from __future__ import annotations

import contextlib
import re
import sys
import threading
from typing import Any, Iterator, Mapping, Sequence

from config import (
    DB_TIMEOUT_SEC,
    ORACLE_CLIENT_LIB_DIR,
    ORACLE_DSN,
    ORACLE_PASSWORD,
    ORACLE_SCHEMA,
    ORACLE_THICK_MODE,
    ORACLE_USER,
)


class OracleUnavailable(RuntimeError):
    """드라이버 미설치 / 설정 누락 / 접속 실패를 한 종류로 묶는다.

    호출부는 '쿼리 결과가 없다'와 'DB 를 쓸 수 없다'를 반드시 구분해야
    한다. 후자는 예외로만 전달한다.
    """


def is_configured() -> bool:
    """DB 설정이 갖춰졌는지. 에이전트가 기능을 켤지 결정할 때 쓴다."""
    return bool(ORACLE_DSN and ORACLE_USER and ORACLE_PASSWORD)


def _import_driver():
    try:
        import oracledb  # type: ignore
    except ImportError as e:
        raise OracleUnavailable(
            "oracledb 패키지가 설치되어 있지 않다. "
            "pip install oracledb 후 다시 시도할 것."
        ) from e
    return oracledb


# thick 모드 전환은 프로세스 전역이고 한 번만 할 수 있다.
# 여러 스레드가 동시에 붙을 수 있으므로(Flask, MCP 브리지) 락으로 감싼다.
_client_lock = threading.Lock()
_client_mode = "thin"


def client_mode() -> str:
    """현재 드라이버 모드. 진단 화면에 보여 준다."""
    return _client_mode


def _enable_thick_mode(oracledb: Any) -> None:
    """Oracle Client 라이브러리를 적재해 thick 모드로 바꾼다.

    lib_dir 이 비어 있으면 PATH·레지스트리에서 찾는다. 사내 PC 에 이미
    클라이언트가 깔려 있으면(SQL Developer, Toad 등) 대개 그것으로 된다.
    """
    global _client_mode
    with _client_lock:
        if _client_mode == "thick":
            return
        try:
            oracledb.init_oracle_client(lib_dir=ORACLE_CLIENT_LIB_DIR or None)
        except Exception as e:
            raise OracleUnavailable(
                "Oracle Client 라이브러리를 적재하지 못해 thick 모드로 전환할 수 없다: "
                f"{e}\n"
                "  - Instant Client 를 설치하고 config_local.py 에 "
                'ORACLE_CLIENT_LIB_DIR = r"C:\\oracle\\instantclient_21_13" 지정\n'
                "  - 32/64비트가 파이썬과 같아야 한다"
            ) from e
        _client_mode = "thick"


@contextlib.contextmanager
def get_conn() -> Iterator[Any]:
    """연결 컨텍스트 매니저. 블록을 벗어나면 반드시 닫는다."""
    if not is_configured():
        raise OracleUnavailable(
            "Oracle 설정이 비어 있다. config.py 의 ORACLE_DSN / ORACLE_USER / "
            "ORACLE_PASSWORD 를 채우거나 동명의 환경변수를 설정할 것."
        )
    oracledb = _import_driver()
    if ORACLE_THICK_MODE:
        _enable_thick_mode(oracledb)

    def _connect():
        return oracledb.connect(
            user=ORACLE_USER, password=ORACLE_PASSWORD, dsn=ORACLE_DSN
        )

    try:
        conn = _connect()
    except Exception as e:
        # DPY-3010 = 이 서버 버전은 thin 모드가 지원하지 않는다(대개 12.1 미만).
        # 설정을 고치라고 안내만 하면 한 번 더 왕복해야 하니, Oracle Client 가
        # 있으면 그 자리에서 thick 모드로 바꿔 한 번 재시도한다.
        if "DPY-3010" in str(e) and _client_mode != "thick":
            print(
                "[oracle] 서버가 thin 모드를 지원하지 않는다(DPY-3010). "
                "thick 모드로 전환해 재시도한다.",
                file=sys.stderr,
            )
            _enable_thick_mode(oracledb)   # 실패하면 안내와 함께 예외
            try:
                conn = _connect()
            except Exception as e2:
                raise OracleUnavailable(
                    f"Oracle 접속 실패 (thick 모드, dsn={ORACLE_DSN}, "
                    f"user={ORACLE_USER}): {e2}"
                ) from e2
        else:
            # 예외 메시지에 비밀번호가 섞이지 않도록 DSN/USER 만 남긴다.
            raise OracleUnavailable(
                f"Oracle 접속 실패 (dsn={ORACLE_DSN}, user={ORACLE_USER}): {e}"
            ) from e
    try:
        yield conn
    finally:
        with contextlib.suppress(Exception):
            conn.close()


def check_connection(timeout: int | None = None) -> tuple[bool, str]:
    """연결과 계정을 확인한다. (성공여부, 메시지)

    예외로 던지지 않고 튜플로 돌려주는 이유: 진단 화면은 실패해도 계속
    진행되어야 한다. 실패 원인은 메시지에 그대로 담는다.
    """
    if not is_configured():
        return False, (
            "Oracle 설정이 비어 있다 — config_local.py 의 "
            "ORACLE_DSN / ORACLE_USER / ORACLE_PASSWORD 를 채울 것"
        )
    try:
        rows = query(
            "SELECT USER AS db_user, SYSDATE AS now FROM dual", timeout=timeout
        )
    except Exception as e:
        return False, f"{type(e).__name__}: {e}"
    if not rows:
        return False, "연결은 됐으나 dual 조회 결과가 비었다 — 확인 필요"
    return True, (
        f"접속 OK (dsn={ORACLE_DSN}, 접속계정={rows[0].get('DB_USER')}, "
        f"모드={client_mode()}, 서버시각={rows[0].get('NOW')})"
    )


# 데이터 딕셔너리에서 온 식별자만 허용하는 패턴.
# 테이블·컬럼 이름은 바인드 변수로 넘길 수 없어 문자열에 넣어야 하는데,
# 그 값이 사용자 입력이면 주입 위험이 생긴다. 그래서 (1) 이름은 반드시
# 데이터 딕셔너리 조회 결과에서만 가져오고 (2) 그 결과도 이 패턴으로
# 다시 검증한 뒤 큰따옴표로 감싼다.
_IDENT = re.compile(r"^[A-Za-z][A-Za-z0-9_$#]{0,29}$")


def _quote_ident(name: str) -> str:
    """식별자를 검증하고 큰따옴표로 감싼다. 아니면 예외."""
    if not _IDENT.match(name or ""):
        raise ValueError(f"식별자로 쓸 수 없는 이름: {name!r}")
    return '"' + name.upper() + '"'


def find_columns(
    name_like: str, schema: str | None = None, limit: int = 200
) -> list[dict[str, Any]]:
    """이름 조각으로 테이블·컬럼을 찾는다. 스키마를 모를 때 쓴다.

    값이 아니라 '이름'으로 찾는다. 예: find_columns("EAI") →
    이름에 EAI 가 들어간 테이블이나 컬럼.
    """
    owner = (schema or ORACLE_SCHEMA or ORACLE_USER or "").upper()
    return query(
        """
        SELECT owner, table_name, column_name, data_type, data_length
          FROM all_tab_columns
         WHERE owner = :owner
           AND (UPPER(column_name) LIKE :pat OR UPPER(table_name) LIKE :pat)
         ORDER BY table_name, column_id
        """,
        {"owner": owner, "pat": f"%{name_like.upper()}%"},
    )[:limit]


def find_value(
    value: str,
    name_like: str = "IF",
    schema: str | None = None,
    max_tables: int = 60,
    timeout: int | None = None,
) -> list[dict[str, Any]]:
    """특정 값이 들어 있는 테이블·컬럼을 찾는다. 스키마를 모를 때의 마지막 수단.

    이름에 name_like 가 들어간 문자형 컬럼만 뒤진다. 전수 조사는 사내
    DB 에서 몇 분씩 걸리고 부하도 크므로 후보를 좁히고 상한(max_tables)을 둔다.

    값은 바인드로 넘긴다. 테이블·컬럼 이름만 문자열에 들어가는데,
    그 이름은 데이터 딕셔너리에서 온 것을 _quote_ident 로 다시 검증한다.
    """
    owner = (schema or ORACLE_SCHEMA or ORACLE_USER or "").upper()
    candidates = query(
        """
        SELECT owner, table_name, column_name
          FROM all_tab_columns
         WHERE owner = :owner
           AND data_type IN ('VARCHAR2', 'CHAR', 'NVARCHAR2')
           AND UPPER(column_name) LIKE :pat
         ORDER BY table_name, column_name
        """,
        {"owner": owner, "pat": f"%{name_like.upper()}%"},
    )

    hits: list[dict[str, Any]] = []
    if len(candidates) > max_tables:
        # 후보를 잘랐다는 사실을 결과에 남긴다. 조용히 자르면 '없다'로
        # 읽히는데, 실제로는 안 본 컬럼에 있을 수 있다.
        hits.append(
            {
                "table": "(안내)",
                "column": "",
                "count": None,
                "error": (
                    f"후보 {len(candidates)}개 중 {max_tables}개만 확인했다 — "
                    "--max-tables 를 늘리거나 --like 로 범위를 좁힐 것"
                ),
            }
        )
    for c in candidates[:max_tables]:
        tab = f"{_quote_ident(c['OWNER'])}.{_quote_ident(c['TABLE_NAME'])}"
        col = _quote_ident(c["COLUMN_NAME"])
        try:
            rows = query(
                f"SELECT COUNT(*) AS cnt FROM {tab} WHERE {col} = :v",
                {"v": value},
                timeout=timeout,
            )
        except Exception as e:
            # 권한 없는 테이블 하나 때문에 탐색 전체가 멈추면 안 된다.
            hits.append(
                {
                    "table": c["TABLE_NAME"],
                    "column": c["COLUMN_NAME"],
                    "count": None,
                    "error": f"{type(e).__name__}: {e}",
                }
            )
            continue
        cnt = rows[0]["CNT"] if rows else 0
        if cnt:
            hits.append(
                {"table": c["TABLE_NAME"], "column": c["COLUMN_NAME"], "count": cnt}
            )
    return hits


def schema_prefix() -> str:
    """SQL 의 {schema} 자리에 넣을 접두어. 미설정이면 빈 문자열.

    식별자는 바인드로 넘길 수 없어 문자열에 들어가므로 반드시 검증한다.
    """
    if not ORACLE_SCHEMA:
        return ""
    return _quote_ident(ORACLE_SCHEMA) + "."


def render_sql(sql: str) -> str:
    """SQL 템플릿의 {schema} 를 치환한다. 값은 절대 치환하지 않는다."""
    return sql.replace("{schema}", schema_prefix())


def query(
    sql: str,
    binds: Mapping[str, Any] | Sequence[Any] | None = None,
    timeout: int | None = None,
) -> list[dict[str, Any]]:
    """SELECT 실행 후 dict 리스트로 반환.

    binds 는 필수는 아니지만(파라미터 없는 쿼리도 있으므로),
    값을 SQL 문자열에 끼워 넣는 것은 금지다. 반드시 binds 로 넘길 것.

    timeout: 초. 폐쇄망에서 DB 가 느려질 때 요청 스레드가 무한정
    잡히는 것을 막는다. 미지정 시 config.DB_TIMEOUT_SEC.
    """
    if "%s" in sql or "'{" in sql:
        # 흔한 문자열 결합 실수를 이른 단계에서 잡는다.
        raise ValueError("SQL 에 문자열 포맷 흔적이 있다. 바인드 변수를 사용할 것.")

    sql = render_sql(sql)

    with get_conn() as conn:
        # call_timeout 은 밀리초 단위다.
        conn.call_timeout = int((timeout or DB_TIMEOUT_SEC) * 1000)
        cur = conn.cursor()
        try:
            cur.execute(sql, binds or {})
            if cur.description is None:  # SELECT 가 아닌 경우
                return []
            cols = [d[0] for d in cur.description]
            return [dict(zip(cols, row)) for row in cur.fetchall()]
        finally:
            with contextlib.suppress(Exception):
                cur.close()
