"""Transactional email: signup confirmation, the "you already have an account" note and invitation links.

These go out at once, from the request that asks for them, never through a job or a Delivery row: they carry a
one-time link, and job payloads and stored deliveries never hold secrets.

- `send` uses the installation's transport (SOFTWARDEN_SMTP_*, or the configured backend; locmem in tests) through
  notify/smtp.py's guarded backend, so a slow server can't hold a request past DEADLINE. Signup uses it: there is no
  organization yet.
- `send_for_org` uses the organization's own email channel when it has one configured (its Graph or SMTP settings,
  Notify/channels.send_email), else the installation's transport when SOFTWARDEN_EMAIL_CONFIGURED is set, else
  sends nothing. Invitations use it, in every edition.

A transactional provider would replace the installation transport here; it is blocked until
one is chosen. Nothing here logs an address, a subject or a body: a failure is logged by kind and exception class
only. Subjects lose control characters (an organization name with a line break can't break the header)."""

from __future__ import annotations

import html
import logging
import re

from django.conf import settings
from django.core.mail import EmailMessage

from .channels import DeliveryFailure, email_configured, installation_configured, send_email, smtp_connection
from .smtp import SmtpGuard

log = logging.getLogger("softwarden.email")

DEADLINE = 20.0  # seconds for the whole SMTP conversation: a person is waiting on the response
_CONTROL = re.compile(r"[\x00-\x1f\x7f]+")


def configured() -> bool:
    return installation_configured()


def clean_subject(subject: str) -> str:
    return _CONTROL.sub(" ", subject).strip()


def send(kind: str, to: str, subject: str, text: str) -> bool:
    """Send one plain-text message through the installation's transport; True when the transport accepted it. Never
    raises: the caller decides what a failure means (signup answers 503, an invitation still returns its link)."""
    if not configured():
        return False
    guard = SmtpGuard(restrict=False, deadline=DEADLINE)
    try:
        connection = smtp_connection(None, guard)
        message = EmailMessage(clean_subject(subject), text, settings.DEFAULT_FROM_EMAIL, [to], connection=connection)
        with guard:
            message.send()
    except Exception as error:  # the transport's failure, whatever it is; the request goes on
        log.warning("transactional email not sent kind=%s error=%s", kind, type(error).__name__)
        return False
    log.info("transactional email sent kind=%s", kind)
    return True


def send_for_org(org, kind: str, to: str, subject: str, text: str) -> bool:
    """Send through the organization's email channel (its own settings, or the installation default it falls back to;
    notify/channels.email_configured). False when it has none or the send failed. The message is never stored: the
    Delivery handed to the channel is an unsaved carrier for its fields."""
    from .models import Channel, Delivery, DeliveryKind

    if not email_configured(org):
        return False
    carrier = Delivery(
        organization=org,
        channel=Channel.EMAIL,
        kind=DeliveryKind.TEST,
        recipient=to,
        subject=clean_subject(subject),
        content={"text": text, "html": "<pre>" + html.escape(text) + "</pre>"},
    )
    try:
        send_email(org, carrier)
    except DeliveryFailure as error:
        log.warning("transactional email not sent kind=%s outcome=%s", kind, error.outcome)
        return False
    except Exception as error:  # a bug or an unexpected transport error: the link is still returned
        log.warning("transactional email not sent kind=%s error=%s", kind, type(error).__name__)
        return False
    log.info("transactional email sent kind=%s via=organization", kind)
    return True
