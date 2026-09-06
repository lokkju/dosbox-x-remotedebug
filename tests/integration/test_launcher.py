#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.11"
# dependencies = [
#     "pytest>=8.0",
# ]
# ///
"""The launcher must isolate itself and clean up after itself."""

import os
import socket
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent))

from launcher import Emulator, free_port


def test_free_port_returns_a_bindable_port():
    port = free_port()
    with socket.socket() as s:
        s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        s.bind(("127.0.0.1", port))


def test_two_launchers_get_different_ports():
    a, b = Emulator(), Emulator()
    a._allocate_ports()
    b._allocate_ports()
    assert a.gdb_port != b.gdb_port
    assert a.qmp_port != b.qmp_port


def test_ports_are_not_the_stock_defaults():
    """A test that binds 2159/4444 would collide with a developer's own
    emulator, and would silently talk to it if the bind lost the race."""
    e = Emulator()
    e._allocate_ports()
    assert e.gdb_port != 2159
    assert e.qmp_port != 4444


def test_start_and_stop_leaves_no_process_and_no_workdir(emulator):
    workdir = emulator.workdir
    pid = emulator.pid
    assert workdir.exists()
    emulator.stop()
    assert not workdir.exists()
    with pytest.raises(OSError):
        os.kill(pid, 0)


def test_conf_uses_the_spaced_option_names(emulator):
    """`gdbport` is not an option. Writing it means the server silently
    falls back to 2159 and the test talks to somebody else's emulator."""
    conf = emulator.conf_path.read_text()
    assert f"gdbserver port = {emulator.gdb_port}" in conf
    assert f"qmpserver port = {emulator.qmp_port}" in conf
    assert "gdbport" not in conf
    assert "qmpport" not in conf


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-v"]))
