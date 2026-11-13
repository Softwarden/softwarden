"""DNS TXT lookups for sign-in domain verification (core/sso.verify_domain).

Standard library only (no new dependency): one query for the TXT records of a name, over UDP with an EDNS0 buffer of
4096 bytes, again over TCP when the answer comes back truncated. Each attempt has one deadline of TIMEOUT seconds;
only a reply from the resolver's own address and port, with the query's ID, one question, this name and type TXT, is
read as the answer. The resolver is SOFTWARDEN_DNS_RESOLVER (`manage.py doctor` warns about `simulated` with DEBUG
off):

- `simulated`: no network at all. Answers come from SIMULATED (tests set it), and a name under SIMULATED_SUFFIX
  answers the challenge of every pending claim of its domain, as if its owner had published the record (development
  and the Playwright server). The default under DEBUG; tests/settings.py sets it explicitly, so a test never resolves
  real DNS.
- `host` or `host:port` (an IP address or a name): that recursive resolver.
- empty (the default with DEBUG off): the first `nameserver` in /etc/resolv.conf (in a container, Docker's or the
  cluster's resolver).

`txt_records(name)` returns each record's strings joined, decoded as UTF-8 (undecodable bytes replaced); an empty list
when the name or its TXT records don't exist. Any other failure (no resolver, timeout, a server error, a malformed
answer) raises DnsError; callers treat it as "not verified", in the same words."""

from __future__ import annotations

import ipaddress
import secrets
import socket
import struct
import time
from pathlib import Path

from django.conf import settings

TIMEOUT = 3.0  # seconds, per attempt (UDP, then TCP when truncated)
TXT, IN, OPT = 16, 1, 41
EDNS_BUFFER = 4096
SIMULATED_SUFFIX = ".dns-verified.test"
# {query name: [TXT values]} answered by the simulated resolver (tests fill it; never used outside simulation).
SIMULATED: dict[str, list[str]] = {}
RESOLV_CONF = Path("/etc/resolv.conf")


class DnsError(Exception):
    """The lookup couldn't be done or answered (never the case "no such record", which is an empty answer)."""


def simulated() -> bool:
    return resolver_setting() == "simulated"


def resolver_setting() -> str:
    value = getattr(settings, "SOFTWARDEN_DNS_RESOLVER", None)
    if value is None:
        return "simulated" if settings.DEBUG else ""
    return str(value).strip()


def txt_records(name: str) -> list[str]:
    name = name.strip().rstrip(".").lower()
    if simulated():
        return _simulated(name)
    host, port = _resolver()
    return _lookup(name, host, port)


def _simulated(name: str) -> list[str]:
    if name in SIMULATED:
        return list(SIMULATED[name])
    from . import sso  # the challenge values of pending claims (sso imports nothing from here at import time)

    prefix = sso.CHALLENGE_PREFIX + "."
    if name.startswith(prefix) and name.endswith(SIMULATED_SUFFIX):
        return sso.challenge_values(name[len(prefix) :])
    return []


def _resolver() -> tuple[str, int]:
    value = resolver_setting()
    if not value:
        try:
            lines = RESOLV_CONF.read_text(encoding="utf-8", errors="replace").splitlines()
        except OSError as e:
            raise DnsError("no resolver: set SOFTWARDEN_DNS_RESOLVER") from e
        servers = [line.split()[1] for line in lines if line.startswith("nameserver") and len(line.split()) > 1]
        if not servers:
            raise DnsError("no resolver: set SOFTWARDEN_DNS_RESOLVER")
        return servers[0], 53
    if value.startswith("[") and "]" in value:  # [v6]:port
        host, _, rest = value[1:].partition("]")
        return host, _port(rest.lstrip(":") or "53")
    if value.count(":") == 1:
        host, _, port = value.partition(":")
        return host, _port(port)
    return value, 53


def _port(text: str) -> int:
    """A port from SOFTWARDEN_DNS_RESOLVER: a misconfiguration is a failed lookup (DnsError), never a 500."""
    if not text.isascii() or not text.isdigit() or not 0 < int(text) < 65536:
        raise DnsError("SOFTWARDEN_DNS_RESOLVER has an invalid port")
    return int(text)


def _query(name: str) -> tuple[int, bytes]:
    qid = secrets.randbits(16)
    try:
        labels = [label.encode("ascii") for label in name.split(".")]
    except UnicodeEncodeError as e:
        raise DnsError("not an ASCII (IDNA) name") from e
    if any(not 0 < len(label) < 64 for label in labels) or len(name) > 253:
        raise DnsError("not a valid name")
    qname = b"".join(bytes([len(label)]) + label for label in labels) + b"\0"
    header = struct.pack("!HHHHHH", qid, 0x0100, 1, 0, 0, 1)  # recursion desired; one question; one OPT record
    opt = b"\0" + struct.pack("!HHIH", OPT, EDNS_BUFFER, 0, 0)
    return qid, header + qname + struct.pack("!HH", TXT, IN) + opt


def _remaining(deadline: float) -> float:
    left = deadline - time.monotonic()
    if left <= 0:
        raise DnsError("timeout")
    return left


def _same_sender(sender, address) -> bool:
    """Whether a datagram came from the resolver queried: its address and port (IPv6 tuples carry more fields)."""
    try:
        return ipaddress.ip_address(sender[0]) == ipaddress.ip_address(address[0]) and sender[1] == address[1]
    except (ValueError, IndexError, TypeError):
        return False


def _lookup(name: str, host: str, port: int) -> list[str]:
    """One query: over UDP, then TCP when the answer is truncated, each attempt within TIMEOUT seconds overall. Only a
    datagram from the resolver's own address and port, carrying this query's ID, is read as the answer; parse then
    checks that it answers this name's TXT question."""
    qid, packet = _query(name)
    try:
        family, kind, proto, _, address = socket.getaddrinfo(host, port, type=socket.SOCK_DGRAM)[0]
        deadline = time.monotonic() + TIMEOUT
        with socket.socket(family, kind, proto) as udp:
            udp.sendto(packet, address)
            while True:
                udp.settimeout(_remaining(deadline))
                answer, sender = udp.recvfrom(EDNS_BUFFER)
                if not _same_sender(sender, address):
                    continue  # another host's datagram: never an answer
                if len(answer) >= 2 and struct.unpack("!H", answer[:2])[0] == qid:
                    break  # anything else is a stray datagram: keep waiting until the deadline
        if len(answer) >= 4 and answer[2] & 0x02:  # TC: truncated, ask again over TCP
            deadline = time.monotonic() + TIMEOUT
            with socket.create_connection((str(address[0]), port), timeout=TIMEOUT) as tcp:
                tcp.sendall(struct.pack("!H", len(packet)) + packet)
                size = struct.unpack("!H", _read(tcp, 2, deadline))[0]
                answer = _read(tcp, size, deadline)
    except (OSError, struct.error) as e:
        raise DnsError(type(e).__name__) from e
    return parse(answer, qid, name)


def _read(sock: socket.socket, size: int, deadline: float) -> bytes:
    data = b""
    while len(data) < size:
        sock.settimeout(_remaining(deadline))
        chunk = sock.recv(size - len(data))
        if not chunk:
            raise DnsError("connection closed")
        data += chunk
    return data


def _skip_name(buf: bytes, offset: int) -> int:
    while True:
        if offset >= len(buf):
            raise DnsError("malformed answer")
        length = buf[offset]
        if length == 0:
            return offset + 1
        if length & 0xC0 == 0xC0:  # a compression pointer ends the name
            return offset + 2
        offset += 1 + length


def _question_name(buf: bytes, offset: int) -> tuple[str, int]:
    """The question's name (uncompressed, as a resolver echoes it) and the offset after it."""
    labels: list[str] = []
    while True:
        if offset >= len(buf):
            raise DnsError("malformed answer")
        length = buf[offset]
        if length == 0:
            return ".".join(labels), offset + 1
        if length & 0xC0 or offset + 1 + length > len(buf):
            raise DnsError("malformed question")
        labels.append(buf[offset + 1 : offset + 1 + length].decode("ascii", errors="replace"))
        offset += 1 + length


def parse(answer: bytes, qid: int, name: str) -> list[str]:
    """The TXT values in a DNS response to query `qid` for `name`: [] for NXDOMAIN or no TXT records; DnsError for an
    answer to another query (ID, a question count other than one, another name or type), a server failure or a
    malformed answer."""
    if len(answer) < 12:
        raise DnsError("malformed answer")
    got, flags, questions, answers, _, _ = struct.unpack("!HHHHHH", answer[:12])
    if got != qid or not flags & 0x8000:
        raise DnsError("not the answer to this query")
    if questions != 1:
        raise DnsError("not the answer to this query")
    asked, offset = _question_name(answer, 12)
    if offset + 4 > len(answer):
        raise DnsError("malformed answer")
    qtype, _ = struct.unpack("!HH", answer[offset : offset + 4])
    if asked.lower().rstrip(".") != name.lower().rstrip(".") or qtype != TXT:
        raise DnsError("not the answer to this query")
    offset += 4
    code = flags & 0x000F
    if code == 3:  # NXDOMAIN
        return []
    if code != 0:
        raise DnsError(f"server answered rcode {code}")
    values = []
    for _ in range(answers):
        offset = _skip_name(answer, offset)
        if offset + 10 > len(answer):
            raise DnsError("malformed answer")
        rtype, _, _, length = struct.unpack("!HHIH", answer[offset : offset + 10])
        offset += 10
        data = answer[offset : offset + length]
        if len(data) != length:
            raise DnsError("malformed answer")
        offset += length
        if rtype != TXT:
            continue  # a CNAME on the way, for example
        parts, i = [], 0
        while i < len(data):
            size = data[i]
            if i + 1 + size > len(data):
                raise DnsError("malformed TXT record")
            parts.append(data[i + 1 : i + 1 + size])
            i += 1 + size
        values.append(b"".join(parts).decode("utf-8", errors="replace"))
    return values
