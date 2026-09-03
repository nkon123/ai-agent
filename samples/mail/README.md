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
| `05_eaa_alert.eml` | `(EAA) Alert Mail` 머리말 — `startswith` 모드 |
| `06_eaa_forwarded.eml` | `RE: FW:` 가 붙은 사본 → 기본 설정에서는 빠져야 한다 |
| `07_mentions_eaa.eml` | 제목 중간에 머리말 언급 → `startswith` 에서는 빠져야 한다 |
| `08_prefix_id.eml` | "접두어 + 숫자" ID(`ABCIF0001234`). 본문의 `ABCIF0001234_TMP` 는 키가 아니다 |

```bash
MAIL_BACKEND=eml MAIL_SUBJECT_MATCH=startswith \
  MAIL_SUBJECT_KEYWORDS="(EAA) Alert Mail" \
  python agents/iferr/agent.py --mails
```
