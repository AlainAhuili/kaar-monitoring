"""Notification delivery. Runs in a thread pool so slow SMTP never blocks ingest."""

from __future__ import annotations

import json
import logging
import smtplib
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from email.message import EmailMessage

from .config import Config
from .rules import Transition

log = logging.getLogger("notify")


def format_message(t: Transition, site: str | None) -> tuple[str, str]:
    where = f"{t.device.name}" + (f" ({site})" if site else "")
    when = t.at.astimezone().strftime("%d.%m.%Y %H:%M")
    if t.kind == "offline":
        if t.action == "fire":
            return f"[OFFLINE] {where}", f"{where} sendet seit {when} keine Daten mehr."
        return f"[WIEDER ONLINE] {where}", f"{where} sendet seit {when} wieder Daten."
    r = t.rule
    assert r is not None
    if t.action == "fire":
        subj = f"[{r.severity.upper()}] {r.name} – {where}"
        body = (
            f"{r.name}: {r.metric} = {t.value:g} (Grenzwert {r.operator} {r.threshold:g}"
            + (f", seit mind. {r.duration_s // 60} min" if r.duration_s else "")
            + f").\nGerät: {where}\nZeit: {when}"
        )
    else:
        subj = f"[OK] {r.name} – {where}"
        body = (
            f"{r.name} ist behoben: {r.metric} = {t.value:g}.\n"
            f"Extremwert während der Störung: {t.peak:g}\nGerät: {where}\nZeit: {when}"
        )
    return subj, body


class Notifier:
    def __init__(self, cfg: Config) -> None:
        self.cfg = cfg
        self.pool = ThreadPoolExecutor(max_workers=4, thread_name_prefix="notify")

    def send(self, kind: str, target: str, subject: str, body: str, payload: dict) -> None:
        self.pool.submit(self._send_safe, kind, target, subject, body, payload)

    def shutdown(self) -> None:
        self.pool.shutdown(wait=True)

    def _send_safe(self, kind, target, subject, body, payload) -> None:
        try:
            getattr(self, f"_send_{kind}")(target, subject, body, payload)
            log.info("sent %s notification to %s: %s", kind, target, subject)
        except Exception:
            log.exception("failed to send %s notification to %s", kind, target)

    def _send_email(self, target, subject, body, _payload) -> None:
        if not self.cfg.smtp_host:
            log.warning("SMTP not configured, skipping email to %s", target)
            return
        msg = EmailMessage()
        msg["From"], msg["To"], msg["Subject"] = self.cfg.smtp_from, target, subject
        msg.set_content(body)
        with smtplib.SMTP(self.cfg.smtp_host, self.cfg.smtp_port, timeout=15) as s:
            s.starttls()
            if self.cfg.smtp_user:
                s.login(self.cfg.smtp_user, self.cfg.smtp_password)
            s.send_message(msg)

    def _send_telegram(self, chat_id, subject, body, _payload) -> None:
        if not self.cfg.telegram_bot_token:
            log.warning("TELEGRAM_BOT_TOKEN not set, skipping")
            return
        url = f"https://api.telegram.org/bot{self.cfg.telegram_bot_token}/sendMessage"
        data = urllib.parse.urlencode({"chat_id": chat_id, "text": f"{subject}\n\n{body}"}).encode()
        urllib.request.urlopen(url, data=data, timeout=15).read()

    def _send_webhook(self, url, _subject, _body, payload) -> None:
        if not url.startswith("https://"):
            raise ValueError("webhook target must be https")
        req = urllib.request.Request(  # noqa: S310 – scheme checked above
            url,
            data=json.dumps(payload).encode(),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        urllib.request.urlopen(req, timeout=15).read()  # noqa: S310
