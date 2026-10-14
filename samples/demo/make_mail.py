"""발표·데모용 인터페이스 오류 메일을 '지금' 날짜로 만든다(가짜 값).

샘플 메일(samples/mail)은 날짜가 박혀 있어 "오늘 인터페이스 오류 확인해줘" 로는
잡히지 않는다. 데모 직전에 이 스크립트로 오늘 날짜 메일을 만든다.

    python samples/demo/make_mail.py            # out/demo_mail/ 에 만든다 (git 제외)

만드는 메일:
    1. IF_ORD_RECV 처리 실패 — ORA-12899 IF_ORDER_TMP.ORD_NO 길이 초과 (발표 시나리오)
    2. IF_STK_RECV 처리 실패 — timeout (원인 컬럼 없음 → 영향도 조사 안 함을 보여 준다)
    3. 정상 처리 보고 (오류 아님)
"""

from __future__ import annotations

import sys
from datetime import datetime, timedelta
from email.message import EmailMessage
from email.utils import format_datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
OUT = ROOT / "out" / "demo_mail"

MAILS = [
    (50, "[연계오류] 주문 수신 인터페이스 처리 실패", "if.monitor@partner.example",
     "안녕하세요.\n\n아래 인터페이스 처리 중 오류가 발생했습니다.\n\n"
     "  IF_ID : IF_ORD_RECV\n"
     "  발생시각 : {when}\n"
     "  처리건수 : 1,204건 중 37건 실패\n"
     "  오류내용 : ORA-12899: value too large for column \"ERP\".\"IF_ORDER_TMP\".\"ORD_NO\" "
     "(actual: 25, maximum: 20)\n\n"
     "주문번호 체계 변경(20자리 → 25자리) 이후 발생한 것으로 보입니다. 확인 부탁드립니다.\n"),
    (35, "[연계오류] 재고 수신 인터페이스 응답 없음", "batch.noreply@partner.example",
     "인터페이스 ID: IF_STK_RECV\n발생시각 : {when}\n오류: 상대 시스템 응답 timeout (30s)\n"),
    (20, "주간 연계 처리 결과 보고", "batch.noreply@partner.example",
     "이번 주 연계는 모두 정상 처리되었습니다. IF_ID : IF_ORD_SEND\n"),
]


def main() -> int:
    OUT.mkdir(parents=True, exist_ok=True)
    for old in OUT.glob("*.eml"):
        old.unlink()
    now = datetime.now().astimezone()
    for i, (minutes_ago, subject, sender, body) in enumerate(MAILS, start=1):
        when = now - timedelta(minutes=minutes_ago)
        msg = EmailMessage()
        msg["Subject"] = subject
        msg["From"] = sender
        msg["To"] = "erp.admin@corp.example"
        msg["Date"] = format_datetime(when)
        msg.set_content(body.format(when=when.strftime("%Y-%m-%d %H:%M:%S")))
        (OUT / f"{i:02d}_demo.eml").write_bytes(bytes(msg))
    print(f"데모 메일 {len(MAILS)}통 → {OUT}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
