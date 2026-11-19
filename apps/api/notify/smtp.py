"""Django's SMTP backend with two guards:

- **A total deadline** (`SmtpGuard.deadline`, default 60 s) over the whole conversation, from connecting to QUIT. The
  per-operation timeout alone lets a server answer a byte every few seconds and hold the single worker for hours; when
  the deadline passes the socket is shut down at the OS level and the send fails (ambiguous once the message data was
  being written).
- **A checked, pinned address** for an organization's own SMTP host (`restrict=True`): the host is resolved once and
  refused when it is loopback, link-local (the metadata address included), unspecified, multicast or reserved, directly
  or inside an IPv6 address (core/outbound.smtp_address); private relays are allowed. The connection goes to the
  address that was checked, while TLS (STARTTLS or implicit) still verifies the certificate against the host name. The
  installation's SOFTWARDEN_SMTP_* host is the operator's: `restrict=False` connects as Django would.

The deadline covers the TCP connect and the server's greeting onwards; an implicit-TLS handshake is bounded by the
per-operation timeout (the socket isn't reachable before it completes)."""

from __future__ import annotations

import smtplib
from typing import Any

from django.core.mail.backends.smtp import EmailBackend

from core import outbound

DEADLINE = 60.0


class SmtpGuard:
    def __init__(self, restrict: bool, deadline: float | None = None):
        self.restrict = restrict
        self.deadline = DEADLINE if deadline is None else deadline
        self.client: smtplib.SMTP | None = None
        self.clock: outbound.Deadline | None = None

    @property
    def fired(self) -> bool:
        return self.clock is not None and self.clock.fired

    def address_for(self, host: str, port: int) -> str:
        return outbound.smtp_address(host, port) if self.restrict else host

    def kill(self) -> None:
        outbound.shutdown(getattr(self.client, "sock", None))

    def __enter__(self) -> SmtpGuard:
        self.clock = outbound.Deadline(self.deadline, self.kill).__enter__()
        return self

    def __exit__(self, *exc) -> None:
        if self.clock is not None:
            self.clock.__exit__(*exc)


class _Guarded:
    """Mixed into smtplib.SMTP / SMTP_SSL: registers itself with the guard and connects to the checked address.
    smtplib keeps `_host` (the name) for SNI and certificate checks."""

    _guard: SmtpGuard

    def __init__(self, *args: Any, **kwargs: Any):
        self._guard.client = self  # type: ignore[assignment]
        super().__init__(*args, **kwargs)

    def _get_socket(self, host, port, timeout):
        return super()._get_socket(self._guard.address_for(host, port), port, timeout)  # type: ignore[misc]


class GuardedEmailBackend(EmailBackend):
    """`get_connection("notify.smtp.GuardedEmailBackend", guard=SmtpGuard(...), ...)`.

    Django's backend reads a `None` user name or password as "use settings.EMAIL_HOST_USER / _PASSWORD". For an
    organization's own host (`guard.restrict`) that would send the installation's credentials to a server the
    organization chose, so a missing value is taken as blank (no sign-in) and the settings are never read. The
    installation's own connection (`restrict=False`) keeps Django's fallback to its settings."""

    def __init__(
        self,
        host: str | None = None,
        port: int | None = None,
        username: str | None = None,
        password: str | None = None,
        *args: Any,
        guard: SmtpGuard | None = None,
        **kwargs: Any,
    ):
        self.guard = guard or SmtpGuard(restrict=False)
        if self.guard.restrict:
            username, password = username or "", password or ""
            # The same fallback exists for the TLS client certificate: never present the installation's to them.
            kwargs["ssl_keyfile"], kwargs["ssl_certfile"] = (
                kwargs.get("ssl_keyfile") or "",
                kwargs.get("ssl_certfile") or "",
            )
        super().__init__(host, port, username, password, *args, **kwargs)

    @property
    def connection_class(self):
        base = smtplib.SMTP_SSL if self.use_ssl else smtplib.SMTP
        return type(f"Guarded{base.__name__}", (_Guarded, base), {"_guard": self.guard})
