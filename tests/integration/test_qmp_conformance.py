#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.11"
# dependencies = [
#     "pytest>=8.0",
# ]
# ///
"""Conformance tests for the QMP server."""

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


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-v"]))
