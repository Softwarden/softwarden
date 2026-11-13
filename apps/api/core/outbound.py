"""Outbound connections to an address an Admin typed in: the SSRF guard.

Webhooks (a Teams Workflows webhook, a generic webhook):

- `check_url` (on save and before each send): https only, a host name or a public IP literal, no user name or password
  in the URL, no `localhost` or single-label names, at most 2,048 characters, a host that IDNA-encodes.
- `post` resolves the host **once**, refuses it unless every address it resolves to is public (`public_ip`), then
  connects to the address it checked (so a second DNS answer can't swap in an internal one: DNS rebinding) with TLS
  SNI, certificate verification and the Host header using the host name. Redirects are never followed; the answer's
  body is read up to 64 KB. The whole post has a wall-clock deadline (DEADLINE) on top of the per-operation timeout,
  so a server that trickles a byte at a time can't hold the worker: when it passes, the socket is shut down.

`public_ip` refuses private, loopback, link-local, multicast, reserved, unspecified and carrier-grade NAT addresses,
and every IPv6 form that embeds or translates an IPv4 address (IPv4-compatible `::/96`, SIIT `::ffff:0:0:0/96`, NAT64
`64:ff9b::/96` and `64:ff9b:1::/48`, 6to4 `2002::/16`, Teredo `2001::/32`); an IPv4-mapped address
(`::ffff:a.b.c.d`) is judged by its IPv4 address. On a NAT64/DNS64 network `64:ff9b::a00:1` reaches 10.0.0.1, so
those prefixes are refused outright.

Service base URLs an organization enters for a server that may run on its own network (a HaloITSM instance,
`check_service_url`; `post(..., private_ok=True)`) are https URLs judged by the SMTP rule below for their addresses:
private networks allowed; loopback, link-local (metadata), unspecified, multicast and reserved refused, on save for a
literal and on every address the name resolves to when connecting.

With SOFTWARDEN_ALLOW_PRIVATE_SERVICE_HOSTS off (a hosted, multi-organization install), service and SMTP hosts
may not be private either: RFC 1918, ULA (`fc00::/7`) and carrier-grade NAT (`100.64.0.0/10`) are refused too. Known
cloud metadata addresses (`CLOUD_METADATA`: AWS's IPv6 `fd00:ec2::254`, Alibaba's `100.100.100.200`; the link-local
169.254.169.254 is refused as link-local) are refused in both modes.

SMTP hosts an organization enters (`smtp_address`) are judged more loosely, because internal relays are normal:
private RFC 1918 and ULA addresses are allowed, but loopback, link-local (the cloud metadata address 169.254.169.254
included), unspecified, multicast and reserved addresses are refused, including when an IPv6 address embeds one. The
installation's own SOFTWARDEN_SMTP_* host is the operator's and isn't checked.

Errors are `UnsafeDestination` (nothing was sent) and core.graph.Unreachable (with `ambiguous` as there). Messages
never contain the URL: a webhook URL is a bearer secret."""

from __future__ import annotations

import http.client
import ipaddress
import socket
import ssl
import threading
import time
import urllib.parse
from collections.abc import Mapping
from typing import Any

from .graph import Reply, Unreachable

URL_MAX = 2048
TIMEOUT = 15  # per socket operation
DEADLINE = 30.0  # seconds for a whole post: connect, TLS, request, status, headers and body
BODY_MAX = 64 * 1024
BLOCKED_NAMES = ("localhost", ".localhost", ".local", ".internal", ".home.arpa")
IPAddress = ipaddress.IPv4Address | ipaddress.IPv6Address

# IPv6 prefixes that carry or translate an IPv4 address (other than IPv4-mapped, which is unwrapped).
_V6 = ipaddress.IPv6Network
TRANSLATED = (
    _V6("::/96"),  # IPv4-compatible (deprecated): ::a.b.c.d
    _V6("::ffff:0:0:0/96"),  # SIIT: ::ffff:0:a.b.c.d
    _V6("64:ff9b::/96"),  # NAT64 well-known prefix
    _V6("64:ff9b:1::/48"),  # NAT64 local-use prefix
    _V6("2002::/16"),  # 6to4: 2002:aabb:ccdd::
    _V6("2001::/32"),  # Teredo
)
# Deprecated site-local (RFC 3879): Python's `is_global` says True for it, but it was never globally routable.
SITE_LOCAL = _V6("fec0::/10")
ZERO_NET = ipaddress.IPv4Network("0.0.0.0/8")
CGNAT = ipaddress.IPv4Network("100.64.0.0/10")
ULA = _V6("fc00::/7")
# Cloud metadata services outside link-local, refused even where private networks are allowed.
CLOUD_METADATA = frozenset({ipaddress.ip_address("fd00:ec2::254"), ipaddress.ip_address("100.100.100.200")})


class UnsafeDestination(ValueError):
    """The address isn't allowed. The message is safe to show: it never repeats the URL."""


def embedded_ipv4(address: IPAddress) -> ipaddress.IPv4Address | None:
    """The IPv4 address an IPv6 address carries (mapped, compatible, SIIT, NAT64 /96, 6to4), or None."""
    if not isinstance(address, ipaddress.IPv6Address):
        return None
    if address.ipv4_mapped is not None:
        return address.ipv4_mapped
    value = int(address)
    for network in (TRANSLATED[0], TRANSLATED[1], TRANSLATED[2]):
        if address in network:
            return ipaddress.IPv4Address(value & 0xFFFFFFFF)
    if address in TRANSLATED[4]:
        return ipaddress.IPv4Address((value >> 80) & 0xFFFFFFFF)
    return None


def public_ip(address: IPAddress) -> bool:
    """A webhook may go here: a global unicast address, never an IPv6 form that embeds or translates IPv4."""
    if isinstance(address, ipaddress.IPv6Address):
        if address.ipv4_mapped is not None:
            address = address.ipv4_mapped
        elif address in SITE_LOCAL or any(address in network for network in TRANSLATED):
            return False
    return address.is_global and not address.is_multicast


def _local_only(address: IPAddress) -> bool:
    return (
        address.is_loopback
        or address.is_link_local
        or address.is_unspecified
        or address.is_multicast
        or address.is_reserved
        or (isinstance(address, ipaddress.IPv4Address) and address in ZERO_NET)
    )


def private_hosts_allowed() -> bool:
    from django.conf import settings

    return bool(getattr(settings, "SOFTWARDEN_ALLOW_PRIVATE_SERVICE_HOSTS", True))


def _private(address: IPAddress) -> bool:
    if isinstance(address, ipaddress.IPv4Address):
        return address.is_private or address in CGNAT
    return address.is_private or address in ULA or address in SITE_LOCAL


def smtp_ip_allowed(address: IPAddress) -> bool:
    """An organization's SMTP relay or service (Halo) may be here: private networks yes, unless
    SOFTWARDEN_ALLOW_PRIVATE_SERVICE_HOSTS is off; loopback, link-local (metadata), cloud metadata, unspecified,
    multicast and reserved never, whether direct or embedded in an IPv6 address. NAT64 local-use is refused (its IPv4
    position varies)."""
    if isinstance(address, ipaddress.IPv6Address) and address in TRANSLATED[3]:
        return False
    inner = embedded_ipv4(address)
    for candidate in (address, inner) if inner is not None else (address,):
        if _local_only(candidate) or candidate in CLOUD_METADATA:
            return False
        if not private_hosts_allowed() and _private(candidate):
            return False
    return True


def _literal(host: str) -> IPAddress | None:
    try:
        return ipaddress.ip_address(host.strip("[]").split("%", 1)[0])
    except ValueError:
        return None


def ascii_host(host: str) -> str:
    """The host as sent on the wire (IDNA for international names). UnsafeDestination when it can't be encoded."""
    if _literal(host) is not None:
        return host
    try:
        encoded = host.encode("idna").decode("ascii")
    except UnicodeError:
        raise UnsafeDestination("Enter a valid host name.") from None
    if not encoded:
        raise UnsafeDestination("Enter a valid host name.")
    return encoded.lower()


DEFAULT_PORTS = {"https": 443, "http": 80}


def origin(url: str) -> tuple[str, str, int] | None:
    """Where a URL sends what goes to it: (scheme, host, port) with the scheme and host lowercased, one trailing dot
    dropped, international names IDNA-encoded and the scheme's usual port filled in. None for an empty or unparseable
    URL (it sends nothing anywhere). Two URLs that differ only in their path have the same origin."""
    if not isinstance(url, str) or not url.strip():
        return None
    try:
        parts = urllib.parse.urlsplit(url.strip())
        port = parts.port
    except ValueError:
        return None
    scheme = parts.scheme.lower()
    host = (parts.hostname or "").lower()
    host = host[:-1] if host.endswith(".") else host
    if not host:
        return None
    try:
        host = ascii_host(host)
    except UnsafeDestination:
        return None
    return scheme, host, port if port is not None else DEFAULT_PORTS.get(scheme, 0)


def secret_must_follow(before: tuple | None, after: tuple | None) -> bool:
    """Whether a change of destination from `before` to `after` must come with the secret entered again: a
    stored write-only secret is only ever sent where the Admin who entered it pointed it, so moving it to another
    destination (a first destination included) needs the secret in the same request. Emptying the destination
    sends nothing and doesn't."""
    return after is not None and after != before


def check_url(url: str) -> urllib.parse.SplitResult:
    """The parsed URL, or UnsafeDestination with a reason."""
    if not isinstance(url, str) or not url.strip():
        raise UnsafeDestination("Enter the address.")
    url = url.strip()
    if len(url) > URL_MAX:
        raise UnsafeDestination(f"Use an address of at most {URL_MAX} characters.")
    try:
        parts = urllib.parse.urlsplit(url)
        port = parts.port
    except ValueError:
        raise UnsafeDestination("Enter a valid https:// address.") from None
    if parts.scheme != "https":
        raise UnsafeDestination("Use an https:// address.")
    host = (parts.hostname or "").lower()
    if not host:
        raise UnsafeDestination("Enter a valid https:// address.")
    if parts.username is not None or parts.password is not None:
        raise UnsafeDestination("Leave the user name and password out of the address.")
    if port == 0:
        raise UnsafeDestination("Enter a valid https:// address.")
    literal = _literal(host)
    if literal is not None:
        if not public_ip(literal):
            raise UnsafeDestination("Addresses on private, loopback or link-local networks aren't allowed.")
        return parts
    # A fully qualified name may end in one dot ("localhost.", "foo.local."), which names the same host: judge it
    # without, as the send-time resolution does.
    host = ascii_host(host[:-1] if host.endswith(".") else host)
    if host == "localhost" or host.endswith(BLOCKED_NAMES) or "." not in host:
        raise UnsafeDestination("Use a public host name; local and single-label names aren't allowed.")
    return parts


SERVICE_REFUSED = (
    "That address isn't allowed: loopback, link-local and metadata addresses are refused, and private networks too "
    "when this installation doesn't allow them."
)


def check_service_url(url: str, max_length: int = URL_MAX) -> urllib.parse.SplitResult:
    """A base URL for a service that may be on the organization's own network (Halo). https, a host that IDNA-encodes,
    no user name or password, no query or fragment; a literal address must pass `smtp_ip_allowed` and `localhost`
    names are refused. Names are resolved and checked when connecting (`post(..., private_ok=True)`)."""
    if not isinstance(url, str) or not url.strip():
        raise UnsafeDestination("Enter the address.")
    url = url.strip()
    if len(url) > max_length:
        raise UnsafeDestination(f"Use an address of at most {max_length} characters.")
    try:
        parts = urllib.parse.urlsplit(url)
        port = parts.port
    except ValueError:
        raise UnsafeDestination("Enter a valid https:// address.") from None
    if parts.scheme != "https":
        raise UnsafeDestination("Use an https:// address.")
    host = (parts.hostname or "").lower()
    if not host or port == 0:
        raise UnsafeDestination("Enter a valid https:// address.")
    if parts.username is not None or parts.password is not None:
        raise UnsafeDestination("Leave the user name and password out of the address.")
    if parts.query or parts.fragment:
        raise UnsafeDestination("Use the base address only, without ? or #.")
    literal = _literal(host)
    if literal is not None:
        if not smtp_ip_allowed(literal):
            raise UnsafeDestination(SERVICE_REFUSED)
        return parts
    host = ascii_host(host[:-1] if host.endswith(".") else host)
    if host == "localhost" or host.endswith(".localhost"):
        raise UnsafeDestination(SERVICE_REFUSED)
    return parts


def service_address(host: str, port: int) -> str:
    """The address to connect to for a service host, checked once (UnsafeDestination, Unreachable)."""
    literal = _literal(host)
    if literal is not None:
        if not smtp_ip_allowed(literal):
            raise UnsafeDestination(SERVICE_REFUSED)
        return str(literal)
    addresses = _resolve(host, port)
    for value in addresses:
        parsed = _literal(value)
        if parsed is None or not smtp_ip_allowed(parsed):
            raise UnsafeDestination(SERVICE_REFUSED)
    return addresses[0]


def _resolve(host: str, port: int) -> list[str]:
    try:
        infos = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
    except (socket.gaierror, UnicodeError):
        raise Unreachable("the host name doesn't resolve", ambiguous=False) from None
    addresses = [str(info[4][0]) for info in infos]
    if not addresses:
        raise Unreachable("the host name doesn't resolve", ambiguous=False)
    return addresses


def resolve_public(host: str, port: int) -> str:
    """The first address `host` resolves to, if every address it resolves to is public; else UnsafeDestination."""
    literal = _literal(host)
    if literal is not None:
        if not public_ip(literal):
            raise UnsafeDestination("Addresses on private, loopback or link-local networks aren't allowed.")
        return str(literal)
    addresses = _resolve(host, port)
    for value in addresses:
        parsed = _literal(value)
        if parsed is None or not public_ip(parsed):
            raise UnsafeDestination("The address resolves to a private, loopback or link-local network; refused.")
    return addresses[0]


SMTP_REFUSED = (
    "The SMTP host isn't allowed: loopback, link-local and metadata addresses are refused, and private networks too "
    "when this installation doesn't allow them."
)


def check_smtp_host(host: str) -> None:
    """On save: an IP literal or a name that may be an SMTP relay (UnsafeDestination otherwise). Names are resolved
    when sending (smtp_address)."""
    literal = _literal(host)
    if literal is not None:
        if not smtp_ip_allowed(literal):
            raise UnsafeDestination(SMTP_REFUSED)
        return
    if host.lower() == "localhost" or host.lower().endswith(".localhost"):
        raise UnsafeDestination(SMTP_REFUSED)


def smtp_address(host: str, port: int) -> str:
    """The address to connect to for an organization's SMTP host, checked once (UnsafeDestination, Unreachable)."""
    check_smtp_host(host)
    literal = _literal(host)
    if literal is not None:
        return str(literal)
    addresses = _resolve(host, port)
    for value in addresses:
        parsed = _literal(value)
        if parsed is None or not smtp_ip_allowed(parsed):
            raise UnsafeDestination(SMTP_REFUSED)
    return addresses[0]


# ---- the deadline ---------------------------------------------------------------------------------------------------


def shutdown(sock: Any) -> None:
    """Shut a socket down at the OS level (also an SSL socket, without touching its TLS state), so a read blocked in
    another thread returns at once. Never raises."""
    if sock is None:
        return
    try:
        socket.socket.shutdown(sock, socket.SHUT_RDWR)
    except (OSError, TypeError, ValueError):
        pass


class Deadline:
    """A wall-clock bound on a conversation: `fired` turns true and `kill()` runs when it passes."""

    def __init__(self, seconds: float, kill):
        self.fired = False
        self._kill = kill
        self._timer = threading.Timer(seconds, self._fire)
        self._timer.daemon = True

    def _fire(self) -> None:
        self.fired = True
        self._kill()

    def __enter__(self) -> Deadline:
        self._timer.start()
        return self

    def __exit__(self, *exc) -> None:
        self._timer.cancel()


class _PinnedHTTPSConnection(http.client.HTTPSConnection):
    """Connects to an address already checked, while TLS (SNI and certificate) uses the host name."""

    def __init__(self, host: str, address: str, port: int, timeout: float):
        super().__init__(host, port, timeout=timeout, context=ssl.create_default_context())
        self._address = address
        self._raw: socket.socket | None = None

    def connect(self) -> None:
        self._raw = socket.create_connection((self._address, self.port), self.timeout)
        self.sock = self._context.wrap_socket(self._raw, server_hostname=self.host)  # type: ignore[attr-defined]

    def kill(self) -> None:
        shutdown(self.sock if self.sock is not None else self._raw)


def _quoted(text: str, safe: str) -> str:
    """Percent-encode anything outside ASCII (and spaces); existing escapes are kept."""
    return urllib.parse.quote(text, safe=safe + "%")


def post(
    url: str,
    body: bytes,
    headers: Mapping[str, str],
    timeout: float = TIMEOUT,
    deadline: float | None = None,
    *,
    private_ok: bool = False,
) -> Reply:
    """POST `body` to `url` after the checks above. Returns any HTTP answer as a Reply. `private_ok` applies the
    service rule (private networks allowed, as for an on-premises Halo) instead of the public-only webhook rule."""
    return request("POST", url, body, headers, timeout, deadline, private_ok=private_ok)


def get(
    url: str,
    headers: Mapping[str, str],
    timeout: float = TIMEOUT,
    deadline: float | None = None,
    *,
    body_max: int = BODY_MAX,
) -> Reply:
    """GET `url` from a service an organization configured (the service rule, as `post(..., private_ok=True)`): the
    base address (without its query) must pass `check_service_url`, the host is resolved and checked once and the
    request goes to that address. The answer is read up to `body_max` bytes (Halo's asset pages, discovery/halo.py)."""
    return request("GET", url, None, headers, timeout, deadline, private_ok=True, body_max=body_max)


def request(
    method: str,
    url: str,
    body: bytes | None,
    headers: Mapping[str, str],
    timeout: float = TIMEOUT,
    deadline: float | None = None,
    *,
    private_ok: bool = False,
    body_max: int = BODY_MAX,
) -> Reply:
    """One request after the checks above (`post`, `get`). A service URL (`private_ok`) is checked without its query:
    the base address an Admin entered has none, and a GET adds its own."""
    if private_ok:
        try:
            split = urllib.parse.urlsplit(url)
        except ValueError:
            raise UnsafeDestination("Enter a valid https:// address.") from None
        parts = check_service_url(urllib.parse.urlunsplit(split._replace(query="")))
        parts = parts._replace(query=split.query)
    else:
        parts = check_url(url)
    host = ascii_host(parts.hostname or "")
    port = parts.port or 443
    address = service_address(host, port) if private_ok else resolve_public(host, port)
    path = _quoted(parts.path or "/", "/:@!$&'()*+,;=-._~")
    if parts.query:
        path = f"{path}?{_quoted(parts.query, '/:@!$&()*+,;=?-._~')}"
    host_header = f"[{host}]" if ":" in host else host
    if parts.port:
        host_header = f"{host_header}:{parts.port}"
    connection = _PinnedHTTPSConnection(host, address, port, timeout)
    written = False
    limit = DEADLINE if deadline is None else deadline
    started = time.monotonic()
    try:
        with Deadline(limit, connection.kill) as clock:
            try:
                connection.request(method, path, body=body, headers={"Host": host_header, **headers})
                written = True
                response = connection.getresponse()
                chunks: list[bytes] = []
                size = 0
                while size < body_max:
                    chunk = response.read(min(8192, body_max - size))
                    if not chunk:
                        break
                    chunks.append(chunk)
                    size += len(chunk)
                    if time.monotonic() - started > limit:
                        raise TimeoutError
            except UnicodeError:
                raise UnsafeDestination("The address can't be encoded for sending.") from None
            except Exception as e:
                if not (clock.fired or isinstance(e, OSError | http.client.HTTPException)):
                    raise
                reason = f"no complete answer within {limit:g} s" if clock.fired else type(e).__name__
                # Before the request was written nothing was acted on; after, the outcome is unknown.
                raise Unreachable(reason, ambiguous=written) from None
            if clock.fired:
                raise Unreachable(f"no complete answer within {limit:g} s", ambiguous=True)
        return Reply(response.status, merged_headers(response.getheaders()), b"".join(chunks))
    finally:
        connection.close()


# Headers whose value is a comma-separated list (RFC 9110 5.3), so repeats can be joined without changing meaning.
LIST_HEADERS = frozenset({"link"})


def merged_headers(pairs: list[tuple[str, str]]) -> dict[str, str]:
    """Response headers by lower-cased name. A list-valued header sent more than once (LIST_HEADERS: Okta's two `link`
    headers, rel="self" and rel="next") is joined with ", ", so both reach the caller; any other repeat keeps the last
    value, as before."""
    out: dict[str, str] = {}
    for name, value in pairs:
        key = name.lower()
        out[key] = f"{out[key]}, {value}" if key in out and key in LIST_HEADERS else value
    return out


READ_BODY_MAX = 8 * 1024 * 1024  # a page of Okta apps (200) or Google audit activities (1,000) is far smaller


class PublicTransport:
    """core/graph.Transport's shape over `request` under the public (webhook) rule: https, a public host name, every
    address it resolves to public, no redirects, a deadline. For a vendor's cloud API an Admin pointed Softwarden at
    (an Okta org address, Google's fixed hosts; discovery): a custom domain that resolves to a loopback,
    private or metadata address is refused before anything is sent (graph.HostRefused)."""

    def send(self, method: str, url: str, headers: Mapping[str, str], body: bytes | None = None) -> Reply:
        from .graph import HostRefused

        try:
            return request(method, url, body, dict(headers), timeout=TIMEOUT, body_max=READ_BODY_MAX)
        except UnsafeDestination as e:
            raise HostRefused(str(e)) from None
