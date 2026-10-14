"""[표로 보기] 저장본 — 답할 때의 그리드를 파일로 남긴다.

그리드 데이터는 MCP 서버 메모리에 30분만 있다. 앱을 다시 띄우거나 시간이 지난 뒤 지난 대화의
[표로 보기] 를 누르면 같은 인자로 다시 조사하는데, "오늘" 처럼 기간으로 물은 대화는 '지금의 오늘'
로 조사해 그때와 다른 표나 빈 표가 나왔다(사용자 지적). 그래서 답이 끝나자마자(서버 캐시에 결과가
있을 때) 그리드를 읽어 저장하고, 화면은 그 대화의 저장본을 먼저 연다.

    out/grid_snapshots/<32자리 id>.json   {"saved_at", "uri", "data"}

툴을 몰라도 된다 — view="grid" 이고 detail_uri 가 있는 툴 호출이면 저장한다.
"""

from __future__ import annotations

import json
import re
import time
import uuid
from datetime import datetime
from pathlib import Path
from typing import Any, Awaitable, Callable

import config
from app.mcp_bridge import fill_uri

_ID = re.compile(r"^[0-9a-f]{32}$")


def _dir() -> Path:
    return Path(config.GRID_SNAPSHOT_DIR)


def grid_uri(detail_uri: str, args: dict[str, Any]) -> str:
    """화면(gridUrl)과 같은 규칙으로 URI 를 채운다 — 모델이 안 넣은 인자는 '-'."""
    params = {}
    for name in re.findall(r"\{(\w+)\}", detail_uri):
        v = args.get(name)
        params[name] = "-" if v is None or str(v).strip() == "" else str(v)
    return fill_uri(detail_uri, **params)


def save(uri: str, text: str) -> str | None:
    """그리드 JSON 을 저장하고 id 를 돌려준다. 그리드가 아니면 저장하지 않는다."""
    try:
        data = json.loads(text)
    except ValueError:
        return None
    if not isinstance(data, dict) or not isinstance(data.get("grid"), dict):
        return None
    d = _dir()
    d.mkdir(parents=True, exist_ok=True)
    sid = uuid.uuid4().hex
    (d / f"{sid}.json").write_text(json.dumps(
        {"saved_at": datetime.now().isoformat(timespec="seconds"), "uri": uri, "data": data},
        ensure_ascii=False), encoding="utf-8")
    prune()
    return sid


def load(sid: str) -> dict[str, Any] | None:
    """저장본. id 모양이 아니거나 없으면 None — 경로를 바깥에서 받지 않는다."""
    if not _ID.match(sid or ""):
        return None
    p = _dir() / f"{sid}.json"
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def prune() -> int:
    """보관 기간(GRID_SNAPSHOT_DAYS)이 지난 저장본을 지운다. 지운 수."""
    d = _dir()
    if not d.is_dir():
        return 0
    limit = time.time() - config.GRID_SNAPSHOT_DAYS * 86400
    n = 0
    for p in d.glob("*.json"):
        try:
            if _ID.match(p.stem) and p.stat().st_mtime < limit:
                p.unlink()
                n += 1
        except OSError:
            continue
    return n


async def snapshot_calls(calls: list[dict[str, Any]], specs: dict[str, dict[str, Any]],
                         read: Callable[[str], Awaitable[str]]) -> None:
    """이번 턴의 툴 호출 중 그리드가 있는 것을 저장하고 calls 에 snapshot id 를 단다.

    저장에 실패해도 답은 그대로 간다 — 그때는 화면이 예전처럼 리소스를 다시 읽는다.
    """
    for call in calls:
        spec = specs.get(call.get("name", "")) or {}
        if spec.get("view") != "grid" or not spec.get("detail_uri"):
            continue
        uri = grid_uri(spec["detail_uri"], call.get("args") or {})
        try:
            sid = save(uri, await read(uri))
        except Exception:
            sid = None
        if sid:
            call["snapshot"] = sid
