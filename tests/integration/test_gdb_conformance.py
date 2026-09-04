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


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-v"]))
