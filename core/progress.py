"""에이전트가 진행 상황을 알리는 통로.

사용법:
    from core.progress import notify
    notify("SQL 문장 판정 중 3/8")

에이전트는 '누가 듣고 있는지' 몰라야 한다. CLI 로 돌리면 듣는 사람이 없고,
MCP 툴로 불리면 툴 껍데기가 받아서 MCP 진행 알림으로 바꿔 보낸다.
받을 곳이 없으면 조용히 버린다 — 진행 표시가 없다고 작업이 실패해서는 안 된다.

왜 contextvar 인가:
    같은 프로세스에서 여러 요청이 동시에 돌 수 있다. 전역 변수 하나로 두면
    남의 요청 진행이 내 화면에 섞인다. contextvar 는 요청마다 값이 따로
    잡히고, asyncio.to_thread 로 넘어가는 워커 스레드까지 복사된다.
"""

from __future__ import annotations

import contextvars
from typing import Any, Callable, Iterator

_SINK: contextvars.ContextVar[Callable[[str], None] | None] = contextvars.ContextVar(
    "agent_progress_sink", default=None
)


def notify(text: str) -> None:
    """진행 상황 한 줄. 듣는 곳이 없으면 아무 일도 일어나지 않는다."""
    sink = _SINK.get()
    if sink is None:
        return
    try:
        sink(text)
    except Exception:
        # 진행 표시가 본 작업을 망가뜨려서는 안 된다.
        pass


class listening:
    """with 블록 동안 진행 상황을 sink 로 받는다.

        with listening(queue.put):
            run_impact(...)
    """

    def __init__(self, sink: Callable[[str], None]) -> None:
        self._sink = sink
        self._token: Any = None

    def __enter__(self) -> "listening":
        self._token = _SINK.set(self._sink)
        return self

    def __exit__(self, *exc: Any) -> None:
        if self._token is not None:
            _SINK.reset(self._token)


def current_sink() -> Callable[[str], None] | None:
    """지금 듣고 있는 곳. 테스트에서 확인용."""
    return _SINK.get()
