"""시스템 점검 MCP 툴 — 설정과 연결 상태 확인.

전부 tier="step" 이다. 챗봇 대화에 필요한 툴이 아니라 사람이 문제를
진단할 때 쓰는 툴이라, 로컬 소형 모델에 보여 주면 엉뚱하게 부른다.

'왜 결과가 비었나'를 가르는 첫 단계다. 설정이 다른지, LLM 이 죽었는지,
경로가 틀렸는지를 여기서 먼저 확인한다.
"""

from __future__ import annotations

import config

from . import register


@register(
    label="현재 설정 보기",
    view="text",
    hint="describe_settings 는 서버가 어떤 설정으로 떠 있는지 확인할 때 쓴다.",
    read_only=True,
    tier="step",
)
def describe_settings() -> str:
    """MCP 서버가 실제로 보고 있는 설정을 출력한다.

    앱과 툴이 서로 다른 설정을 보는 사고를 진단할 때 사용한다.
    비밀번호는 값 대신 설정 여부만 나온다.
    """
    # config.describe() 를 그대로 쓴다. 여기서 따로 포맷을 만들면
    # 기동 로그와 이 툴의 출력이 달라져 진단이 더 어려워진다.
    return config.describe()


@register(
    label="LLM 연결 확인",
    view="text",
    hint="check_llm_status 는 Ollama 연결이나 모델 누락을 진단할 때 쓴다.",
    read_only=True,
    tier="step",
)
def check_llm_status() -> str:
    """Ollama 연결과 모델 존재를 확인한다.

    에이전트 결과에 'LLM 호출 실패'가 보일 때 원인을 가른다.
    """
    if not config.USE_LLM:
        return "USE_LLM=false — LLM 없이 규칙만으로 동작 중이다"

    # import 를 함수 안에서 한다. langchain_ollama 가 없는 환경에서도
    # 다른 툴들은 정상 동작해야 한다.
    from core.llm import check_ollama

    lines = []
    for label, model in (("CHAT_MODEL", config.CHAT_MODEL),
                         ("JUDGE_MODEL", config.JUDGE_MODEL)):
        ok, msg = check_ollama(model)
        lines.append(f"{label}({model}): {'OK' if ok else '실패'} — {msg}")
    return "\n".join(lines)


@register(
    label="DB 연결 확인",
    view="text",
    hint="check_db_status 는 Oracle 접속 문제를 진단할 때 쓴다.",
    read_only=True,
    tier="step",
)
def check_db_status() -> str:
    """Oracle 접속과 계정을 확인한다.

    조회 결과가 '확인 불가'로 나올 때 설정 문제인지 연결 문제인지 가른다.
    """
    from core.oracle import check_connection

    ok, msg = check_connection()
    lines = [("OK — " if ok else "실패 — ") + msg]
    configured = [k for k, v in config.IFERR_SQL.items() if v.strip()]
    lines.append(
        "조회 SQL: " + (", ".join(configured) or "(미설정 — config_local.py 의 IFERR_SQL)")
    )
    return "\n".join(lines)
