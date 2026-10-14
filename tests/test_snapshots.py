"""[표로 보기] 저장본 — 앱을 다시 띄운 뒤에도 지난 대화에서 그때 그리드를 연다.

실행:
    pytest -q
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
import time
from pathlib import Path
from typing import Any

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import config  # noqa: E402
from app import snapshots  # noqa: E402

GRID = {"title": "T", "subtitle": "S", "grid": {"columns": [], "rows": [{"a": 1}]}, "warnings": []}
SPECS = {
    "check_interface_errors": {"view": "grid", "detail_uri": "iferr://impact/{period}/{key}/{subject}"},
    "find_usage": {"view": "text", "detail_uri": "usage://detail/{root}/{name}"},
}


@pytest.fixture(autouse=True)
def snap_dir(tmp_path: Path, monkeypatch: Any) -> Path:
    monkeypatch.setattr(config, "GRID_SNAPSHOT_DIR", str(tmp_path / "snap"))
    return tmp_path / "snap"


def test_grid_uri_fills_missing_args_like_the_screen():
    """화면(gridUrl)과 같은 규칙 — 모델이 안 넣은 인자는 '-'. 값은 퍼센트 인코딩."""
    uri = snapshots.grid_uri("iferr://impact/{period}/{key}/{subject}", {"period": "오늘", "key": ""})
    assert uri == "iferr://impact/%EC%98%A4%EB%8A%98/-/-"


def test_save_and_load_roundtrip(snap_dir: Path):
    sid = snapshots.save("x://y", json.dumps(GRID))
    assert sid and (snap_dir / f"{sid}.json").is_file()
    snap = snapshots.load(sid)
    assert snap["data"] == GRID and snap["uri"] == "x://y" and snap["saved_at"]


def test_only_grids_are_saved():
    assert snapshots.save("x://y", "not json") is None
    assert snapshots.save("x://y", json.dumps({"title": "그리드 아님"})) is None


def test_load_takes_only_id_shaped_names(snap_dir: Path):
    """경로를 바깥에서 받지 않는다 — '../config_local' 같은 값은 열지 않는다."""
    snap_dir.mkdir(parents=True)
    (snap_dir.parent / "secret.json").write_text('{"data": 1}', encoding="utf-8")
    for bad in ("../secret", "..\\secret", "", "ABC", "0" * 31):
        assert snapshots.load(bad) is None


def test_old_snapshots_are_pruned(snap_dir: Path, monkeypatch: Any):
    monkeypatch.setattr(config, "GRID_SNAPSHOT_DAYS", 30)
    old = snapshots.save("x://old", json.dumps(GRID))
    past = time.time() - 31 * 86400
    os.utime(snap_dir / f"{old}.json", (past, past))
    new = snapshots.save("x://new", json.dumps(GRID))
    assert snapshots.load(old) is None and snapshots.load(new)


def test_snapshot_calls_marks_grid_tools_only():
    read_uris: list[str] = []

    async def read(uri: str) -> str:
        read_uris.append(uri)
        return json.dumps(GRID)

    calls = [{"name": "check_interface_errors", "args": {"period": "오늘"}},
             {"name": "find_usage", "args": {"name": "X"}}]
    asyncio.run(snapshots.snapshot_calls(calls, SPECS, read))
    assert calls[0]["snapshot"] and "snapshot" not in calls[1]
    assert read_uris == ["iferr://impact/%EC%98%A4%EB%8A%98/-/-"]


def test_snapshot_failure_does_not_break_the_answer():
    async def read(uri: str) -> str:
        raise TimeoutError("느림")

    calls = [{"name": "check_interface_errors", "args": {}}]
    asyncio.run(snapshots.snapshot_calls(calls, SPECS, read))
    assert "snapshot" not in calls[0]


def test_snapshot_endpoint():
    from app.app import app

    sid = snapshots.save("x://y", json.dumps(GRID))
    c = app.test_client()
    r = c.get(f"/api/snapshot/{sid}")
    assert r.status_code == 200 and r.get_json()["grid"] == GRID["grid"] and r.get_json()["_saved_at"]
    assert c.get("/api/snapshot/" + "0" * 32).status_code == 404
    # 거르기 버튼 순서는 tones 의 키 순서다 — 응답이 키를 정렬하면 안 된다.
    tones = {"영향 있음": "bad", "확인 필요": "warn", "영향 없음": "muted"}
    sid2 = snapshots.save("x://z", json.dumps({**GRID, "grid": {**GRID["grid"], "tones": tones}},
                                              ensure_ascii=False))
    body = c.get(f"/api/snapshot/{sid2}").get_data(as_text=True)
    assert body.index("영향 있음") < body.index("확인 필요") < body.index("영향 없음")
    assert c.get("/api/snapshot/..%2Fsecret").status_code == 404
