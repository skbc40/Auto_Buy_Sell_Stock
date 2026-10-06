"""텔레그램 알림과 명령 수신. 토큰·chat_id 는 Windows 자격 증명 관리자(keyring)에 있다 — 코드/설정 파일에 두지 않는다."""
import logging
import queue
import threading
import time
from collections.abc import Callable

import keyring
import requests

SERVICE = "daytrader-telegram"
log = logging.getLogger("telegram")


def load_credentials() -> tuple[str, str] | None:
    token = keyring.get_password(SERVICE, "token")
    chat_id = keyring.get_password(SERVICE, "chat_id")
    return (token, chat_id) if token and chat_id else None


class Telegram:
    """보내기는 별도 스레드 큐로 처리해 매매 루프를 막지 않는다. on_command 가 있으면 명령도 받는다."""

    def __init__(self, token: str, chat_id: str, on_command: Callable[[str], str | None] | None = None) -> None:
        self.base = f"https://api.telegram.org/bot{token}"
        self.chat_id = str(chat_id)
        self.on_command = on_command
        self.q: queue.Queue[str] = queue.Queue()
        threading.Thread(target=self._sender, daemon=True).start()
        if on_command:
            threading.Thread(target=self._poller, daemon=True).start()

    def send(self, text: str) -> None:
        self.q.put(text)

    def _sender(self) -> None:
        while True:
            text = self.q.get()
            try:
                requests.post(f"{self.base}/sendMessage", json={"chat_id": self.chat_id, "text": text[:4000]},
                              timeout=10)
            except Exception as e:
                # 예외 메시지에는 토큰이 든 URL 이 들어갈 수 있어 종류만 남긴다
                log.warning("텔레그램 전송 실패: %s", type(e).__name__)

    def _get_updates(self, offset: int | None, timeout: int) -> list[dict]:
        params = {"timeout": timeout} | ({"offset": offset} if offset is not None else {})
        r = requests.get(f"{self.base}/getUpdates", params=params, timeout=timeout + 10)
        return r.json().get("result", [])

    def _poller(self) -> None:
        offset = None
        try:  # 프로그램이 꺼져 있던 동안 쌓인 옛 명령은 실행하지 않는다
            old = self._get_updates(-1, 0)
            offset = old[-1]["update_id"] + 1 if old else None
        except Exception as e:
            log.warning("텔레그램 초기화 실패: %s", type(e).__name__)
        while True:
            try:
                updates = self._get_updates(offset, 25)
            except Exception as e:
                log.warning("텔레그램 수신 실패 (5초 후 재시도): %s", type(e).__name__)
                time.sleep(5)
                continue
            for u in updates:
                offset = u["update_id"] + 1
                msg = u.get("message") or {}
                if str(msg.get("chat", {}).get("id")) != self.chat_id:
                    continue  # 등록된 내 채팅이 아니면 무시 (다른 사람이 봇을 조작하지 못하게)
                text = (msg.get("text") or "").strip()
                if not text:
                    continue
                try:
                    reply = self.on_command(text)
                except Exception as e:
                    reply = f"명령 처리 중 오류: {e}"
                if reply:
                    self.send(reply)


class NotifyHandler(logging.Handler):
    """extra=NOTIFY 로 남긴 로그와 ERROR 로그를 텔레그램으로 보낸다. 같은 문구는 60초에 한 번만."""

    def __init__(self, tg: Telegram) -> None:
        super().__init__(logging.INFO)
        self.tg = tg
        self._last: dict[str, float] = {}

    def emit(self, record: logging.LogRecord) -> None:
        if not (getattr(record, "notify", False) or record.levelno >= logging.ERROR):
            return
        text = record.getMessage()
        now = time.monotonic()
        if now - self._last.get(text, -1e9) < 60:
            return
        self._last[text] = now
        self.tg.send(("⚠️ " if record.levelno >= logging.ERROR else "") + text)
