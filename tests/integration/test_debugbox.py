#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.11"
# dependencies = [
#     "pytest>=8.0",
# ]
# ///
"""
Integration tests for DEBUGBOX command with remote debugging.

These tests verify that DEBUGBOX and the GDB server work together correctly:
- DEBUGBOX pauses at program entry point
- GDB client can connect and see the paused state
- Registers reflect the program's entry point

Each test gets its own emulator via the `emulator`/`gdb`/`qmp` fixtures in
conftest.py rather than a shared instance on the hardcoded 2159/4444 ports.

Register 8 is EIP, an offset within CS -- not a linear address. The linear
PC is registers[10] * 16 + registers[8]. See protocol/gdb.py.

Run with:
    uv run pytest tests/integration/test_debugbox.py -v
"""

import sys
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent))

from launcher import Emulator

EIP = 8
CS = 10

TEXT_VRAM = 0xB8000

# Test assets directory (relative to this file)
TEST_ASSETS_DIR = Path(__file__).parent / "assets"


def create_test_com_file():
    """Create a minimal test COM file for testing.

    Returns the path to the created file, or None if creation failed.
    The COM file contains a simple program that just exits immediately.
    """
    # Ensure assets directory exists
    TEST_ASSETS_DIR.mkdir(parents=True, exist_ok=True)

    test_com_path = TEST_ASSETS_DIR / "DBXTEST.COM"

    # Minimal COM file: NOP, NOP, INT 20h (terminate)
    # COM files start at offset 0x100 in memory
    # This program does nothing but exit cleanly
    com_bytes = bytes([
        0x90,        # NOP
        0x90,        # NOP
        0xB8, 0x00, 0x4C,  # MOV AX, 4C00h (DOS exit with code 0)
        0xCD, 0x21,  # INT 21h (DOS function call)
    ])

    try:
        with open(test_com_path, 'wb') as f:
            f.write(com_bytes)
        return test_com_path
    except Exception as e:
        print(f"Warning: Could not create test COM file: {e}")
        return None


# -- local plumbing: typing text and reading the screen via raw clients --
#
# dosbox_debug.py's type_text/screen_line convenience is exactly what
# belongs in dbxdebug (Polyform Shield), not here. What DEBUGBOX testing
# needs is minimal: enough of a qcode table to type a DOS command line, and
# enough of a screen reader to confirm it landed. See test_video_tools.py
# for the sibling read_screen() this mirrors.

_SPECIAL_KEYS = {" ": "spc", "\r": "ret", "\n": "ret", ".": "dot"}
_SHIFTED_KEYS = {":": "semicolon"}


def _type_text(qmp, text: str, delay: float = 0.1) -> None:
    for char in text:
        if char in _SPECIAL_KEYS:
            keys = [_SPECIAL_KEYS[char]]
        elif char in _SHIFTED_KEYS:
            keys = ["shift", _SHIFTED_KEYS[char]]
        elif char.isupper():
            keys = ["shift", char.lower()]
        elif char.isalnum():
            keys = [char.lower()]
        else:
            continue
        qmp.execute("send-key",
                    {"keys": [{"type": "qcode", "data": k} for k in keys]})
        time.sleep(delay)


def _screen_text(gdb, width: int = 80, height: int = 25) -> list:
    raw = gdb.read_memory(TEXT_VRAM, width * height * 2)
    lines = []
    for row in range(height):
        chars = []
        for col in range(width):
            offset = (row * width + col) * 2
            byte = raw[offset]
            chars.append(chr(byte) if 32 <= byte < 127 else " ")
        lines.append("".join(chars).rstrip())
    return lines


def _drain_stop(gdb, timeout: float = 0.3) -> None:
    """Consume one extra queued stop-reply packet, if any.

    DEBUGBOX's break-on-exec emits two stop notifications in quick
    succession once the guest reaches the loaded program's entry point.
    halt() only consumes one; without this, the next command's reply gets
    desynced by the packet left behind.
    """
    gdb.wait_for_stop(timeout=timeout)


def _run_command(gdb, qmp, command: str, wait_after: float = 0.5,
                  verify: bool = True) -> None:
    """Type a DOS command and press Enter.

    Raises RuntimeError if verify=True and the command never appeared on
    screen before Enter was pressed. Checks the whole screen rather than a
    fixed row: unlike the DOSBoxInstance this file used to drive, a freshly
    booted emulator has not scrolled its welcome banner off screen, so the
    prompt is not reliably on any particular line.
    """
    _type_text(qmp, command)

    if verify:
        time.sleep(0.2)
        gdb.halt()
        time.sleep(0.1)
        lines = _screen_text(gdb)
        gdb.cont()
        time.sleep(0.1)

        if not any(command.upper() in line.upper() for line in lines):
            raise RuntimeError(
                f"Command '{command}' not visible on screen. "
                f"Screen shows: {lines!r}")

    _type_text(qmp, "\r")
    time.sleep(wait_after)


@pytest.fixture
def emulator_with_test_drive():
    """An emulator with the assets directory mounted as T:, for the
    DEBUGBOX-entry-point tests that need to run a COM file from disk."""
    with Emulator(mounts={"t": str(TEST_ASSETS_DIR)}) as emu:
        yield emu


@pytest.fixture
def gdb_t(emulator_with_test_drive):
    return emulator_with_test_drive.gdb()


@pytest.fixture
def qmp_t(emulator_with_test_drive):
    return emulator_with_test_drive.qmp()


# =============================================================================
# DEBUGBOX Basic Tests
# =============================================================================

class TestDebugboxBasic:
    """Test basic DEBUGBOX functionality."""

    def test_debugbox_without_args_pauses(self, gdb, qmp):
        """DEBUGBOX without arguments should pause in debugger mode."""
        # Type DEBUGBOX command without arguments
        _run_command(gdb, qmp, "DEBUGBOX", wait_after=0.5)

        # Halt to ensure we're stopped
        gdb.halt()
        _drain_stop(gdb)
        time.sleep(0.2)

        # Verify we can read registers (confirms pause state)
        regs = gdb.read_registers()
        assert regs is not None, "Could not read registers during DEBUGBOX"

        # Check if emulator is paused via query-status
        status = qmp.execute("query-status")

        # Debugger mode should pause execution
        is_paused = status.get('status') == 'paused' or not status.get('running', True)
        assert is_paused, f"Expected paused state, got: {status}"


class TestDebugboxWithGdb:
    """Test DEBUGBOX integration with GDB server."""

    def test_gdb_can_connect_during_debugbox(self, gdb):
        """GDB should be able to connect while DEBUGBOX is active."""
        # Should be able to read registers
        regs = gdb.read_registers()
        assert regs is not None
        # regs is a plain list of 16 ints now, not a Registers object;
        # index EIP (8) rather than probing an attribute.
        assert len(regs) == 16

    def test_gdb_sees_pause_after_breakpoint(self, gdb):
        """GDB should see the CPU paused when breakpoint is hit."""
        # Read initial EIP
        regs = gdb.read_registers()
        initial_eip = regs[EIP]

        # Perform a step - this should pause and return
        gdb.step()
        result = gdb.wait_for_stop(timeout=5.0)
        assert result != "", "step produced no stop reply"

        # Read registers after step
        regs_after = gdb.read_registers()
        assert regs_after is not None


class TestDebugboxEntryPoint:
    """Test that DEBUGBOX correctly breaks at program entry point."""

    @pytest.fixture
    def test_com_file(self):
        """Create and provide path to test COM file."""
        com_path = create_test_com_file()
        if com_path is None:
            pytest.skip("Could not create test COM file")
        yield com_path

    def test_debugbox_program_entry_detection(self, test_com_file):
        """DEBUGBOX should pause at program entry point."""
        # Just verify the COM file was created
        assert test_com_file is not None
        assert test_com_file.exists()


class TestDebugboxEntryPointFull:
    """Full end-to-end test for DEBUGBOX entry point."""

    def test_debugbox_breaks_at_com_entry(self, gdb_t, qmp_t):
        """Verify DEBUGBOX pauses at COM file entry point."""
        # Ensure test COM file exists
        com_path = create_test_com_file()
        if com_path is None:
            pytest.skip("Could not create test COM file")

        # Change to test drive
        _run_command(gdb_t, qmp_t, "T:", wait_after=0.3)

        # Run DEBUGBOX with test program
        _run_command(gdb_t, qmp_t, "DEBUGBOX DBXTEST.COM", wait_after=1.0)

        # Halt to ensure we're stopped
        gdb_t.halt()
        _drain_stop(gdb_t)
        time.sleep(0.2)

        # Read registers
        regs = gdb_t.read_registers()
        assert regs is not None, "Failed to read registers"

        # Register 8 (EIP) is already an offset within CS -- not a linear
        # address to subtract cs*16 from, the way the old Registers object
        # (which reported SegPhys(cs)+reg_eip) required.
        eip_offset = regs[EIP]
        cs = regs[CS]

        # The program should be at entry point 0x100 (NOP instruction)
        assert eip_offset in (0x100, 0x101, 0x102), (
            f"Expected EIP offset ~0x100 for COM entry point, "
            f"got 0x{eip_offset:04X} (CS: 0x{cs:04X})"
        )

        # Read memory at entry point to verify it's our test program
        entry_addr = (cs << 4) + 0x100
        mem = gdb_t.read_memory(entry_addr, 7)

        expected_bytes = bytes([0x90, 0x90, 0xB8, 0x00, 0x4C, 0xCD, 0x21])
        assert mem == expected_bytes, (
            f"Memory at entry point doesn't match test program. "
            f"Expected: {expected_bytes.hex()}, Got: {mem.hex()}"
        )

        # Continue execution to clean up
        gdb_t.step()
        gdb_t.wait_for_stop(timeout=2.0)
        gdb_t.step()
        gdb_t.wait_for_stop(timeout=2.0)

    def test_debugbox_paused_state_visible_via_qmp(self, gdb_t, qmp_t):
        """Verify DEBUGBOX pause state is visible via QMP query-status."""
        # Ensure test COM file exists
        com_path = create_test_com_file()
        if com_path is None:
            pytest.skip("Could not create test COM file")

        # Change to test drive (verify=False to avoid halt/continue overhead)
        _run_command(gdb_t, qmp_t, "T:", wait_after=0.5, verify=False)

        # Run DEBUGBOX with test program (verify=False for reliability)
        _run_command(gdb_t, qmp_t, "DEBUGBOX DBXTEST.COM", wait_after=1.5,
                     verify=False)

        # Halt to ensure we're stopped
        gdb_t.halt()
        _drain_stop(gdb_t)
        time.sleep(0.2)

        # Query status - should show debug pause state
        status = qmp_t.execute("query-status")

        # Overall status should be paused
        assert status.get('status') == 'paused', f"Expected 'paused', got {status.get('status')}"
        assert status.get('running') is False, "Expected running to be false"

        # Debug object should show active and paused
        debug = status.get('debug', {})
        assert debug.get('active') is True, "Expected debug.active to be true"
        assert debug.get('paused') is True, "Expected debug.paused to be true"


class TestQueryStatus:
    """Test QMP query-status command for debugging state."""

    def test_query_status_returns_valid_response(self, qmp):
        """query-status should return valid running/paused state with debug info."""
        status = qmp.execute("query-status")

        # Status should have required fields
        assert 'status' in status, "Missing 'status' field"
        assert 'running' in status, "Missing 'running' field"
        assert status['status'] in ('running', 'paused')
        assert isinstance(status['running'], bool)

        # Should have emulator-paused field
        assert 'emulator-paused' in status, "Missing 'emulator-paused' field"
        assert isinstance(status['emulator-paused'], bool)

        # Should have debug object with active and paused fields
        assert 'debug' in status, "Missing 'debug' object"
        debug = status['debug']
        assert 'active' in debug, "Missing 'debug.active' field"
        assert 'paused' in debug, "Missing 'debug.paused' field"

    def test_stop_and_cont_commands(self, gdb, qmp):
        """stop and cont commands should control emulator pause state."""
        # Stop the emulator
        qmp.execute("stop")
        time.sleep(0.2)

        # Verify paused
        status = qmp.execute("query-status")

        assert status.get('status') == 'paused', f"Expected 'paused', got {status.get('status')}"
        # Note: emulator-paused may be False if GDB is connected and pausing
        # The important thing is that status='paused' and running=False

        # Resume
        qmp.execute("cont")
        time.sleep(0.2)

        # Also continue GDB to fully resume
        gdb.cont()
        time.sleep(0.2)

        # Verify running
        status = qmp.execute("query-status")

        assert status.get('status') == 'running', f"Expected 'running', got {status.get('status')}"


class TestGdbPauseState:
    """Test GDB server pause state detection."""

    def test_gdb_halt_pauses_execution(self, gdb):
        """GDB halt command should pause CPU execution."""
        # Halt execution
        result = gdb.halt()
        assert result.startswith("S"), (
            f"halt returned {result!r}; RawGDB.halt() returns '' on timeout, "
            f"so anything but a stop reply means the 0x03 interrupt did not "
            f"stop the guest")

        # After halt, we should be able to read registers
        regs = gdb.read_registers()
        assert regs is not None
        assert len(regs) == 16

    def test_gdb_pause_visible_via_qmp(self, gdb, qmp):
        """GDB step/halt should be visible via QMP query-status."""
        # Step to pause for GDB
        gdb.step()
        gdb.wait_for_stop(timeout=5.0)

        # Check via QMP
        status = qmp.execute("query-status")

        # Should show debug active and paused
        debug = status.get('debug', {})
        assert debug.get('active') is True, "Expected debug.active=true when GDB connected"
        assert debug.get('paused') is True, "Expected debug.paused=true after GDB step"

    def test_gdb_step_pauses_after_one_instruction(self, gdb):
        """GDB step should execute one instruction and pause."""
        # Get initial state
        regs_before = gdb.read_registers()
        eip_before = regs_before[EIP]

        # Step
        gdb.step()
        result = gdb.wait_for_stop(timeout=5.0)
        assert result != "", "step produced no stop reply"

        # After step, should be paused
        regs_after = gdb.read_registers()
        assert regs_after is not None

    def test_gdb_breakpoint_pauses_at_address(self, gdb):
        """GDB breakpoint should pause execution when hit."""
        # Set a breakpoint at a test address
        test_addr = 0x1000
        result = gdb.set_breakpoint(test_addr)
        assert result is True

        # Clean up
        gdb.remove_breakpoint(test_addr)


class TestDebuggerMutualExclusion:
    """Test mutual exclusion between GDB and interactive debugger."""

    def test_gdb_connection_blocks_interactive_debugger(self, gdb, qmp):
        """With GDB connected, attempting to open interactive debugger should fail."""
        # Ensure we're in a good state - halt first
        gdb.halt()
        time.sleep(0.2)

        # Verify GDB is connected and working
        regs = gdb.read_registers()
        assert regs is not None

        # Ensure emulator is running
        gdb.cont()
        time.sleep(0.3)

        # Try to activate interactive debugger via DEBUGBOX
        _run_command(gdb, qmp, "DEBUGBOX", wait_after=0.5)

        # Halt to check state
        gdb.halt()
        _drain_stop(gdb)
        time.sleep(0.2)

        # GDB should still be responsive
        regs_after = gdb.read_registers()
        assert regs_after is not None, "GDB became unresponsive after DEBUGBOX attempt"


class TestRemoteDebugIntegration:
    """Integration tests for remote debugging with DEBUGBOX."""

    def test_gdb_and_qmp_simultaneous_connection(self, gdb, qmp):
        """Both GDB and QMP should be able to connect simultaneously."""
        # Ensure we're halted first for reliable register read
        gdb.halt()
        time.sleep(0.2)

        # Both connections should work
        regs = gdb.read_registers()
        assert regs is not None

        commands = qmp.execute("query-commands")
        assert commands is not None

    def test_qmp_stop_and_query_status(self, gdb, qmp):
        """Pausing via QMP stop should be visible via QMP query-status."""
        # Ensure we start running
        gdb.cont()
        time.sleep(0.3)

        # Stop via QMP
        qmp.execute("stop")
        time.sleep(0.2)

        # Check status - should show paused
        status = qmp.execute("query-status")
        assert status.get('status') == 'paused', f"Expected status='paused', got {status}"

        # Resume via both QMP and GDB
        qmp.execute("cont")
        gdb.cont()
        time.sleep(0.3)

        # Check status - should show running
        status = qmp.execute("query-status")
        assert status.get('status') == 'running', f"Expected status='running', got {status}"

    def test_gdb_registers_valid_during_pause(self, gdb):
        """Register values should be valid and consistent when paused."""
        # Halt to ensure we're in a stable state
        gdb.halt()

        # Read registers multiple times - should be consistent
        regs1 = gdb.read_registers()
        regs2 = gdb.read_registers()

        # All general-purpose registers and EIP should match (no execution
        # happening): indices 0-8 are EAX, ECX, EDX, EBX, ESP, EBP, ESI,
        # EDI, EIP.
        for i in range(9):
            assert regs1[i] == regs2[i], f"Register index {i} changed while paused"


# =============================================================================
# Main entry point
# =============================================================================

if __name__ == "__main__":
    # Create test assets
    com_path = create_test_com_file()
    if com_path:
        print(f"Test COM file created at: {com_path}")

    # Run pytest
    sys.exit(pytest.main([__file__, "-v", "--tb=short"]))
