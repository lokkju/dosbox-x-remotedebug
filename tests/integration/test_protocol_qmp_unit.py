#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.11"
# dependencies = [
#     "pytest>=8.0",
# ]
# ///
"""Offline unit tests for the raw QMP client framing."""

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent))

from protocol.qmp import RawQMP, QMPProtocolError


class FakeSocket:
    def __init__(self, to_read: bytes):
        self._to_read = to_read
        self._pos = 0
        self.written = b""

    def sendall(self, data: bytes) -> None:
        self.written += data

    def recv(self, n: int) -> bytes:
        chunk = self._to_read[self._pos:self._pos + n]
        self._pos += len(chunk)
        return chunk

    def settimeout(self, _t) -> None:
        pass

    def gettimeout(self):
        return None

    def close(self) -> None:
        pass


def test_execute_sends_a_command_and_returns_the_return_object():
    sock = FakeSocket(b'{"return": {"size": 4}}\r\n')
    client = RawQMP()
    client._sock = sock
    assert client.execute("memdump", {"address": 0, "size": 4}) == {"size": 4}
    sent = json.loads(sock.written.decode())
    assert sent == {"execute": "memdump",
                    "arguments": {"address": 0, "size": 4}}


def test_execute_raises_on_an_error_reply():
    sock = FakeSocket(b'{"error": {"class": "GenericError", "desc": "nope"}}\r\n')
    client = RawQMP()
    client._sock = sock
    with pytest.raises(QMPProtocolError, match="nope"):
        client.execute("memdump")


def test_execute_raw_returns_the_error_without_raising():
    sock = FakeSocket(b'{"error": {"class": "GenericError", "desc": "nope"}}\r\n')
    client = RawQMP()
    client._sock = sock
    reply = client.execute_raw("memdump")
    assert reply["error"]["class"] == "GenericError"


def test_omits_the_arguments_key_when_there_are_none():
    sock = FakeSocket(b'{"return": {}}\r\n')
    client = RawQMP()
    client._sock = sock
    client.execute("query-status")
    assert json.loads(sock.written.decode()) == {"execute": "query-status"}


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-v"]))
