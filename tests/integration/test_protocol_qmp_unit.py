#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.11"
# dependencies = [
#     "pytest>=8.0",
# ]
# ///
"""Offline unit tests for the raw QMP client framing."""

import json
import re
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


class TimingOutSocket:
    """A socket that accepts writes and never answers."""

    def __init__(self):
        self.written = b""

    def sendall(self, data: bytes) -> None:
        self.written += data

    def recv(self, n: int) -> bytes:
        raise TimeoutError("timed out")

    def settimeout(self, _t) -> None:
        pass

    def gettimeout(self):
        return None

    def close(self) -> None:
        pass


def test_an_unresponsive_server_raises_a_protocol_error_not_a_socket_error():
    """A bare socket.timeout escaping _recv_json makes every caller handle
    two unrelated exception types for the same condition."""
    client = RawQMP(timeout=0.1)
    client._sock = TimingOutSocket()
    with pytest.raises(QMPProtocolError, match="timed out"):
        client.execute("query-status")



# --- Source-level guard on the QMP command table -------------------------

# query-commands used to be a second, hand-written copy of the command set,
# and it drifted from the dispatch. Both now read one table, so the only way
# to reintroduce the drift is to add a dispatch branch that bypasses it.
# This scans for that shape.
QMP_CPP = Path(__file__).resolve().parents[2] / "src" / "debug" / "qmp.cpp"

EXECUTE_COMPARE = re.compile(r'execute\s*==\s*"([^"]+)"')


def test_no_command_is_dispatched_outside_the_command_table():
    source = QMP_CPP.read_text(encoding="utf-8", errors="replace")
    stray = EXECUTE_COMPARE.findall(source)
    assert not stray, (
        "these commands are dispatched by a direct string comparison instead "
        "of the command table, so query-commands cannot see them: "
        + ", ".join(sorted(set(stray))))


COMMAND_TABLE_ENTRY = re.compile(r'\{"([A-Za-z0-9_\-]+)",\s*\[\]')


def test_the_command_table_is_the_source_of_the_advertised_list():
    """handle_query_commands must build its reply by walking the table. A
    second, hand-written list of names is exactly what drifted before."""
    source = QMP_CPP.read_text(encoding="utf-8", errors="replace")
    names = COMMAND_TABLE_ENTRY.findall(source)
    assert len(names) >= 10, (
        f"found only {names} in the command table; did it move or change "
        f"shape? update this guard")

    start = source.find("void QMPServer::handle_query_commands()")
    assert start != -1, "handle_query_commands moved; update this guard"
    body = source[start:source.find("\n}", start)]

    assert "command_table()" in body, (
        "handle_query_commands does not walk the command table")
    duplicated = [n for n in names if f'"{n}"' in body]
    assert not duplicated, (
        "handle_query_commands hard-codes command names that also live in "
        "the table, so the two can drift again: " + ", ".join(duplicated))


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-v"]))
