# DOSBox-X Remote Debugging

DOSBox-X includes remote debugging capabilities for external tools to connect and interact with the emulator:

- **GDB Server** - Debug DOS programs using GDB or compatible debuggers
- **QMP Server** - Inject keyboard input using QEMU Monitor Protocol

## Building

Remote debugging requires POSIX sockets and is supported on Linux, BSD, and macOS.

```bash
./build-debug --enable-remotedebug
```

Or manually:
```bash
./configure --enable-debug --enable-remotedebug
make
```

## Configuration

Add to the `[dosbox]` section of your configuration file:

```ini
[dosbox]
# GDB remote debugging server
gdbserver=true
gdbserver port=2159

# QMP keyboard input server
qmpserver=true
qmpserver port=4444
```

| Option | Default | Description |
|--------|---------|-------------|
| `gdbserver` | `false` | Enable GDB remote debugging server |
| `gdbserver port` | `2159` | GDB server TCP port |
| `qmpserver` | `false` | Enable QMP keyboard input server |
| `qmpserver port` | `4444` | QMP server TCP port |

Servers can be toggled at runtime via **Debug > GDB Server** and **Debug > QMP Server** menus.

### Logging

Control remote debugging verbosity via the `[log]` section:

```ini
[log]
remote=debug    # debug, normal (default), warn, error, fatal, never
```

---

## GDB Server

Implements the [GDB Remote Serial Protocol](https://sourceware.org/gdb/current/onlinedocs/gdb.html/Remote-Protocol.html) for debugging DOS programs.

### Connecting

```bash
gdb
(gdb) target remote localhost:2159
```

### Register Mapping

| Index | Register |
|-------|----------|
| 0-7 | EAX, ECX, EDX, EBX, ESP, EBP, ESI, EDI |
| 8 | EIP -- an offset within CS, **not** a linear address |
| 9 | EFLAGS |
| 10-15 | CS, SS, DS, ES, FS, GS |

Register 8 is EIP exactly as the CPU holds it: an offset within the CS
segment. To get the linear program counter, compute it yourself as
`cs * 16 + eip` from registers 10 and 8. Builds that advertise
`dosbox-x-eip-offset+` in `qSupported` (see [Capability
Negotiation](#capability-negotiation) below) behave this way. Older builds
returned `SegPhys(cs) + reg_eip` (a linear value) when reading register 8,
while writing register 8 still wrote `reg_eip` directly -- so reading `g`
and writing it back with `G` silently moved the program counter.

Registers can also be written one at a time with `P<n>=<value>`, where `n`
and `value` are hex and `value` uses the same little-endian-swapped 32-bit
wire format as `g`/`G`. This is in addition to the existing `p<n>` (read one
register) and full-register `g`/`G`.

### Memory Addressing

Addresses are linear physical. For real-mode: `(segment << 4) + offset`.

`m`, `M`, `Z0` and `z0` (read memory, write memory, set breakpoint, remove
breakpoint) all take this same linear address -- there is no far-pointer
form for any of them. Builds that advertise `dosbox-x-linear-bp+` in
`qSupported` (see below) behave this way. Older builds split the `Z0`/`z0`
argument as a packed far pointer instead (`FP_SEG`/`FP_OFF` on the raw hex
value), so any breakpoint address at or above `0x10000` answered `OK` but
never fired; below `0x10000` the two interpretations happen to agree, which
is why it looked like it worked.

### Capability Negotiation

`qSupported` advertises two vendor feature strings naming the two behaviors
above:

- `dosbox-x-linear-bp+` -- `Z0`/`z0` take a linear address.
- `dosbox-x-eip-offset+` -- register 8 is EIP, an offset within CS.

Real GDB ignores vendor features it does not recognize, so both stay
protocol-legal against any client. A build that does not advertise a
feature behaves the corresponding older way described above.

### Limitations

- Software breakpoints only (hardware breakpoints/watchpoints not implemented)
- Breakpoints set via GDB do not work while the guest is in protected mode.
  `DEBUG_SetBreakpoint` stores the breakpoint against segment 0, and in
  protected mode `GetAddress(0, off)` routes through `LinMakeProt(0, off)`,
  which rejects selector 0 and reports no address -- so the breakpoint is
  stored but never matches. This is pre-existing (the old packed-far-pointer
  form was also broken in protected mode, for a different reason), and the
  conformance suite (`tests/integration/test_gdb_conformance.py`) only
  asserts real-mode behavior.
- A QMP `system_reset` issued while the CPU is halted for GDB now actually
  reboots the guest -- previously the request sat pending forever and never
  ran. The GDB client is not notified of the reset, so it remains attached
  to a stale halt against a rebooted memory image; if you issue a reset
  while stopped at a breakpoint, reconnect afterward rather than trusting
  the existing session.

---

## QMP Server

Implements a subset of [QEMU Monitor Protocol](https://www.qemu.org/docs/master/interop/qemu-qmp-ref.html) for keyboard input injection. Compatible with QEMU tooling.

### Connecting

Connect via TCP and send JSON commands. The server sends a greeting on connect:

```json
{"QMP": {"version": {...}, "capabilities": ["oob"]}}
```

Acknowledge capabilities to enter command mode:

```json
{"execute": "qmp_capabilities"}
```

### Commands

#### send-key

Press keys simultaneously, auto-release after hold-time:

```json
{"execute": "send-key", "arguments": {
  "keys": [
    {"type": "qcode", "data": "ctrl"},
    {"type": "qcode", "data": "alt"},
    {"type": "qcode", "data": "delete"}
  ],
  "hold-time": 100
}}
```

- `keys`: Array of key objects with `type: "qcode"` and `data: "<keyname>"`
- `hold-time`: Milliseconds before releasing (default: 100)

#### input-send-event

Explicit key press/release control:

```json
{"execute": "input-send-event", "arguments": {
  "events": [{
    "type": "key",
    "data": {"down": true, "key": {"type": "qcode", "data": "a"}}
  }]
}}
```

#### Mouse Events (input-send-event)

Mouse movement and button clicks:

```json
{"execute": "input-send-event", "arguments": {
  "events": [
    {"type": "rel", "data": {"axis": "x", "value": 50}},
    {"type": "rel", "data": {"axis": "y", "value": -30}},
    {"type": "btn", "data": {"button": "left", "down": true}},
    {"type": "btn", "data": {"button": "left", "down": false}}
  ]
}}
```

- `type: "rel"` - Relative mouse movement (`axis`: `x`/`y`, `value`: pixels)
- `type: "btn"` - Mouse button (`button`: `left`/`right`/`middle`, `down`: true/false)

#### debug-break-on-exec

Enable breakpoint on program entry for GDB debugging:

```json
{"execute": "debug-break-on-exec", "arguments": {"enabled": true}}
```

When enabled, DOSBox-X will automatically set a breakpoint when DOS loads an EXE/COM program. If a GDB client is connected, it receives the S05 (SIGTRAP) notification at the program entry point.

Response: `{"return": {"enabled": true}}`

#### memdump

Dump a range of guest memory to a file:

```json
{"execute": "memdump", "arguments": {"address": 753664, "size": 4000, "file": "/tmp/screen.bin"}}
```

- `address`, `size`: decimal, linear address and byte count. `size` is
  capped at 16 MB.
- `file`: optional; if omitted, a temp file is created and its path is
  returned.

**The CPU must be stopped before calling `memdump`.** The handler reads
guest memory directly from the QMP server thread instead of handing the
read off to the emulation thread, which is only safe when the emulation
thread is not executing -- reading live while the guest runs would race it.
"Stopped" means any of: halted for GDB (Ctrl-C, a breakpoint, or a `?`
query), paused in the interactive debugger, or paused via a QMP `stop`
command. Calling `memdump` against a running guest returns an error instead
of reading:

```json
{"error": {"class": "GenericError", "desc": "memdump requires the CPU to be stopped for debugging; halt via GDB or QMP stop first"}}
```

### Pending Work While Halted for GDB

`savestate`/`loadstate`, emulator control (pause/resume/reset), and queued
keyboard/mouse input from QMP are now serviced while the CPU is halted for
GDB -- at a breakpoint, after Ctrl-C, or between single steps -- in addition
to while it runs freely. Previously none of this ran on the halted path: a
`savestate` issued while stopped at a breakpoint sat until it timed out, and
`send-key` input typed while halted was silently dropped instead of queuing
for when execution resumed.

### Key Names (QKeyCode)

Standard QEMU key names: `a`-`z`, `0`-`9`, `f1`-`f12`, `ret`, `esc`, `tab`, `spc`, `shift`, `ctrl`, `alt`, `caps_lock`, `left`, `right`, `up`, `down`, `insert`, `delete`, `home`, `end`, `pgup`, `pgdn`, `kp_0`-`kp_9`, etc.

See [QEMU QKeyCode](https://www.qemu.org/docs/master/interop/qemu-qmp-ref.html) for full list.

---

## Debugging Program Entry Points

There are two approaches for setting breakpoints at a program's entry point.

### Entry Point Addresses

When a breakpoint is hit at program entry:
- **EXE files**: CS:IP points to segment:0000 (entry at start of code segment)
- **COM files**: CS:IP points to segment:0100 (code starts after 256-byte PSP)

The segment address varies based on available memory. Register 8 (EIP) is
already the offset within CS -- no further calculation is needed. If you
want the linear address instead, compute `CS * 16 + EIP`.

### Option 1: DEBUGBOX Shell Command

The built-in `DEBUGBOX` command sets a breakpoint at program entry:

```
C:\> DEBUGBOX MYGAME.EXE
```

**Important**: A GDB client must be connected BEFORE running DEBUGBOX to receive the breakpoint notification (S05/SIGTRAP).

#### Example with GDB

Terminal 1 - Connect GDB first:
```bash
gdb
(gdb) target remote localhost:2159
```

Terminal 2 - Type DEBUGBOX command via QMP:
```bash
# Initialize QMP
echo '{"execute": "qmp_capabilities"}' | nc localhost 4444

# Type the command (each key separately)
for key in d e b u g b o x spc m y g a m e dot e x e ret; do
  echo "{\"execute\": \"send-key\", \"arguments\": {\"keys\": [{\"type\": \"qcode\", \"data\": \"$key\"}]}}" | nc localhost 4444
  sleep 0.05
done
```

Terminal 1 - GDB receives breakpoint:
```
(gdb) info registers
eax    0x0      0
...
eip    0x0      0x0       # Offset within CS -- already the entry offset
cs     0x824    0x824     # Code segment
...
# Linear address = CS * 16 + EIP = 0x824 * 16 + 0x0 = 0x8240 (EXE entry point)
```

### Option 2: QMP debug-break-on-exec

For automation without using the DEBUGBOX command:

**Important**: Connect GDB BEFORE enabling debug-break-on-exec to receive S05.

#### Complete Example

```bash
#!/bin/bash
# debug_program.sh - Debug a DOS program at entry point

PROGRAM="MYGAME.EXE"
QMP_PORT=4444
GDB_PORT=2159

# Step 1: Connect GDB in background (must be first!)
(echo "target remote localhost:$GDB_PORT" | gdb -batch -x -) &
GDB_PID=$!
sleep 0.5

# Step 2: Initialize QMP and enable break-on-exec
{
  echo '{"execute": "qmp_capabilities"}'
  sleep 0.1
  echo '{"execute": "debug-break-on-exec", "arguments": {"enabled": true}}'
  sleep 0.1
} | nc localhost $QMP_PORT

# Step 3: Type the program name
for char in $(echo "$PROGRAM" | grep -o .); do
  key=$(echo "$char" | tr '[:upper:]' '[:lower:]')
  case "$char" in
    .) key="dot" ;;
    " ") key="spc" ;;
  esac
  echo "{\"execute\": \"send-key\", \"arguments\": {\"keys\": [{\"type\": \"qcode\", \"data\": \"$key\"}]}}" | nc localhost $QMP_PORT
  sleep 0.05
done

# Step 4: Press Enter to execute
echo '{"execute": "send-key", "arguments": {"keys": [{"type": "qcode", "data": "ret"}]}}' | nc localhost $QMP_PORT

# GDB will receive S05 when program entry breakpoint is hit
wait $GDB_PID
```

#### Python Example (Low-Level)

```python
#!/usr/bin/env python3
"""Debug a DOS program at its entry point (low-level socket example)."""
import socket
import json
import time

def qmp_command(sock, cmd, args=None):
    """Send QMP command and return response."""
    msg = {"execute": cmd}
    if args:
        msg["arguments"] = args
    sock.send((json.dumps(msg) + "\n").encode())
    return json.loads(sock.recv(4096).decode())

def type_text(sock, text):
    """Type text via QMP send-key."""
    keymap = {'.': 'dot', ' ': 'spc', '\n': 'ret'}
    for char in text:
        key = keymap.get(char, char.lower())
        qmp_command(sock, "send-key", {
            "keys": [{"type": "qcode", "data": key}]
        })
        time.sleep(0.05)

# Connect GDB first (critical!)
gdb = socket.socket()
gdb.connect(("localhost", 2159))
gdb.recv(1024)  # Receive any greeting

# Connect QMP
qmp = socket.socket()
qmp.connect(("localhost", 4444))
qmp.recv(1024)  # Greeting
qmp_command(qmp, "qmp_capabilities")

# Enable break-on-exec
qmp_command(qmp, "debug-break-on-exec", {"enabled": True})

# Type program name and press Enter
type_text(qmp, "MYGAME.EXE\n")

# Wait for S05 from GDB
gdb.settimeout(5.0)
response = gdb.recv(1024)
print(f"GDB received: {response}")  # Should contain $S05#b8

# Now you can read registers, set breakpoints, etc.
gdb.send(b"$g#67")  # Read all registers
print(gdb.recv(4096))
```

---

## Python Automation Library

> **Deprecated.** `tests/integration/dosbox_debug.py` is kept only for
> backward compatibility during this stage -- an external project still
> imports `GDBClient`/`QMPClient` from it by path. It is not the
> recommended way to automate DOSBox-X going forward:
>
> - For automation, use `dbxdebug` (the packaged replacement for this
>   module) once it ships.
> - For in-repo conformance work, use the protocol clients under
>   `tests/integration/protocol/` (`protocol/gdb.py`, `protocol/qmp.py`),
>   which back `test_gdb_conformance.py` and `test_qmp_conformance.py`.
>
> `dosbox_debug.py` itself is scheduled for removal once `dbxdebug` ships.
> It did get one behavior change in this stage: `GDBClient.set_breakpoint()`
> and `.remove_breakpoint()` now reject any address `>= 0x110000` with
> `PackedAddressError`, on the assumption that it is a packed
> `(seg << 16) | off` far pointer left over from before `Z0`/`z0` took
> linear addresses -- real-mode linear addresses, including the HMA, stay
> below that boundary.

The `tests/integration/dosbox_debug.py` module provides high-level Python classes for automating DOSBox-X:

- **`DOSBoxInstance`** - Launch and manage DOSBox-X processes
- **`GDBClient`** - GDB Remote Serial Protocol client with video memory access
- **`QMPClient`** - QMP client for keyboard/mouse input

### Installation

Copy `tests/integration/dosbox_debug.py` to your project, or add the path to `sys.path`.

### DOSBoxInstance

The `DOSBoxInstance` class launches DOSBox-X, connects GDB and QMP clients, and provides convenience methods.

```python
from dosbox_debug import DOSBoxInstance

with DOSBoxInstance(
    executable="./src/dosbox-x",       # Path to DOSBox-X binary
    config="my_test.conf",             # Configuration file
    gdb_port=2159,                     # GDB server port
    qmp_port=4444,                     # QMP server port
) as dbx:
    # Access clients
    dbx.gdb.read_registers()           # GDBClient
    dbx.qmp.send_key(["a"])            # QMPClient

    # Convenience methods
    dbx.halt()                         # Pause CPU (GDB break)
    dbx.continue_()                    # Resume execution
    dbx.type_text("DIR\r")             # Type text (QMP)
    dbx.screen_dump()                  # Read screen (GDB memory read)
    dbx.query_status()                 # Get emulator status (QMP)
```

### Complete Example: Debug a Program with Breakpoints

This example demonstrates:
1. Launching DOSBox-X with a custom executable path
2. Enabling break-on-exec to pause at program entry
3. Setting a new breakpoint at a specific address
4. Continuing execution until the breakpoint is hit

```python
#!/usr/bin/env python3
"""
Complete debugging example: launch DOSBox-X, break at program entry,
set a breakpoint, and continue until it's hit.
"""
import sys
import time
sys.path.insert(0, "tests/integration")

from dosbox_debug import DOSBoxInstance

# Path to your DOSBox-X build
DOSBOX_EXECUTABLE = "./src/dosbox-x"

# Configuration file with remotedebug enabled
# (must have gdbserver=true and qmpserver=true)
CONFIG_FILE = "tests/integration/test.conf"

def main():
    print("=== DOSBox-X Remote Debugging Example ===\n")

    with DOSBoxInstance(
        executable=DOSBOX_EXECUTABLE,
        config=CONFIG_FILE,
    ) as dbx:
        print("1. DOSBox-X started, GDB and QMP connected")

        # Let DOS boot up
        dbx.continue_()
        time.sleep(2.0)

        # Enable break-on-exec: DOSBox-X will pause when DOS loads a program
        print("2. Enabling debug-break-on-exec...")
        result = dbx.qmp.debug_break_on_exec(True)
        print(f"   Result: {result}")

        # Type a command to run (change to your program)
        # Using MEM.EXE as an example since it's built into DOS
        print("3. Typing 'MEM' command...")
        dbx.type_text("MEM\r")

        # Wait for the breakpoint at program entry
        print("4. Waiting for program entry breakpoint...")
        time.sleep(1.0)

        # Halt to ensure we're stopped (entry breakpoint should have fired)
        dbx.halt()
        time.sleep(0.2)

        # Read registers at entry point
        # NOTE: regs.eip is EIP as the CPU holds it -- an offset within CS,
        # not a linear address. Compute the linear PC yourself.
        regs = dbx.gdb.read_registers()
        print(f"5. At program entry point:")
        print(f"   EIP = 0x{regs.eip:08X}  (offset within CS)")
        print(f"   CS  = 0x{regs.cs:04X}")

        linear_pc = regs.cs * 16 + regs.eip
        print(f"   Linear PC: 0x{linear_pc:08X}")

        # Set a breakpoint a few instructions ahead.
        # Breakpoint addresses are linear (this is what set_breakpoint
        # expects, and what its packed-far-pointer guard checks against),
        # so build it from the linear PC, not from EIP alone.
        # (+ 0x10 is arbitrary - adjust for your program)
        breakpoint_addr = linear_pc + 0x10
        print(f"\n6. Setting breakpoint at 0x{breakpoint_addr:08X}...")
        success = dbx.gdb.set_breakpoint(breakpoint_addr)
        print(f"   Breakpoint set: {success}")

        # Continue execution
        print("7. Continuing execution...")
        dbx.continue_()

        # Wait for the breakpoint to be hit
        print("8. Waiting for breakpoint...")
        stop_reply = dbx.gdb.wait_for_stop(timeout=5.0)

        if stop_reply:
            print(f"   Stop reply: {stop_reply}")

            # Read registers at breakpoint
            regs = dbx.gdb.read_registers()
            linear_pc = regs.cs * 16 + regs.eip
            print(f"\n9. Breakpoint hit!")
            print(f"   EIP = 0x{regs.eip:08X}  (offset within CS)")
            print(f"   Linear PC = 0x{linear_pc:08X}")

            # Read some memory at current location.
            # read_memory takes a linear address, so use linear_pc, not
            # regs.eip alone.
            mem = dbx.gdb.read_memory(linear_pc, 8)
            print(f"   Bytes at EIP: {mem.hex()}")
        else:
            print("   Timeout waiting for breakpoint")

        # Clean up: remove breakpoint and continue
        print("\n10. Cleaning up...")
        dbx.gdb.remove_breakpoint(breakpoint_addr)
        dbx.continue_()

        print("\n=== Done ===")

if __name__ == "__main__":
    main()
```

### GDBClient Methods

```python
# Connection
gdb.connect()                          # Connect to server
gdb.close()                            # Disconnect

# Execution control
gdb.halt()                             # Send break (Ctrl+C)
gdb.step()                             # Single step
gdb.continue_()                        # Continue (returns immediately)
gdb.wait_for_stop(timeout=5.0)         # Wait for S05 stop reply

# Registers
gdb.read_registers()                   # Read all registers -> Registers dataclass
gdb.read_register(8)                   # Read single register by index (8=EIP, offset within CS)

# Memory
gdb.read_memory(0xB8000, 100)          # Read bytes from linear address
gdb.write_memory(0x1000, b"\x90\x90")  # Write bytes

# Breakpoints
gdb.set_breakpoint(0x1234)             # Set software breakpoint
gdb.remove_breakpoint(0x1234)          # Remove breakpoint

# Video memory (convenience methods)
gdb.screen_dump()                      # Get screen as list of strings
gdb.screen_line(24)                    # Get single line (default: line 24)
gdb.screen_raw()                       # Raw VGA memory (char+attr pairs)
gdb.read_video_mode()                  # Current video mode from BIOS
gdb.read_timer_ticks()                 # BIOS timer tick count
```

### QMPClient Methods

```python
# Connection
qmp.connect()                          # Connect and negotiate capabilities
qmp.close()                            # Disconnect

# Keyboard input
qmp.send_key(["ctrl", "alt", "del"])   # Press key combination
qmp.key_down("shift")                  # Press key (no release)
qmp.key_up("shift")                    # Release key
qmp.key_press("a", hold_time=0.1)      # Press and release
qmp.type_text("DIR\r", delay=0.15)     # Type string (\r = Enter)

# Mouse input (via input_send_event)
qmp.input_send_event([
    {"type": "rel", "data": {"axis": "x", "value": 50}},
    {"type": "btn", "data": {"button": "left", "down": True}},
])

# Emulator control
qmp.stop()                             # Pause emulator
qmp.cont()                             # Resume emulator
qmp.query_status()                     # Get running/paused state
qmp.debug_break_on_exec(True)          # Enable break at program entry
```

---

## Files

- `src/debug/gdbserver.cpp`, `include/gdbserver.h` - GDB server
- `src/debug/qmp.cpp`, `include/qmp.h` - QMP server
- `src/debug/debug.cpp` - Integration
- `tests/integration/` - Integration tests

---

## Integration Tests

Integration tests use pytest with the `dosbox_debug.py` module. Tests automatically launch and manage DOSBox-X instances.

### Requirements

- Python 3.11+
- [uv](https://github.com/astral-sh/uv) (recommended) or pip
- DOSBox-X built with `--enable-remotedebug`

### Running Tests

```bash
# Run all DEBUGBOX integration tests
uv run pytest tests/integration/test_debugbox.py -v

# Run specific test class
uv run pytest tests/integration/test_debugbox.py::TestDebugboxEntryPointFull -v

# Run with test drive mounted (for tests that need test assets)
DEBUGBOX_TEST_DRIVE=T uv run pytest tests/integration/test_debugbox.py -v

# Filter by test name
uv run pytest tests/integration/test_debugbox.py -k "breakpoint" -v

# Stop on first failure
uv run pytest tests/integration/test_debugbox.py -x
```

### Test Coverage

| Suite | Coverage |
|-------|----------|
| `test_debugbox.py` | DEBUGBOX command, GDB pause states, step/continue, breakpoints, debug-break-on-exec, query-status, screen capture |

### Writing Tests

Tests use the `DOSBoxInstance` fixture for automatic setup/teardown:

```python
import pytest
from dosbox_debug import DOSBoxInstance

@pytest.fixture(scope="module")
def dosbox():
    """Start DOSBox-X for the test module."""
    with DOSBoxInstance() as dbx:
        dbx.continue_()
        time.sleep(2.0)  # Wait for boot
        yield dbx

def test_can_read_registers(dosbox):
    """Test that we can read CPU registers."""
    dosbox.halt()
    regs = dosbox.gdb.read_registers()
    assert regs is not None
    assert hasattr(regs, 'eip')
```

See `tests/integration/README.md` for detailed documentation.

---

## TODO

- [ ] Windows support (currently POSIX sockets only)
- [ ] Hardware breakpoints/watchpoints for GDB server
