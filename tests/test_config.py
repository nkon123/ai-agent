"""config_local.py 덮어쓰기 회귀 테스트.

사내 PC 는 pull 만 한다. 추적되는 config.py 를 고치면 pull 이 충돌하므로
사내 값은 config_local.py 에 두고 덮어쓴다. 그 규칙을 고정한다.

실제 config_local.py 를 만들지 않고 순수 함수 _merge_overrides 를 직접
검증한다 — 테스트가 저장소 루트에 파일을 만들면 개발 PC 설정을 덮어쓴다.

실행:
    pytest -q
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import config  # noqa: E402


def test_local_overrides_defaults():
    target = {"USE_LLM": True, "ORACLE_DSN": None}
    applied, unknown = config._merge_overrides(
        target, {"ORACLE_DSN": "dbhost:1521/ORCL"}, env={}
    )
    assert target["ORACLE_DSN"] == "dbhost:1521/ORCL"
    assert applied == ["ORACLE_DSN"] and unknown == []


def test_env_beats_local():
    """환경변수가 config_local.py 를 이긴다.

    USE_LLM=false 처럼 한 번만 다르게 실행하는 경우를 막으면 안 된다.
    """
    target = {"USE_LLM": True}
    applied, _ = config._merge_overrides(
        target, {"USE_LLM": True}, env={"USE_LLM": "false"}
    )
    assert applied == []          # 환경변수가 있으므로 덮어쓰지 않았다
    assert target["USE_LLM"] is True   # 값은 _env_bool 이 이미 처리한다


def test_empty_env_does_not_block_local():
    """빈 환경변수는 '설정 안 함'으로 본다(SET VAR= 로 지운 경우)."""
    target = {"OLLAMA_HOST": "http://localhost:11434"}
    config._merge_overrides(
        target, {"OLLAMA_HOST": "http://ollama:11434"}, env={"OLLAMA_HOST": ""}
    )
    assert target["OLLAMA_HOST"] == "http://ollama:11434"


def test_unknown_name_is_reported_not_silently_ignored():
    """오타는 드러나야 한다.

    ORACLE_DNS 처럼 잘못 쓰면 조용히 무시되어 '설정했는데 왜 안 되지'로
    한참 헤매게 된다.
    """
    target = {"ORACLE_DSN": None}
    applied, unknown = config._merge_overrides(
        target, {"ORACLE_DNS": "typo"}, env={}
    )
    assert applied == [] and unknown == ["ORACLE_DNS"]
    assert target["ORACLE_DSN"] is None


def test_modules_and_functions_are_skipped():
    """config_local.py 안의 import 나 헬퍼 함수까지 설정으로 보지 않는다."""
    import os as os_module

    target = {"USE_LLM": True}
    applied, unknown = config._merge_overrides(
        target,
        {"os": os_module, "_helper": lambda: 1, "helper": lambda: 1, "USE_LLM": False},
        env={},
    )
    assert applied == ["USE_LLM"] and unknown == []


def test_derived_url_follows_overridden_port():
    """파생값은 덮어쓰기가 끝난 뒤에 조립되어야 한다.

    config_local.py 가 포트만 바꿨는데 URL 이 기본 포트를 가리키면
    '설정했는데 다른 곳에 붙는' 사고가 난다.
    """
    assert config.MCP_SERVER_URL.endswith(f":{config.MCP_HTTP_PORT}/mcp")


def test_describe_shows_config_source():
    """어떤 값이 어디서 왔는지 기동 로그에 남아야 진단이 된다."""
    assert "config_local" in config.describe()
