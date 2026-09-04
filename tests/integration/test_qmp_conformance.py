#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.11"
# dependencies = [
#     "pytest>=8.0",
# ]
# ///
"""Conformance tests for the QMP server."""

import base64
import sys
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent))

from protocol.qmp import QMPProtocolError


def test_savestate_completes_while_halted_for_gdb(emulator, tmp_path):
    """Normal_Loop used to return before its drains when gdb_cpu_paused, so
    SAVESTATE_CheckPendingRequest never ran and this timed out after 30s."""
    gdb = emulator.gdb()
    qmp = emulator.qmp()

    gdb.halt()
    status = qmp.execute("query-status")
    assert status is not None

    target = tmp_path / "halted.sav"
    started = time.time()
    result = qmp.execute("savestate", {"file": str(target)})
    elapsed = time.time() - started

    assert elapsed < 20.0, (
        f"savestate took {elapsed:.1f}s while halted; the pending-request "
        f"drain is not running on the GDB-halted path")
    assert result["file"] == str(target)


def test_query_status_reports_the_debug_pause(emulator):
    gdb = emulator.gdb()
    qmp = emulator.qmp()
    gdb.halt()
    status = qmp.execute("query-status")
    assert status.get("debug", {}).get("paused") is True, (
        f"query-status did not report the debug pause: {status}")


def test_memdump_while_halted_matches_gdb_memory_read(emulator):
    """memdump and the m packet must agree byte for byte on the same
    linear range. Both read guest memory; only the transport differs."""
    gdb = emulator.gdb()
    qmp = emulator.qmp()

    gdb.halt()
    marker = bytes(range(16))
    assert gdb.write_memory(0x30000, marker) is True

    result = qmp.execute("memdump", {"address": 0x30000, "size": 16})
    assert base64.b64decode(result["data"]) == marker
    assert result["size"] == 16


def test_memdump_rejects_a_missing_size(emulator):
    qmp = emulator.qmp()
    reply = qmp.execute_raw("memdump", {"address": 0x30000})
    assert "error" in reply
    assert reply["error"]["class"] == "GenericError"


def test_memdump_rejects_an_oversized_request(emulator):
    qmp = emulator.qmp()
    reply = qmp.execute_raw("memdump",
                            {"address": 0, "size": 17 * 1024 * 1024})
    assert "error" in reply
    assert "too large" in reply["error"]["desc"].lower()


def test_memdump_refuses_while_the_guest_is_running(emulator):
    """Reading guest memory from the QMP socket thread while the emulation
    thread is executing is a data race. The command refuses rather than
    returning bytes that were never coherent."""
    qmp = emulator.qmp()
    reply = qmp.execute_raw("memdump", {"address": 0x30000, "size": 16})
    assert "error" in reply
    assert "stopped" in reply["error"]["desc"].lower()


def test_memdump_works_after_a_qmp_stop(emulator):
    """QMP `stop` parks the emulation thread in PauseDOSBoxLoop, so guest
    memory is quiescent and a direct read is safe -- even though it sets
    none of the flags DEBUG_IsCpuPausedForDebug() tests. The refusal message
    tells users to do this, so it has to work.
    """
    qmp = emulator.qmp()
    qmp.execute("stop")
    try:
        result = qmp.execute("memdump", {"address": 0xB8000, "size": 16})
        assert result["size"] == 16
        assert len(base64.b64decode(result["data"])) == 16
    finally:
        qmp.execute("cont")


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-v"]))
