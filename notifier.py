"""Builds and sends the email listing newly found seats."""

from __future__ import annotations

import os
import smtplib
import ssl
from email.message import EmailMessage
from html import escape
from pathlib import Path


def load_env(path: Path) -> None:
    """Read KEY=VALUE lines into os.environ without overriding real env vars."""
    if not path.exists():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


def _group_line(group) -> str:
    numbers = [s.number for s in group.seats]
    if len(numbers) > 2 and numbers == list(range(numbers[0], numbers[-1] + 1)):
        seats = f"{numbers[0]}–{numbers[-1]}"
    else:
        seats = ", ".join(map(str, numbers))
    price = f" — {group.seats[0].price}" if group.seats[0].price else ""
    return f"{group.section}, rząd {group.row}: miejsca {seats} ({len(group.seats)} obok siebie){price}"


def build_message(findings) -> tuple[str, str, str]:
    """findings: list of (Event, [SeatGroup]). Returns (subject, text, html)."""
    total = sum(len(groups) for _, groups in findings)
    subject = f"Teatr Muzyczny: wolne miejsca w pierwszych rzędach ({total})"

    text_parts, html_parts = [], []
    for event, groups in findings:
        header = f"{event.title} — {event.date:%d.%m.%Y} {event.time} ({event.stage})"
        text_parts.append(header)
        text_parts.extend("  • " + _group_line(g) for g in groups)
        text_parts.append(f"  Kup bilet: {event.ticket_url}\n")

        items = "".join(f"<li>{escape(_group_line(g))}</li>" for g in groups)
        html_parts.append(
            f"<h3 style='margin-bottom:4px'>{escape(header)}</h3>"
            f"<ul style='margin-top:0'>{items}</ul>"
            f"<p><a href='{escape(event.ticket_url)}'>Kup bilet</a></p>"
        )

    return subject, "\n".join(text_parts), "".join(html_parts)


def send_email(subject: str, text: str, html: str) -> None:
    user = os.environ["GMAIL_USER"]
    password = os.environ["GMAIL_APP_PASSWORD"]
    to = os.environ.get("MAIL_TO", user)

    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"] = user
    msg["To"] = to
    msg.set_content(text)
    msg.add_alternative(f"<html><body>{html}</body></html>", subtype="html")

    with smtplib.SMTP_SSL("smtp.gmail.com", 465, context=ssl.create_default_context(), timeout=30) as smtp:
        smtp.login(user, password)
        smtp.send_message(msg)
