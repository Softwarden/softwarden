"""A minimal SMTP server in a thread, on 127.0.0.1 with a free port, for the SMTP transport tests (Python 3.13 has no
smtpd module, and aiosmtpd isn't a dependency). Plain SMTP with optional AUTH PLAIN; scripted replies let a test make
the server defer (4xx), refuse (5xx) or drop the connection after DATA."""

import base64
import socketserver
import threading
from dataclasses import dataclass, field


@dataclass
class Script:
    username: str = ""
    password: str = ""
    rcpt_reply: str = "250 OK"
    data_reply: str = "250 Queued"
    drop_after_data: bool = False
    trickle_greeting: bool = False  # answer "2", then a byte every 0.2 s, never a whole line
    trickle_after_data: bool = False  # the same, as the answer to the message data
    messages: list[dict] = field(default_factory=list)
    auths: list[tuple[str, str]] = field(default_factory=list)  # every AUTH PLAIN attempt: (user name, password)


class _Handler(socketserver.StreamRequestHandler):
    def reply(self, line: str) -> None:
        self.wfile.write((line + "\r\n").encode())
        self.wfile.flush()

    def trickle(self) -> None:
        import time

        try:
            for _ in range(100):
                self.wfile.write(b"2")
                self.wfile.flush()
                time.sleep(0.2)
        except OSError:
            return

    def handle(self) -> None:
        script: Script = self.server.script  # type: ignore[attr-defined]
        if script.trickle_greeting:
            return self.trickle()
        self.reply("220 fake.smtp.test ESMTP")
        sender, recipients = "", []
        while True:
            raw = self.rfile.readline()
            if not raw:
                return
            line = raw.decode().rstrip("\r\n")
            verb = line.split(" ", 1)[0].upper()
            if verb in ("EHLO", "HELO"):
                if script.username:
                    self.wfile.write(b"250-fake.smtp.test\r\n250 AUTH PLAIN\r\n")
                    self.wfile.flush()
                else:
                    self.reply("250 fake.smtp.test")
            elif verb == "AUTH":
                parts = line.split(" ")
                decoded = base64.b64decode(parts[2]).split(b"\x00") if len(parts) > 2 else []
                if len(decoded) == 3:
                    script.auths.append((decoded[1].decode(), decoded[2].decode()))
                if decoded[1:] == [script.username.encode(), script.password.encode()]:
                    self.reply("235 Authentication successful")
                else:
                    self.reply("535 Authentication failed")
            elif verb == "MAIL":
                sender = line.split(":", 1)[1].strip()
                self.reply("250 OK")
            elif verb == "RCPT":
                recipients.append(line.split(":", 1)[1].strip())
                self.reply(script.rcpt_reply)
            elif verb == "DATA":
                self.reply("354 End data with <CR><LF>.<CR><LF>")
                body = []
                while True:
                    chunk = self.rfile.readline()
                    if not chunk or chunk in (b".\r\n", b".\n"):
                        break
                    body.append(chunk)
                if script.drop_after_data:
                    return  # the message was written; the answer never comes
                if script.trickle_after_data:
                    return self.trickle()
                script.messages.append({"from": sender, "to": recipients, "data": b"".join(body).decode()})
                self.reply(script.data_reply)
            elif verb == "RSET":
                sender, recipients = "", []
                self.reply("250 OK")
            elif verb == "NOOP":
                self.reply("250 OK")
            elif verb == "QUIT":
                self.reply("221 Bye")
                return
            else:
                self.reply("502 Not implemented")


class FakeSmtp:
    def __init__(self, script: Script | None = None):
        self.script = script or Script()
        self.server = socketserver.ThreadingTCPServer(("127.0.0.1", 0), _Handler)
        self.server.daemon_threads = True
        self.server.script = self.script  # type: ignore[attr-defined]
        self.port = self.server.server_address[1]
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)

    def __enter__(self) -> "FakeSmtp":
        self.thread.start()
        return self

    def __exit__(self, *exc) -> None:
        self.server.shutdown()
        self.server.server_close()
