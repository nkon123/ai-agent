# samples/mail

`iferr` 에이전트를 Outlook 없이 돌려 보기 위한 예제 메일.

```bash
MAIL_BACKEND=eml python agents/iferr/agent.py
```

| 파일 | 무엇을 보는가 |
|---|---|
| `01_order_fail.eml` | `IF_ID : IF_ORD_SEND` — 라벨 형태 키 추출 |
| `02_stock_error.eml` | **euc-kr 본문** + `인터페이스 ID:` 한글 라벨 |
| `03_no_key.eml` | 오류 메일인데 키가 없다 → "확인 필요"로 남아야 한다 |
| `04_normal.eml` | 키는 있지만 정상 처리 보고 → 대상이 아니어야 한다 |
