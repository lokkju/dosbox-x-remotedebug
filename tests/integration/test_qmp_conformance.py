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

from protocol.qmp import RawQMP


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


def test_query_commands_lists_every_dispatched_command(qmp):
    """Guards against a command being added to the dispatch at qmp.cpp:404
    without being announced, which makes it undiscoverable."""
    listed = {entry["name"] for entry in qmp.execute("query-commands")}
    expected = {
        "qmp_capabilities", "send-key", "input-send-event", "query-commands",
        "memdump", "screendump", "savestate", "loadstate", "stop", "cont",
        "system_reset", "query-status", "debug-break-on-exec",
        # Acknowledged no-ops kept for QEMU-client compatibility. They are
        # dispatched, so they belong in the list; a client that sees them
        # absent concludes the server rejects them.
        "quit", "system_powerdown",
    }
    missing = expected - listed
    assert not missing, f"query-commands omits {sorted(missing)}"


def test_send_key_accepts_a_qcode(qmp):
    assert qmp.execute("send-key",
                       {"keys": [{"type": "qcode", "data": "a"}]}) is not None


def test_input_send_event_accepts_a_key_event(qmp):
    assert qmp.execute("input-send-event", {"events": [
        {"type": "key", "data": {"down": True,
                                 "key": {"type": "qcode", "data": "a"}}},
    ]}) is not None


def test_stop_then_cont_round_trips(qmp):
    qmp.execute("stop")
    assert qmp.execute("query-status")["running"] is False
    qmp.execute("cont")
    assert qmp.execute("query-status")["running"] is True


def test_screendump_returns_png_data(qmp, tmp_path):
    result = qmp.execute("screendump", {"file": str(tmp_path / "shot.png")})
    assert result["format"] == "png"
    assert result["size"] > 0


def test_savestate_then_loadstate_round_trips(qmp, tmp_path):
    """loadstate has no dispatch coverage unless a save exists first --
    write one instead of skipping the loadstate command entirely."""
    target = tmp_path / "roundtrip.sav"

    save_result = qmp.execute("savestate", {"file": str(target)})
    assert save_result["file"] == str(target)
    assert target.exists()

    load_result = qmp.execute("loadstate", {"file": str(target)})
    assert load_result["file"] == str(target)


def test_a_failing_savestate_reports_an_error_and_leaves_the_emulator_alive(
        qmp, tmp_path):
    """SaveState::save() reports most failures through notifyError(), which
    opens a modal message box. A modal dialog waits for a click, so on a
    headless host it parks the emulation thread forever: no error, no crash,
    no further GDB or QMP progress. Save into a directory that does not
    exist -- zipOpen fails, which is the same failure branch the dialog sat
    on -- then prove the emulator is still executing afterwards."""
    doomed = tmp_path / "no-such-directory" / "doomed.sav"

    started = time.time()
    reply = qmp.execute_raw("savestate", {"file": str(doomed)})
    elapsed = time.time() - started

    assert "error" in reply, f"a save that cannot be written returned {reply}"
    desc = reply["error"].get("desc", "")
    assert "timed out" not in desc, (
        f"savestate answered only by hitting its own timeout ({desc!r}); "
        f"the emulation thread was blocked, not reporting")
    assert elapsed < 10.0, f"the failure took {elapsed:.1f}s to report"
    assert not doomed.exists()

    # Liveness: this second save only completes if the emulation thread is
    # still draining pending requests, which a modal dialog would prevent.
    survivor = tmp_path / "survivor.sav"
    assert qmp.execute("savestate", {"file": str(survivor)})["file"] == str(
        survivor)
    assert survivor.exists()


def test_system_reset_is_acknowledged(qmp):
    """system_reset replies immediately and reboots the guest
    asynchronously on the main thread. Assert the ack, then confirm the
    server (this test's own emulator, torn down by the fixture afterward)
    still answers commands rather than leaving the socket wedged."""
    assert qmp.execute("system_reset") is not None
    assert qmp.execute("query-status") is not None


def test_debug_break_on_exec_toggles(qmp):
    """The handler echoes {"enabled": <bool>}, so assert the echoed state
    rather than merely that a reply arrived. `is not None` would pass even
    if the server ignored the argument and never changed state at all.
    """
    enabled = qmp.execute("debug-break-on-exec", {"enabled": True})
    assert enabled["enabled"] is True

    disabled = qmp.execute("debug-break-on-exec", {"enabled": False})
    assert disabled["enabled"] is False


def test_an_unknown_command_is_an_error_not_a_hang(qmp):
    reply = qmp.execute_raw("no-such-command")
    assert "error" in reply


# -- ported from the retired test_qmp_server.py ---------------------------
#
# send-key acks successfully whenever its `keys` array is non-empty, even
# if every qcode in it is unrecognized (qmp.cpp:503-549 never checks
# whether kbd_keys ended up empty before calling send_success()). That
# means the legacy per-key-family tests (letters, digits, function keys,
# navigation, punctuation, keypad) could never have caught a broken or
# missing keymap entry -- a "yes" reply proves nothing about recognition,
# the same shape of test the fixtures already make redundant. They are not
# ported. What IS real protocol behaviour -- multi-key arrays, an empty
# array being rejected, and an unrecognized qcode being silently accepted
# rather than erroring -- is kept below. See task-11-report.md.

def test_send_key_accepts_multiple_simultaneous_keys(qmp):
    """The `keys` array holds keys pressed together (e.g. a modifier
    combination), looping over press-then-release in reverse order --
    a code path a single-key send-key never exercises."""
    assert qmp.execute("send-key", {"keys": [
        {"type": "qcode", "data": "shift"},
        {"type": "qcode", "data": "a"},
    ]}) is not None


def test_send_key_rejects_an_empty_key_list(qmp):
    """The legacy suite accepted either an error or silent success here.
    The server actually rejects it outright with GenericError."""
    reply = qmp.execute_raw("send-key", {"keys": []})
    assert "error" in reply
    assert reply["error"]["class"] == "GenericError"


def test_send_key_silently_ignores_an_unrecognized_qcode(qmp):
    """An unrecognized qcode is logged and dropped, not rejected -- pin
    down the behaviour the legacy suite accepted either side of."""
    assert qmp.execute("send-key", {"keys": [
        {"type": "qcode", "data": "not_a_real_key_name_xyz"},
    ]}) is not None


def test_input_send_event_accepts_a_key_release(qmp):
    """The conformance suite above only exercises `down: true`; the release
    half of the same command is a separate code path in qmp.cpp."""
    assert qmp.execute("input-send-event", {"events": [
        {"type": "key", "data": {"down": False,
                                 "key": {"type": "qcode", "data": "a"}}},
    ]}) is not None


def test_a_client_can_disconnect_and_reconnect(emulator):
    """Ported from the retired test_qmp_server.py. See the GDB twin: the
    suite otherwise never exercises a second connection to a live server.
    """
    first = RawQMP(port=emulator.qmp_port, timeout=10.0)
    first.connect()
    assert first.execute("query-status")["running"] is True
    first.close()

    second = RawQMP(port=emulator.qmp_port, timeout=10.0)
    second.connect()
    assert second.execute("query-status")["running"] is True, (
        "the server did not accept a second client after the first closed")
    second.close()


def test_system_reset_refuses_while_halted_for_gdb(emulator):
    """handle_memdump already refuses when it cannot produce a coherent
    result; system_reset is reachable on the same GDB-halted path and must
    not answer differently. Rebooting the guest while a GDB client still
    believes it is attached at a halt leaves that client looking at a
    session -- registers, memory, breakpoints -- that no longer exists.

    Both halves matter: the first proves the halted case is refused, the
    second proves system_reset was not simply broken outright.
    """
    gdb = emulator.gdb()
    qmp = emulator.qmp()

    gdb.halt()
    reply = qmp.execute_raw("system_reset")
    assert "error" in reply, "system_reset succeeded while halted for GDB"
    assert "halt" in reply["error"]["desc"].lower(), (
        f"refusal did not mention the halt: {reply['error']}")

    gdb.cont()
    deadline = time.time() + 5.0
    status = qmp.execute("query-status")
    while status.get("debug", {}).get("paused") and time.time() < deadline:
        time.sleep(0.1)
        status = qmp.execute("query-status")
    assert not status.get("debug", {}).get("paused"), (
        "debug pause never cleared after gdb.cont()")

    # Must not raise: system_reset succeeds once the debug halt is gone.
    qmp.execute("system_reset")



def test_greeting_does_not_advertise_oob(emulator):
    """QMP `oob` means the server accepts out-of-band commands -- ones
    carrying an "id" and executable while another command is in flight.
    This server processes commands strictly in order on one thread and no
    handler reads or echoes "id", so a client that trusts the advertisement
    waits for a reply that cannot arrive."""
    client = RawQMP(port=emulator.qmp_port, timeout=10.0)
    greeting = client.connect()
    try:
        capabilities = greeting["QMP"]["capabilities"]
        assert "oob" not in capabilities, (
            f"greeting advertises oob with no out-of-band path: "
            f"{capabilities!r}")
    finally:
        client.close()


def test_greeting_is_still_a_well_formed_qmp_banner(emulator):
    """Dropping oob must not break the shape QEMU clients parse."""
    client = RawQMP(port=emulator.qmp_port, timeout=10.0)
    greeting = client.connect()
    try:
        assert "QMP" in greeting
        assert greeting["QMP"]["version"]["package"] == "DOSBox-X"
        assert isinstance(greeting["QMP"]["capabilities"], list)
    finally:
        client.close()


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-v"]))
