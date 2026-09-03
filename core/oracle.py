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
from typing import Any, Iterator, Mapping, Sequence

from config import DB_TIMEOUT_SEC, ORACLE_DSN, ORACLE_PASSWORD, ORACLE_USER


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
