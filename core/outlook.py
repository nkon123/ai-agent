"""Outlook 메일 읽기.

사용법:
    from core.outlook import read_mails

    for m in read_mails(since_hours=24):
        print(m.received, m.subject)

백엔드 (config.MAIL_BACKEND):
    com : 로컬 Outlook 데스크톱을 COM 으로 붙는다. 폐쇄망 기본값.
          Windows + Outlook 설치 + pywin32 필요.
    eml : 폴더에 저장된 .eml 파일을 읽는다. 의존성이 없어 테스트와
          리눅스 개발 PC 에서 쓴다.

읽기 전용이다. 회신·삭제·이동·읽음 표시 변경을 하지 않는다.
메일함을 건드리는 동작은 사람이 Outlook 에서 직접 한다(안전 규칙).
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime, timedelta
from email import message_from_bytes
from email.header import decode_header, make_header
from email.utils import parsedate_to_datetime
from pathlib import Path

from config import (
    MAIL_BACKEND,
    MAIL_EML_DIR,
    MAIL_FOLDER,
    MAIL_LOOKBACK_HOURS,
    MAIL_MAX_COUNT,
)
from core.text import ENCODINGS


class MailUnavailable(RuntimeError):
    """메일함에 접근할 수 없다.

    '메일이 없다'와 '메일을 못 읽었다'는 반드시 구분되어야 한다.
    후자는 예외로만 전달한다 — 조용히 빈 목록을 돌려주면 호출부가
    '오류 메일 없음'으로 오해한다. 누락은 오탐보다 나쁘다.
    """


@dataclass(frozen=True)
class Mail:
    """읽어 온 메일 한 통. 필요한 필드만 담는다."""

    subject: str
    body: str
    sender: str
    received: datetime | None
    source: str          # COM entry id 또는 파일 경로

    @property
    def sender_masked(self) -> str:
        """로그·근거에 남길 발신자. 계정을 그대로 남기지 않는다.

        개인정보·인증정보가 로그에 남지 않게 한다(안전 규칙).
        """
        return mask_address(self.sender)


def mask_address(addr: str) -> str:
    """hong.gildong@corp.com → ho***@corp.com"""
    if "@" not in addr:
        return addr[:2] + "***" if len(addr) > 2 else "***"
    local, _, domain = addr.partition("@")
    return f"{local[:2]}***@{domain}"


def read_mails(
    folder: str | None = None,
    since_hours: int | None = None,
    max_count: int | None = None,
    backend: str | None = None,
) -> list[Mail]:
    """최근 메일을 읽는다. 최신순.

    since_hours 로 기간을 좁히는 것이 중요하다. 사내 사서함은 수만 통이라
    전체를 순회하면 요청 하나가 몇 분씩 걸린다.
    """
    backend = (backend or MAIL_BACKEND).lower()
    folder = folder if folder is not None else MAIL_FOLDER
    since_hours = since_hours if since_hours is not None else MAIL_LOOKBACK_HOURS
    max_count = max_count or MAIL_MAX_COUNT
    since = datetime.now() - timedelta(hours=since_hours)

    if backend == "com":
        return _read_com(folder, since, max_count)
    if backend == "eml":
        return _read_eml(MAIL_EML_DIR, since, max_count)
    raise MailUnavailable(f"알 수 없는 MAIL_BACKEND: {backend} (com | eml)")


def list_folders(max_depth: int = 3) -> list[str]:
    r"""Outlook 의 폴더 경로 목록. config.MAIL_FOLDER 에 넣을 값을 찾는 용도다.

    사내 PC 에서 가장 먼저 막히는 곳이 폴더 이름이다. 한글 Outlook 은
    '받은 편지함', 영문은 'Inbox' 이고, 계정이 여러 개면 최상위 이름도
    다르다. 추측하지 말고 이 목록에서 그대로 복사해 쓸 것.

    깊이를 제한하는 이유: 폴더가 수백 개인 사서함에서 전체를 훑으면
    COM 왕복이 그만큼 일어나 몇 분씩 걸린다.
    """
    if MAIL_BACKEND.lower() != "com":
        raise MailUnavailable(
            f"폴더 목록은 com 백엔드에서만 의미가 있다 (현재 {MAIL_BACKEND}). "
            "eml 백엔드는 config.MAIL_EML_DIR 폴더의 .eml 파일을 읽는다."
        )
    try:
        import pythoncom  # type: ignore
        import win32com.client  # type: ignore
    except ImportError as e:
        raise MailUnavailable(
            "pywin32 가 설치되어 있지 않다. pip install pywin32 후 다시 시도할 것."
        ) from e

    pythoncom.CoInitialize()
    try:
        ns = win32com.client.Dispatch("Outlook.Application").GetNamespace("MAPI")
        out: list[str] = []

        def walk(folders, prefix: str, depth: int) -> None:
            if depth > max_depth:
                return
            for i in range(1, folders.Count + 1):
                try:
                    f = folders.Item(i)
                    path = f"{prefix}\\{f.Name}" if prefix else f.Name
                    # 메일 수를 함께 보여 주면 어느 폴더가 대상인지 바로 안다.
                    try:
                        count = f.Items.Count
                    except Exception:
                        count = -1
                    out.append(path if count < 0 else f"{path}  ({count}통)")
                    walk(f.Folders, path, depth + 1)
                except Exception:
                    # 접근 권한이 없는 폴더 하나 때문에 전체가 멈추면 안 된다.
                    continue

        walk(ns.Folders, "", 1)
        return out
    finally:
        pythoncom.CoUninitialize()


# --------------------------------------------------------------------------
# COM 백엔드 — 로컬 Outlook
# --------------------------------------------------------------------------


def _read_com(folder_path: str, since: datetime, max_count: int) -> list[Mail]:
    try:
        import pythoncom  # type: ignore
        import win32com.client  # type: ignore
    except ImportError as e:
        raise MailUnavailable(
            "pywin32 가 설치되어 있지 않다. pip install pywin32 후 다시 시도할 것. "
            "(Windows + Outlook 데스크톱이 설치된 PC 에서만 동작한다)"
        ) from e

    # COM 은 스레드마다 초기화가 필요하다. Flask 워커나 MCP 브리지 스레드에서
    # 부르면 초기화 없이 CoCreateInstance 가 실패한다 — 원인을 알기 어려운
    # 에러가 뜨므로 여기서 항상 초기화한다.
    pythoncom.CoInitialize()
    try:
        try:
            ns = win32com.client.Dispatch("Outlook.Application").GetNamespace("MAPI")
        except Exception as e:
            raise MailUnavailable(
                f"Outlook 에 연결하지 못했다: {e}. "
                "Outlook 이 실행 중인지, 같은 사용자 세션인지 확인할 것."
            ) from e

        target = _resolve_folder(ns, folder_path)
        items = target.Items
        # 정렬을 먼저 하고 Restrict 를 건다. 순서가 반대면 Restrict 결과가
        # 정렬되지 않아 상위 N 통을 잘라도 최신이 아니다.
        items.Sort("[ReceivedTime]", True)
        # 날짜 필터는 반드시 Restrict 로 건다. 파이썬에서 전부 돌며 거르면
        # 메일 한 통마다 COM 왕복이 일어나 수만 통에서 몇 분씩 걸린다.
        # 형식은 Outlook 로케일을 타므로 미국식으로 고정한다.
        query = "[ReceivedTime] >= '" + since.strftime("%m/%d/%Y %H:%M %p") + "'"
        try:
            items = items.Restrict(query)
        except Exception:
            # 로케일 때문에 Restrict 가 실패하면 필터 없이 진행하되,
            # 아래에서 파이썬으로 다시 거른다. 조용히 넘기지 않는다.
            pass

        mails: list[Mail] = []
        for item in items:
            if len(mails) >= max_count:
                break
            # 메일이 아닌 항목(회의 요청, 보고서 등)은 Subject/Body 가 없다.
            if getattr(item, "Class", 43) != 43:   # 43 = olMail
                continue
            received = _com_time(item)
            if received is not None and received < since:
                # 정렬이 최신순이므로 여기서 멈춰도 된다.
                break
            mails.append(
                Mail(
                    subject=str(getattr(item, "Subject", "") or ""),
                    body=str(getattr(item, "Body", "") or ""),
                    sender=str(getattr(item, "SenderEmailAddress", "") or ""),
                    received=received,
                    source=str(getattr(item, "EntryID", "") or ""),
                )
            )
        return mails
    finally:
        pythoncom.CoUninitialize()


def _resolve_folder(ns, folder_path: str):
    r"""'받은 편지함\인터페이스' 같은 경로를 폴더 객체로.

    빈 문자열이면 기본 받은 편지함(olFolderInbox = 6).
    """
    if not folder_path.strip():
        return ns.GetDefaultFolder(6)

    parts = [p for p in re.split(r"[\\/]", folder_path) if p.strip()]
    current = ns.GetDefaultFolder(6) if parts[0] in ("받은 편지함", "Inbox") else None
    rest = parts[1:] if current is not None else parts
    if current is None:
        current = ns.Folders.Item(rest.pop(0))

    for part in rest:
        try:
            current = current.Folders.Item(part)
        except Exception as e:
            raise MailUnavailable(
                f"Outlook 폴더를 찾지 못했다: '{folder_path}' 의 '{part}'. "
                "config.MAIL_FOLDER 를 확인할 것."
            ) from e
    return current


def _com_time(item) -> datetime | None:
    try:
        t = item.ReceivedTime
        # pywin32 의 시간 객체는 timezone 이 붙어 있어 naive 와 비교하면 터진다.
        return datetime(t.year, t.month, t.day, t.hour, t.minute, t.second)
    except Exception:
        return None


# --------------------------------------------------------------------------
# eml 백엔드 — 파일 폴더
# --------------------------------------------------------------------------


def _read_eml(dir_path: str, since: datetime, max_count: int) -> list[Mail]:
    base = Path(dir_path)
    if not base.is_dir():
        raise MailUnavailable(
            f"메일 폴더가 없다: {dir_path} (config.MAIL_EML_DIR 확인)"
        )

    mails: list[Mail] = []
    for path in sorted(base.glob("*.eml")):
        try:
            msg = message_from_bytes(path.read_bytes())
        except Exception as e:
            raise MailUnavailable(f"{path} 를 읽지 못했다: {e}") from e

        received = None
        if msg.get("Date"):
            try:
                received = parsedate_to_datetime(msg["Date"]).replace(tzinfo=None)
            except (TypeError, ValueError):
                received = None
        if received is not None and received < since:
            continue

        mails.append(
            Mail(
                subject=_decode_header(msg.get("Subject", "")),
                body=_eml_body(msg),
                sender=_decode_header(msg.get("From", "")),
                received=received,
                source=str(path),
            )
        )

    mails.sort(key=lambda m: m.received or datetime.min, reverse=True)
    return mails[:max_count]


def _decode_header(raw: str) -> str:
    """=?utf-8?B?...?= 형태의 인코딩된 헤더를 푼다. 한글 제목이 흔하다."""
    if not raw:
        return ""
    try:
        return str(make_header(decode_header(raw)))
    except Exception:
        return raw


def _eml_body(msg) -> str:
    """본문(text/plain)을 문자열로. 첨부와 HTML 파트는 건너뛴다."""
    parts = [msg] if not msg.is_multipart() else msg.walk()
    chunks: list[str] = []
    for part in parts:
        if part.get_content_type() != "text/plain":
            continue
        payload = part.get_payload(decode=True)
        if payload is None:
            continue
        charset = part.get_content_charset()
        # 선언된 charset 을 먼저 쓰되, 사내 메일은 cp949 인 경우가 흔하고
        # 선언이 틀린 경우도 있어 core.text 와 같은 순서로 폴백한다.
        for enc in ([charset] if charset else []) + list(ENCODINGS):
            try:
                chunks.append(payload.decode(enc))
                break
            except (UnicodeDecodeError, LookupError):
                continue
        else:
            chunks.append(payload.decode("utf-8", errors="replace"))
    return "\n".join(chunks)
