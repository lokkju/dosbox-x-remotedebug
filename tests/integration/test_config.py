#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.11"
# dependencies = [
#     "pytest>=8.0",
# ]
# ///
"""The conf option names are `gdbserver port` and `qmpserver port`, with a
space (dosbox.cpp:1726 and :1732). The spaceless spellings are not options
at all: they are ignored, the server falls back to 2159/4444, and a client
that thinks it asked for its own port connects to another emulator."""

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent))

from launcher import Emulator, port_is_listening


def test_the_requested_ports_are_the_ports_that_listen(emulator):
    assert port_is_listening(emulator.gdb_port)
    assert port_is_listening(emulator.qmp_port)


def test_the_stock_ports_are_not_bound_by_our_emulator(emulator):
    """If the conf were ignored the emulator would be on 2159/4444. This
    can only false-pass when a foreign emulator holds those ports, so it
    asserts our own ports are elsewhere too."""
    assert emulator.gdb_port != 2159
    assert emulator.qmp_port != 4444
    gdb = emulator.gdb()
    assert "PacketSize=" in gdb.supported()


def test_the_spaceless_option_names_are_silently_ignored(tmp_path):
    """Documents the trap. `gdbport` is accepted by the parser and does
    nothing, so an emulator configured with it lands on the default port."""
    if port_is_listening(2159):
        pytest.skip("2159 already in use")
    emu = Emulator()
    emu._allocate_ports()
    requested = emu.gdb_port
    emu.workdir = tmp_path
    conf = tmp_path / "bad.conf"
    conf.write_text(
        "[dosbox]\n"
        "quit warning = false\n"
        "gdbserver = true\n"
        f"gdbport = {requested}\n"
        "qmpserver = true\n"
        f"qmpport = {emu.qmp_port}\n"
    )
    emu.conf_path = conf
    try:
        emu._spawn()
        # The emulator comes up on 2159/4444, not on what was requested.
        import time
        deadline = time.time() + 15.0
        while time.time() < deadline and not port_is_listening(2159):
            time.sleep(0.1)
        assert port_is_listening(2159), (
            "expected the ignored option to leave the server on its default "
            "port; if this fails the option names may have been fixed")
        assert not port_is_listening(requested)
    finally:
        emu.stop()


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-v"]))
