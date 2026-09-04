"""Raw QMP client. Standard library only.

Asserts what the QMP server does. Ergonomics belong in dbxdebug, which this
repository cannot depend on -- see the note at the top of gdb.py.
"""

import json
import socket
import time


class QMPProtocolError(Exception):
    """The server returned an error reply, or something unparseable."""


class RawQMP:
    def __init__(self, host: str = "127.0.0.1", port: int = 4444,
                 timeout: float = 5.0):
        self.host = host
        self.port = port
        self.timeout = timeout
        self._sock = None
        self._buf = b""

    def __enter__(self):
        self.connect()
        return self

    def __exit__(self, exc_type, exc, tb):
        self.close()
        return False

    def connect(self) -> dict:
        self._sock = socket.create_connection((self.host, self.port),
                                              timeout=self.timeout)
        self._sock.settimeout(self.timeout)
        greeting = self._recv_json()
        self.execute("qmp_capabilities")
        return greeting

    def close(self) -> None:
        if self._sock is not None:
            try:
                self._sock.close()
            finally:
                self._sock = None

    def _recv_json(self) -> dict:
        deadline = time.time() + self.timeout
        while time.time() < deadline:
            if b"\n" in self._buf:
                line, self._buf = self._buf.split(b"\n", 1)
                line = line.strip()
                if not line:
                    continue
                try:
                    return json.loads(line.decode())
                except json.JSONDecodeError as exc:
                    raise QMPProtocolError(
                        f"not JSON: {line!r}") from exc
            chunk = self._sock.recv(65536)
            if not chunk:
                raise QMPProtocolError("connection closed")
            self._buf += chunk
        raise QMPProtocolError("timed out waiting for a reply")

    def execute_raw(self, command: str, arguments: dict = None) -> dict:
        payload = {"execute": command}
        if arguments is not None:
            payload["arguments"] = arguments
        self._sock.sendall(json.dumps(payload).encode())
        while True:
            reply = self._recv_json()
            # Skip asynchronous events; only replies carry return/error.
            if "return" in reply or "error" in reply:
                return reply

    def execute(self, command: str, arguments: dict = None) -> dict:
        reply = self.execute_raw(command, arguments)
        if "error" in reply:
            err = reply["error"]
            raise QMPProtocolError(
                f"{err.get('class', '?')}: {err.get('desc', '?')}")
        return reply["return"]
