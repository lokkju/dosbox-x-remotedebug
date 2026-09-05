#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.11"
# dependencies = [
#     "pytest>=8.0",
# ]
# ///
"""Offline unit tests for the raw GDB RSP client framing.

These never touch an emulator. Conformance tests that do live in
test_gdb_conformance.py.
"""

import re
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent))

from protocol.gdb import RawGDB, GDBProtocolError


class FakeSocket:
    """Replays a scripted byte stream and records what was written."""

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

    def close(self) -> None:
        pass


def test_checksum_is_sum_of_bytes_mod_256():
    assert RawGDB.checksum("g") == 0x67
    assert RawGDB.checksum("qSupported:") == sum(b"qSupported:") % 256


def test_send_frames_the_packet_and_strips_the_reply():
    # Reply: an ack, then $OK#9a
    sock = FakeSocket(b"+$OK#9a")
    client = RawGDB()
    client._sock = sock
    assert client.send("g") == "OK"
    assert sock.written == b"$g#67"


def test_send_raises_on_a_bad_checksum():
    sock = FakeSocket(b"+$OK#00")
    client = RawGDB()
    client._sock = sock
    with pytest.raises(GDBProtocolError, match="checksum"):
        client.send("g")


def test_read_registers_decodes_sixteen_little_endian_words():
    # Sixteen registers, each 0x00000001 little-endian => "01000000" * 16
    payload = "01000000" * 16
    sock = FakeSocket(b"+$" + payload.encode() + b"#"
                      + f"{RawGDB.checksum(payload):02x}".encode())
    client = RawGDB()
    client._sock = sock
    assert client.read_registers() == [1] * 16


def test_read_memory_decodes_hex_to_bytes():
    sock = FakeSocket(b"+$ebfe#" + f"{RawGDB.checksum('ebfe'):02x}".encode())
    client = RawGDB()
    client._sock = sock
    assert client.read_memory(0x20000, 2) == b"\xeb\xfe"
    assert sock.written == b"$m20000,2#" + f"{RawGDB.checksum('m20000,2'):02x}".encode()


def test_set_breakpoint_sends_a_linear_address():
    sock = FakeSocket(b"+$OK#9a")
    client = RawGDB()
    client._sock = sock
    assert client.set_breakpoint(0x20000) is True
    assert sock.written == b"$Z0,20000,1#" + f"{RawGDB.checksum('Z0,20000,1'):02x}".encode()


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-v"]))


# --- Source-level guard on hand-written packets -------------------------

# Every GDB reply should go through GDBServer::send_packet(), which computes
# the checksum. One rejection path predates that and writes the packet out by
# hand, where a wrong checksum is invisible until a conformance-checking
# client rejects the reply. This scans for any such literal and verifies it,
# so a future hand-written packet cannot reintroduce the bug.
GDBSERVER_CPP = Path(__file__).resolve().parents[2] / "src" / "debug" / "gdbserver.cpp"

PACKET_LITERAL = re.compile(r'"\$([^"#]*)#([0-9a-fA-F]{2})"')


def test_hand_written_packet_literals_have_correct_checksums():
    source = GDBSERVER_CPP.read_text(encoding="utf-8", errors="replace")
    literals = PACKET_LITERAL.findall(source)
    assert literals, f"no packet literals found in {GDBSERVER_CPP}; did the file move?"

    wrong = []
    for body, given in literals:
        expected = sum(body.encode("ascii")) & 0xFF
        if int(given, 16) != expected:
            wrong.append(f"${body}#{given} should be #{expected:02x}")

    assert not wrong, "hand-written packets with bad checksums: " + "; ".join(wrong)
