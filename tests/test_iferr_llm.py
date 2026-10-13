"""iferr 의 LLM 메일 판정 — 오류 메일인가, 인터페이스 ID 는 무엇인가.

실제 Ollama 를 부르지 않는다. `llm_judge_mail` 자리에 가짜를 끼워 LLM 이
맞게 답할 때·틀리게 답할 때·죽었을 때 에이전트가 어떻게 하는지를 고정한다.
모델이 실제로 얼마나 맞히는지는 테스트가 아니라 측정의 몫이다(README 참고).

실행:
    pytest -q tests/test_iferr_llm.py
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any, Callable

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from agents.iferr import agent as iferr  # noqa: E402
from core import outlook  # noqa: E402
from core.progress import listening  # noqa: E402

NO_KEYWORD_SUBJECT = "주문취소 전송 건"       # samples/mail/09 — 제목에 오류라는 말이 없다


@pytest.fixture(autouse=True)
def llm_mode(monkeypatch: Any) -> None:
    monkeypatch.setattr(outlook, "MAIL_BACKEND", "eml")
    monkeypatch.setattr(outlook, "MAIL_EML_DIR", "./samples/mail")
    monkeypatch.setattr(outlook, "MAIL_LOOKBACK_HOURS", 24 * 3650)
    monkeypatch.setattr(iferr, "USE_LLM", True)
    monkeypatch.setattr(iferr, "IFERR_MAIL_JUDGE", "llm")
    monkeypatch.setattr(iferr, "MAIL_SUBJECT_STRIP_PREFIXES", ())
    iferr._query_cached.cache.clear()


def fake_llm(monkeypatch: Any, answer: Callable[[Any], dict[str, Any]]) -> list[str]:
    """가짜 LLM 을 끼운다. 불린 메일의 제목을 순서대로 기록해 돌려준다."""
    called: list[str] = []

    def judge(mail: Any) -> dict[str, Any]:
        called.append(mail.subject)
        out = {"is_error": False, "interface_ids": [], "reason": "", "note": ""}
        out.update(answer(mail))
        return out

    monkeypatch.setattr(iferr, "llm_judge_mail", judge)
    return called


def sensible(mail: Any) -> dict[str, Any]:
    """메일을 제대로 읽은 LLM 처럼 답한다."""
    s = mail.subject
    if NO_KEYWORD_SUBJECT in s:
        return {"is_error": True, "interface_ids": ["IF_ORD_CANCEL"],
                "reason": "주문취소 인터페이스가 전송되지 못했다고 알린다"}
    if "연계오류" in s:
        return {"is_error": True, "interface_ids": ["IF_ORD_SEND"], "reason": "오류 알림"}
    if "결과 보고" in s or "문의" in s:
        return {"is_error": False, "reason": "정상 보고·문의다"}
    return {"is_error": True, "reason": "실패 알림"}


def row(result: dict[str, Any], needle: str) -> dict[str, Any]:
    return next(m for m in result["mails"] if needle in m["subject"])


# --------------------------------------------------------------------------
# LLM 이 키워드의 빈틈을 메운다
# --------------------------------------------------------------------------


def test_keyword_alone_misses_failure_without_keyword(monkeypatch: Any):
    """대조군: 키워드 방식은 '오류'라는 말이 없는 실패 알림(09)을 놓친다."""
    monkeypatch.setattr(iferr, "IFERR_MAIL_JUDGE", "keyword")
    assert row(iferr.list_mails(), NO_KEYWORD_SUBJECT)["is_error"] is False


def test_llm_catches_failure_without_keyword(monkeypatch: Any):
    fake_llm(monkeypatch, sensible)
    r = row(iferr.list_mails(), NO_KEYWORD_SUBJECT)
    assert r["is_error"] is True
    assert r["judged_by"] == "llm"                 # 키워드가 아니라 LLM 이 잡았다
    assert r["keys"] == ["IF_ORD_CANCEL"]
    assert "전송되지 못했다" in r["llm_reason"]     # 근거가 남는다


def test_combo_tool_reports_who_judged(monkeypatch: Any):
    fake_llm(monkeypatch, sensible)
    cases = {c["key"]: c for c in iferr.run_iferr(detail="summary")["cases"]}
    assert cases["IF_ORD_CANCEL"]["judged_by"] == "llm"
    # 키워드와 LLM 이 둘 다 오류로 봤고, 정규식과 LLM 이 같은 키를 냈다.
    assert cases["IF_ORD_SEND"]["judged_by"] == "rule+llm"
    assert cases["IF_ORD_SEND"]["found_by"] == "if-id-labeled+llm"


def test_combo_and_step_agree(monkeypatch: Any):
    """통합 툴과 단계 툴이 같은 판정을 쓴다 — 어느 쪽으로 불러도 키가 같다."""
    fake_llm(monkeypatch, sensible)
    step_keys = {k for m in iferr.list_mails()["mails"] if m["is_error"] for k in m["keys"]}
    combo_keys = {c["key"] for c in iferr.run_iferr(detail="summary")["cases"]}
    assert step_keys == combo_keys


# --------------------------------------------------------------------------
# LLM 이 지어낸 ID 는 쓰지 않는다
# --------------------------------------------------------------------------


def test_invented_id_is_dropped(monkeypatch: Any):
    def invents(mail: Any) -> dict[str, Any]:
        out = sensible(mail)
        if NO_KEYWORD_SUBJECT in mail.subject:
            out["interface_ids"] = ["IF_ORD_CANCEL", "IF_ORD_CANCEL_V2"]
        return out

    fake_llm(monkeypatch, invents)
    r = iferr.list_mails()
    assert row(r, NO_KEYWORD_SUBJECT)["keys"] == ["IF_ORD_CANCEL"]
    assert any("메일에 없는 것" in w and "IF_ORD_CANCEL_V2" in w for w in r["warnings"])


def test_truncated_id_is_not_accepted():
    """본문에는 ABCIF0001234_TMP 뿐 — 잘린 키로 DB 를 조회하면 엉뚱한 행을 집는다."""
    kept, rejected = iferr.verify_llm_keys("적재 테이블 ABCIF0001234_TMP 확인", ["ABCIF0001234"])
    assert kept == [] and rejected == ["ABCIF0001234"]


def test_key_follows_mail_spelling_and_keeps_evidence():
    kept, _ = iferr.verify_llm_keys("인터페이스(IF_ORD_CANCEL)가 전송되지", ["if_ord_cancel"])
    assert kept[0]["key"] == "IF_ORD_CANCEL"       # LLM 이 바꾼 대소문자가 아니라 메일 표기
    assert kept[0]["rule"] == "llm"
    assert "IF_ORD_CANCEL" in kept[0]["evidence"]


def test_sentence_fragments_are_not_ids():
    kept, rejected = iferr.verify_llm_keys("주문 취소 연계 실패", ["주문 취소", "", "  "])
    assert kept == [] and rejected == ["주문 취소"]


# --------------------------------------------------------------------------
# LLM 은 규칙을 뒤집지 않는다 / 실패해도 멈추지 않는다
# --------------------------------------------------------------------------


def test_llm_cannot_drop_keyword_hit(monkeypatch: Any):
    """키워드가 잡은 메일을 LLM 이 아니라고 해도 빼지 않는다. 누락이 더 나쁘다."""
    fake_llm(monkeypatch, lambda m: {"is_error": False, "reason": "단순 공지로 보인다"})
    r = iferr.list_mails()
    hit = row(r, "연계오류")
    assert hit["is_error"] is True and hit["conflict"] is True
    assert any("확인 필요" in w and "연계오류" in w for w in r["warnings"])


def test_llm_failure_falls_back_to_keyword(monkeypatch: Any):
    monkeypatch.setattr(iferr, "IFERR_MAIL_JUDGE", "keyword")
    expected = {m["subject"]: m["is_error"] for m in iferr.list_mails()["mails"]}

    monkeypatch.setattr(iferr, "IFERR_MAIL_JUDGE", "llm")

    def down(mail: Any) -> dict[str, Any]:
        raise ConnectionError("Ollama 연결 거부")

    monkeypatch.setattr(iferr, "llm_judge_mail", down)
    r = iferr.list_mails()
    assert {m["subject"]: m["is_error"] for m in r["mails"]} == expected
    assert any("LLM 판정 실패" in w and "확인 필요" in w for w in r["warnings"])


def test_use_llm_false_never_calls_llm(monkeypatch: Any):
    monkeypatch.setattr(iferr, "USE_LLM", False)

    def must_not_run(mail: Any) -> dict[str, Any]:
        raise AssertionError("USE_LLM=false 인데 LLM 을 불렀다")

    monkeypatch.setattr(iferr, "llm_judge_mail", must_not_run)
    r = iferr.list_mails()
    assert all(m["judged_by"] in ("rule", "") or not m["is_error"] for m in r["mails"])


# --------------------------------------------------------------------------
# 비용 — 사본은 보내지 않고, 상한을 넘으면 알린다
# --------------------------------------------------------------------------


def test_reply_copies_are_not_sent_to_llm(monkeypatch: Any):
    called = fake_llm(monkeypatch, sensible)
    iferr.list_mails()
    assert called and not [s for s in called if s.upper().startswith(("RE:", "FW:"))]


def test_cap_limits_llm_calls_and_says_so(monkeypatch: Any):
    monkeypatch.setattr(iferr, "IFERR_LLM_MAX_MAILS", 2)
    called = fake_llm(monkeypatch, sensible)
    r = iferr.list_mails()
    assert len(called) == 2
    assert any("키워드로만 판정" in w and "IFERR_LLM_MAX_MAILS=2" in w for w in r["warnings"])


def test_progress_is_reported_per_mail(monkeypatch: Any):
    fake_llm(monkeypatch, sensible)
    seen: list[str] = []
    with listening(seen.append):
        iferr.list_mails()
    assert any(t.startswith("메일 판정 중 1/") for t in seen)
