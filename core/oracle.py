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
from typing import Any, Iterator, Mapping, Sequence

from config import (
    DB_TIMEOUT_SEC,
    ORACLE_DSN,
    ORACLE_PASSWORD,
    ORACLE_SCHEMA,
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


@contextlib.contextmanager
def get_conn() -> Iterator[Any]:
    """연결 컨텍스트 매니저. 블록을 벗어나면 반드시 닫는다."""
    if not is_configured():
        raise OracleUnavailable(
            "Oracle 설정이 비어 있다. config.py 의 ORACLE_DSN / ORACLE_USER / "
            "ORACLE_PASSWORD 를 채우거나 동명의 환경변수를 설정할 것."
        )
    oracledb = _import_driver()
    try:
        conn = oracledb.connect(
            user=ORACLE_USER, password=ORACLE_PASSWORD, dsn=ORACLE_DSN
        )
    except Exception as e:
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
        f"서버시각={rows[0].get('NOW')})"
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
