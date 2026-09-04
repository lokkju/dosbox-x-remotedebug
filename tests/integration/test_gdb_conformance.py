#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.11"
# dependencies = [
#     "pytest>=8.0",
# ]
# ///
"""Conformance tests for the GDB stub.

These assert what the SERVER does. Each gets a fresh emulator because they
halt the CPU, write guest memory and move the program counter.

Register 8 is EIP, an offset within CS. The linear PC is
registers[10] * 16 + registers[8].
"""

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent))

CS = 10
EIP = 8


def linear_pc(registers: list) -> int:
    return registers[CS] * 16 + registers[EIP]


def test_p_packet_writes_a_single_register(gdb):
    """Without P a client must use G, which rewrites all sixteen registers
    at once -- and G is exactly the operation the EIP asymmetry corrupts."""
    gdb.halt()
    assert gdb.write_register(3, 0x1234) is True, (
        "the stub did not accept a P packet")
    assert gdb.read_registers()[3] == 0x1234


def test_register_eight_is_eip_not_a_linear_address(gdb):
    """RSP defines register 8 as EIP. Reporting SegPhys(cs)+reg_eip is the
    same class of non-conformance as a packed Z0 argument: real gdb shows a
    wrong $pc and every $pc-relative expression is wrong."""
    gdb.halt()
    regs = gdb.read_registers()
    assert regs[EIP] <= 0xFFFF, (
        f"register 8 read back as 0x{regs[EIP]:X}, which is outside a 16-bit "
        f"offset -- the stub is still reporting a linear PC")


def test_writing_eip_then_reading_it_round_trips(gdb):
    """The asymmetry meant a g/G round-trip silently moved the PC."""
    gdb.halt()
    before = gdb.read_registers()
    assert gdb.write_register(EIP, before[EIP]) is True
    after = gdb.read_registers()
    assert after[EIP] == before[EIP]
    assert linear_pc(after) == linear_pc(before)


def test_the_linear_pc_points_at_executable_memory(gdb):
    """Cross-check that cs * 16 + eip names a byte the m packet can read."""
    gdb.halt()
    regs = gdb.read_registers()
    assert len(gdb.read_memory(linear_pc(regs), 1)) == 1


# A linear address comfortably above 0x10000, which is where the packed and
# linear interpretations of a Z0 argument stop coinciding.
LOOP_ADDR = 0x30000
JMP_SELF = b"\xeb\xfe"


def _park_cpu_in_a_loop(gdb, addr: int = LOOP_ADDR) -> None:
    """Halt, plant `jmp $` at `addr`, and point CS:IP at it.

    CS and EIP are written separately via P. There is no single register
    holding a linear PC to write.
    """
    gdb.halt()
    assert gdb.write_memory(addr, JMP_SELF) is True
    assert gdb.read_memory(addr, 2) == JMP_SELF
    assert gdb.write_register(10, addr >> 4) is True   # CS
    assert gdb.write_register(8, addr & 0xF) is True   # EIP, an offset


def test_breakpoint_above_64k_fires_at_the_linear_address(gdb):
    """Z0 must take a linear address, the same as m and M.

    Before the fix DEBUG_SetBreakpoint split this argument with
    FP_SEG(x) = x >> 16, so 0x30000 became 0003:0000 -- physical 0x30 --
    which answers OK and never fires.
    """
    _park_cpu_in_a_loop(gdb)
    gdb.remove_breakpoint(LOOP_ADDR)

    assert gdb.set_breakpoint(LOOP_ADDR) is True
    gdb.cont()
    stop = gdb.wait_for_stop(timeout=15.0)
    assert stop.startswith("S05"), (
        f"breakpoint at linear 0x{LOOP_ADDR:X} never fired (got {stop!r}). "
        f"The stub is interpreting the Z0 argument as a packed far pointer.")

    regs = gdb.read_registers()
    assert linear_pc(regs) == LOOP_ADDR, (
        f"stopped at 0x{linear_pc(regs):X}, expected 0x{LOOP_ADDR:X}")


def test_breakpoint_and_memory_agree_on_what_an_address_is(gdb):
    """The byte `m` reads at L is the byte execution stops on at L."""
    _park_cpu_in_a_loop(gdb)
    assert gdb.read_memory(LOOP_ADDR, 2) == JMP_SELF
    assert gdb.set_breakpoint(LOOP_ADDR) is True
    gdb.cont()
    assert gdb.wait_for_stop(timeout=15.0).startswith("S05")
    regs = gdb.read_registers()
    assert gdb.read_memory(linear_pc(regs), 2) == JMP_SELF


def test_removing_a_breakpoint_lets_execution_continue(gdb):
    _park_cpu_in_a_loop(gdb)
    assert gdb.set_breakpoint(LOOP_ADDR) is True
    gdb.cont()
    assert gdb.wait_for_stop(timeout=15.0).startswith("S05")

    assert gdb.remove_breakpoint(LOOP_ADDR) is True
    gdb.cont()
    assert gdb.wait_for_stop(timeout=3.0) == "", (
        "execution stopped again after the breakpoint was removed")


def test_qsupported_advertises_linear_breakpoints(gdb):
    """Z0 answers OK whichever way it reads the address, so a client cannot
    detect the semantics by probing. It has to be advertised."""
    features = gdb.supported()
    assert "dosbox-x-linear-bp+" in features, (
        f"qSupported did not advertise linear breakpoints: {features!r}")


def test_qsupported_advertises_eip_as_an_offset(gdb):
    """Register 8 returns a plausible number under either interpretation, so
    a client that guesses wrong computes a wrong PC and never finds out."""
    features = gdb.supported()
    assert "dosbox-x-eip-offset+" in features, (
        f"qSupported did not advertise EIP semantics: {features!r}")


def test_qsupported_still_advertises_the_stock_features(gdb):
    features = gdb.supported()
    for feature in ("PacketSize=", "swbreak+", "hwbreak+",
                    "vContSupported+", "QStartNoAckMode+"):
        assert feature in features, f"lost {feature} from qSupported"


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-v"]))
