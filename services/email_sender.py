"""Outgoing e-mail over SMTP using only the standard library.

Configuration (all from the environment, read at send time):
  SMTP_HOST, SMTP_PORT (default 587), SMTP_USERNAME, SMTP_PASSWORD, SMTP_FROM_EMAIL
  PUBLIC_BASE_URL  - origin used to build links in e-mails (e.g. https://connect.example.com)

Port 465 uses implicit TLS; every other port must support STARTTLS (a server that doesn't is refused
rather than sent credentials and links in clear text).

Failures raise EmailDeliveryError with a generic message. The underlying SMTP error is logged by
class name only, because SMTP exceptions can echo credentials or addresses.
"""
from __future__ import annotations

import logging
import os
import smtplib
import ssl
from email.message import EmailMessage

logger = logging.getLogger(__name__)

SMTP_TIMEOUT_SECONDS = 10


class EmailDeliveryError(Exception):
    """The message could not be handed to the SMTP server."""


def public_base_url(fallback: str = "") -> str:
    """Origin for links in e-mails. PUBLIC_BASE_URL wins; ``fallback`` is the request's own origin."""
    return (os.getenv("PUBLIC_BASE_URL", "").strip() or fallback).rstrip("/")


def smtp_configured() -> bool:
    return bool(os.getenv("SMTP_HOST", "").strip() and _from_address())


def _from_address() -> str:
    return os.getenv("SMTP_FROM_EMAIL", "").strip() or os.getenv("SMTP_USERNAME", "").strip()


def send_email(to: str, subject: str, body: str) -> None:
    if not smtp_configured():
        logger.error("E-mail not sent: SMTP_HOST / SMTP_FROM_EMAIL are not configured.")
        raise EmailDeliveryError("E-mail delivery is not configured.")

    message = EmailMessage()
    message["From"] = _from_address()
    message["To"] = to
    message["Subject"] = subject
    message.set_content(body)

    host = os.getenv("SMTP_HOST", "").strip()
    try:
        port = int(os.getenv("SMTP_PORT", "").strip() or "587")
    except ValueError:
        port = 587
    username = os.getenv("SMTP_USERNAME", "").strip()
    password = os.getenv("SMTP_PASSWORD", "")
    context = ssl.create_default_context()

    try:
        if port == 465:
            server = smtplib.SMTP_SSL(host, port, timeout=SMTP_TIMEOUT_SECONDS, context=context)
        else:
            server = smtplib.SMTP(host, port, timeout=SMTP_TIMEOUT_SECONDS)
        with server:
            if port != 465:
                server.starttls(context=context)
            if username:
                server.login(username, password)
            server.send_message(message)
    except (smtplib.SMTPException, OSError) as exc:
        logger.error("E-mail delivery failed (%s).", type(exc).__name__)
        raise EmailDeliveryError("E-mail delivery failed.") from exc


def send_verification_email(to: str, link: str, hours: int = 24) -> None:
    send_email(
        to,
        "Confirm your CONNECT email",
        "Welcome to CONNECT.\n\n"
        f"Confirm your email address to finish creating your account (this link works for {hours} hours):\n\n"
        f"{link}\n\n"
        "If you didn't create a CONNECT account, you can ignore this message.\n",
    )


def send_password_reset_email(to: str, link: str, hours: int = 1) -> None:
    send_email(
        to,
        "Reset your CONNECT password",
        "Someone asked to reset the password for your CONNECT account.\n\n"
        f"Choose a new password here (this link works for {hours} hour and only once):\n\n"
        f"{link}\n\n"
        "If this wasn't you, ignore this message; your password has not changed.\n",
    )
