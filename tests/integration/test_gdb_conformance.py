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

from protocol.gdb import RawGDB

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


# -- ported from the retired test_gdb_server.py --------------------------
#
# The tests below cover behaviour test_gdb_server.py asserted that the
# suite above does not: QStartNoAckMode, the `p` single-register packet,
# zero-length and odd-address/large memory reads, single-step, and
# breakpoints that are actually verified to fire independently rather than
# merely accepted. See task-11-report.md for the full retirement audit.

def test_qstartnoackmode_is_accepted(gdb):
    assert gdb.start_no_ack() is True


def test_p_packet_reads_a_single_register_matching_the_bulk_read(gdb):
    """`p` (single-register read) has no RawGDB wrapper -- send it directly.
    Every register must agree with what `g` (bulk read) reports."""
    gdb.halt()
    bulk = gdb.read_registers()
    for i in range(16):
        reply = gdb.send(f"p{i:x}")
        value = int.from_bytes(bytes.fromhex(reply), "little")
        assert value == bulk[i], (
            f"p{i:x} returned 0x{value:X}, bulk read said 0x{bulk[i]:X}")


def test_memory_read_returns_the_exact_length_requested(gdb):
    """Covers both size variation and an odd (unaligned) start address --
    x86 real mode has no alignment requirement, so both are one behaviour:
    `m` must return exactly the number of bytes asked for."""
    for size in (1, 2, 4, 7, 8, 16, 64, 256, 1024, 8192):
        data = gdb.read_memory(0xB8001, size)
        assert len(data) == size, f"asked for {size} bytes, got {len(data)}"


def test_memory_read_of_zero_length_returns_empty(gdb):
    assert gdb.read_memory(0xB8000, 0) == b""


def test_single_step_advances_the_program_counter(gdb):
    """`s` must execute at least one instruction and report SIGTRAP each
    time -- a code path `c` + breakpoint never exercises. The first step
    out of a freshly-written CS:EIP may cross more than one NOP, so this
    checks forward progress on every step rather than a fixed delta."""
    addr = 0x30100
    gdb.halt()
    assert gdb.write_memory(addr, b"\x90" * 6) is True
    assert gdb.write_register(10, addr >> 4) is True   # CS
    assert gdb.write_register(8, addr & 0xF) is True   # EIP

    last = linear_pc(gdb.read_registers())
    start = last
    for _ in range(5):
        gdb.step()
        stop = gdb.wait_for_stop(timeout=5.0)
        assert stop.startswith("S05"), f"step did not report SIGTRAP: {stop!r}"
        pc = linear_pc(gdb.read_registers())
        assert pc > last, f"step did not advance the PC (stuck at 0x{pc:X})"
        last = pc
    assert last > start


SECOND_LOOP_ADDR = 0x30010


def test_two_breakpoints_are_independent(gdb):
    """The legacy suite set and removed a batch of breakpoints without ever
    checking one fired. Verify the actual effect: two breakpoints each stop
    execution at their own address, and clearing one leaves the other armed.
    """
    gdb.halt()
    for addr in (LOOP_ADDR, SECOND_LOOP_ADDR):
        assert gdb.write_memory(addr, JMP_SELF) is True
        gdb.remove_breakpoint(addr)
    assert gdb.set_breakpoint(LOOP_ADDR) is True
    assert gdb.set_breakpoint(SECOND_LOOP_ADDR) is True

    gdb.write_register(10, LOOP_ADDR >> 4)
    gdb.write_register(8, LOOP_ADDR & 0xF)
    gdb.cont()
    stop = gdb.wait_for_stop(timeout=15.0)
    assert stop.startswith("S05")
    assert linear_pc(gdb.read_registers()) == LOOP_ADDR

    assert gdb.remove_breakpoint(LOOP_ADDR) is True
    gdb.write_register(10, SECOND_LOOP_ADDR >> 4)
    gdb.write_register(8, SECOND_LOOP_ADDR & 0xF)
    gdb.cont()
    stop2 = gdb.wait_for_stop(timeout=15.0)
    assert stop2.startswith("S05"), (
        "the second breakpoint never fired after the first was removed")
    assert linear_pc(gdb.read_registers()) == SECOND_LOOP_ADDR

    assert gdb.remove_breakpoint(SECOND_LOOP_ADDR) is True


def test_a_client_can_disconnect_and_reconnect(emulator):
    """Ported from the retired test_gdb_server.py. Every other test in this
    suite makes exactly ONE connection to a freshly spawned emulator, so a
    detach that failed to return the stub to its accept loop -- or to clear
    the debug-active state a GDB attach sets -- would leave the whole suite
    green.
    """
    first = RawGDB(port=emulator.gdb_port, timeout=10.0)
    first.connect()
    assert "PacketSize=" in first.supported()
    first.detach()
    first.close()

    second = RawGDB(port=emulator.gdb_port, timeout=10.0)
    second.connect()
    assert "PacketSize=" in second.supported(), (
        "the stub did not accept a second client after the first detached")
    second.close()


# -- hardening: malformed input must not kill the stub -------------------
#
# gdbserver.cpp had no try/catch anywhere. std::stoul/std::stoi in the
# handlers below throw std::invalid_argument or std::out_of_range on
# malformed input, and process_command runs on the main emulation thread --
# an uncaught throw there calls std::terminate and takes the whole emulator
# down, not just the debug connection.

MALFORMED_PACKETS = ["m,", "mzzzz,4", "Z0,zzzz,1", "P=1234", "Pzz=1234"]


def test_malformed_packets_get_an_error_reply_not_a_dead_emulator(gdb):
    """The point of this test is the liveness check at the end, not the
    individual error replies: a crash inside process_command would kill the
    connection (or the whole process) rather than politely returning E01."""
    for packet in MALFORMED_PACKETS:
        reply = gdb.send(packet)
        assert reply.startswith("E"), (
            f"packet {packet!r} did not get an error reply: {reply!r}")

    # The stub must still be alive and responsive after every malformed
    # packet above.
    features = gdb.supported()
    assert "PacketSize=" in features, (
        "stub did not answer qSupported after malformed input -- "
        "the connection (or emulator) did not survive")
    regs = gdb.read_registers()
    assert len(regs) == 16, (
        "stub did not answer a register read after malformed input")


# -- hardening: P must reject an out-of-range register --------------------
#
# DEBUG_SetRegister's switch has no default case, so an index above 15 was
# silently discarded while handle_write_register still replied "OK".

def test_write_register_rejects_an_out_of_range_index(gdb):
    reply = gdb.send("P10=00000000")  # index 16, one past GS (15)
    assert reply.startswith("E"), (
        f"P with index 16 should be rejected, got {reply!r}")


def test_write_register_still_accepts_a_valid_index(gdb):
    gdb.halt()
    assert gdb.write_register(3, 0xdeadbeef) is True
    assert gdb.read_registers()[3] == 0xdeadbeef


# -- regression: the protected-mode breakpoint guard must not affect real
#    mode ------------------------------------------------------------------
#
# DEBUG_SetBreakpoint/DEBUG_RemoveBreakpoint now refuse when
# cpu.pmode && !(reg_flags & FLAG_VM). This harness never enters protected
# mode, so this test always exercises the real-mode path and guards against
# that new check breaking it. The protected-mode branch itself is NOT
# reachable from this harness and is therefore unverified by test.

REAL_MODE_GUARD_ADDR = 0x30020


def test_breakpoint_above_64k_still_works_after_the_pmode_guard(gdb):
    _park_cpu_in_a_loop(gdb, REAL_MODE_GUARD_ADDR)
    gdb.remove_breakpoint(REAL_MODE_GUARD_ADDR)

    assert gdb.set_breakpoint(REAL_MODE_GUARD_ADDR) is True, (
        "real-mode breakpoint above 0x10000 was refused -- "
        "the protected-mode guard is over-broad")
    gdb.cont()
    stop = gdb.wait_for_stop(timeout=15.0)
    assert stop.startswith("S05"), (
        f"real-mode breakpoint above 0x10000 never fired (got {stop!r})")
    regs = gdb.read_registers()
    assert linear_pc(regs) == REAL_MODE_GUARD_ADDR, (
        f"stopped at 0x{linear_pc(regs):X}, expected 0x{REAL_MODE_GUARD_ADDR:X}")
    assert gdb.remove_breakpoint(REAL_MODE_GUARD_ADDR) is True


# -- M must honour its declared length, and report a write that fails ------

M_SCRATCH = 0x30300


def test_write_memory_rejects_a_payload_shorter_than_the_declared_length(gdb):
    """`M addr,length:XX...` carries the byte count twice. The stub used to
    ignore `length` entirely and write however many bytes the payload
    decoded to, so a truncated packet wrote a short block and still said OK
    -- the client believes it changed guest state it did not change."""
    gdb.halt()
    reply = gdb.send(f"M{M_SCRATCH:x},4:0011")
    assert reply == "E01", (
        f"M with a 2-byte payload declaring 4 bytes should be rejected, "
        f"got {reply!r}")


def test_write_memory_rejects_a_payload_longer_than_the_declared_length(gdb):
    """The over-long direction is the dangerous one: the extra bytes land in
    guest memory past the region the client asked to write."""
    gdb.halt()
    reply = gdb.send(f"M{M_SCRATCH:x},2:00112233")
    assert reply == "E01", (
        f"M with a 4-byte payload declaring 2 bytes should be rejected, "
        f"got {reply!r}")


def test_write_memory_rejects_an_odd_length_payload(gdb):
    """A payload that is not a whole number of hex byte pairs is malformed;
    hex_decode silently drops the trailing nibble."""
    gdb.halt()
    reply = gdb.send(f"M{M_SCRATCH:x},2:00112")
    assert reply == "E01", f"M with an odd-length payload got {reply!r}"


def test_write_memory_still_accepts_an_agreeing_length(gdb):
    """The guard must not reject well-formed packets."""
    gdb.halt()
    assert gdb.write_memory(M_SCRATCH, b"\xde\xad\xbe\xef") is True
    assert gdb.read_memory(M_SCRATCH, 4) == b"\xde\xad\xbe\xef"


def test_write_memory_of_zero_length_is_accepted(gdb):
    gdb.halt()
    assert gdb.send(f"M{M_SCRATCH:x},0:") == "OK"


# -- p and G must bound-check register indices the way P already does ------

def test_read_register_rejects_an_out_of_range_index(gdb):
    """DEBUG_GetRegister's `default: return 0` makes p10 (index 16, one past
    GS) answer 00000000, which a client cannot tell from a register that
    genuinely holds zero. The spec wants E NN for an unknown register."""
    gdb.halt()
    reply = gdb.send("p10")
    assert reply == "E01", (
        f"p with index 16 should be rejected, got {reply!r}")


def test_read_register_still_accepts_a_valid_index(gdb):
    gdb.halt()
    assert gdb.write_register(3, 0x1234) is True
    assert gdb.send("p3") == "34120000"


def test_write_registers_rejects_a_payload_with_too_many_registers(gdb):
    """G looped over whatever the client sent; indices past 15 fell through
    DEBUG_SetRegister's switch and were dropped, and the client got OK."""
    gdb.halt()
    reply = gdb.send("G" + "00000000" * 17)
    assert reply == "E01", (
        f"G with 17 registers should be rejected, got {reply!r}")


def test_write_registers_rejects_a_ragged_payload(gdb):
    """A G payload that is not a whole number of 8-hex-digit registers is
    malformed; the trailing partial word was silently discarded."""
    gdb.halt()
    reply = gdb.send("G" + "00000000" * 2 + "0011")
    assert reply == "E01", f"G with a ragged payload got {reply!r}"


def test_write_registers_still_accepts_sixteen_registers(gdb):
    gdb.halt()
    before = gdb.read_registers()
    payload = "".join(v.to_bytes(4, "little").hex() for v in before)
    assert gdb.send("G" + payload) == "OK"



if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-v"]))
