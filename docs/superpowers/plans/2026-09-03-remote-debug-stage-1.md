# Remote Debug Harness Stage 1 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make the DOSBox-X GDB stub conform to the GDB remote serial protocol on breakpoint addressing, fix two threading bugs it exposes, and replace the integration tests with a dependency-free conformance suite that pins all three.

**Architecture:** `Z0`/`z0` stop splitting their argument as a far pointer and take a linear address like `m`/`M` already do. A vendor `qSupported` feature lets clients detect the change. Pending QMP work is drained while the CPU is halted for GDB, and `memdump` reads guest memory directly only when the CPU is provably stopped. A new `tests/integration/protocol/` package speaks both wire protocols using nothing but the standard library, so the suite can assert what the servers actually do without taking a non-GPL-compatible dependency.

**Tech Stack:** C++11 (`src/debug/`, `src/dosbox.cpp`), Python 3.11+ standard library, pytest, `uv` with PEP 723 inline script metadata.

**Spec:** `docs/superpowers/specs/2026-09-03-dosbox-debug-harness-design.md`

## Global Constraints

- **The Python in `tests/integration/` may import the standard library and pytest, and nothing else.** `dbxdebug` is Polyform Shield 1.0.0, which is not GPL-compatible; this repo is GPLv2 and cannot take it as a test dependency. This applies to PEP 723 blocks too.
- C++ must compile as C++11.
- Use `uint32_t`, `uint16_t`, `int16_t` etc. for specific-width integers; never assume `int`/`long` sizes.
- Build with `./build-debug --enable-remotedebug`. The binary lands at `src/dosbox-x`.
- Config option names are `gdbserver port` and `qmpserver port`, with a space. `gdbport`/`qmpport` are not options.
- Commit after every task using conventional commit format. Never add `Co-authored-by` or any AI/agent attribution to commits.
- **Do not modify anything under `~/projects/lokkju/powerbasic-decompile`.** That working tree is edited by other agents concurrently.
- `dosbox_debug.py` is **not** deleted in this stage. It is deleted in Stage 2, once `dbxdebug` ships a replacement.

## Notes for the implementer

**Rebuilds are slow.** Tasks 4-7 each change C++ and need `./build-debug --enable-remotedebug` before their tests can pass. Budget for it; do not skip the build and assume.

**Two register-layout facts you will need.** The `g`/`G` packets use 16 registers, 4 bytes each, little-endian hex, in the order EAX, ECX, EDX, EBX, ESP, EBP, ESI, EDI, EIP, EFLAGS, CS, SS, DS, ES, FS, GS (`gdbserver.cpp:379`). Register 8 is asymmetric: reading returns `SegPhys(cs) + reg_eip`, a **linear** PC, while writing sets `reg_eip`, an **offset** (`debug.cpp:6133` versus `:6155`). A blind `g` then `G` round-trip therefore corrupts EIP. Do not do one. This asymmetry is out of scope for Stage 1 — file it as a Beads issue in Task 12 rather than fixing it here.

**Why `AddBreakpoint(0, linear, false)` is exact.** `GetAddress(seg, off)` returns `((uint64_t)seg << 4) + offset` in real mode (`debug.cpp:450`), so segment zero makes the stored `location` equal to `linear`. `CheckBreakpoint` compares that against `GetAddress(SegValue(cs), reg_eip)`, which is the true physical PC. Any `seg:off` pair naming the byte fires.

---

### Task 1: Raw GDB remote serial protocol client

A minimal, dependency-free RSP client. It frames packets and nothing else — no register dataclasses, no convenience wrappers. Ergonomics belong in `dbxdebug`.

**Files:**
- Create: `tests/integration/protocol/__init__.py`
- Create: `tests/integration/protocol/gdb.py`
- Test: `tests/integration/test_protocol_gdb_unit.py`

**Interfaces:**
- Consumes: nothing.
- Produces: `RawGDB(host: str = "127.0.0.1", port: int = 2159, timeout: float = 5.0)` as a context manager, with `connect() -> None`, `close() -> None`, `send(data: str) -> str`, `checksum(data: str) -> int` (a `@staticmethod`), `start_no_ack() -> bool`, `read_registers() -> list[int]` (16 ints), `write_register(index: int, value: int) -> bool`, `read_memory(addr: int, size: int) -> bytes`, `write_memory(addr: int, data: bytes) -> bool`, `set_breakpoint(addr: int) -> bool`, `remove_breakpoint(addr: int) -> bool`, `cont() -> None`, `step() -> None`, `send_no_reply(data: str) -> None`, `wait_for_stop(timeout: float = 10.0) -> str`, `halt() -> str`, `halt_reason() -> str`, `supported() -> str`, `detach() -> None`. `GDBProtocolError` is raised on malformed replies.

- [ ] **Step 1: Write the failing unit test**

These tests exercise framing only and need no emulator.

```python
#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.11"
# dependencies = [
#     "pytest>=8.0",
# ]
# ///
"""Offline unit tests for the raw GDB RSP client framing.

These never touch an emulator. Conformance tests that do live in
test_gdb_conformance.py.
"""

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent))

from protocol.gdb import RawGDB, GDBProtocolError


class FakeSocket:
    """Replays a scripted byte stream and records what was written."""

    def __init__(self, to_read: bytes):
        self._to_read = to_read
        self._pos = 0
        self.written = b""

    def sendall(self, data: bytes) -> None:
        self.written += data

    def recv(self, n: int) -> bytes:
        chunk = self._to_read[self._pos:self._pos + n]
        self._pos += len(chunk)
        return chunk

    def settimeout(self, _t) -> None:
        pass

    def close(self) -> None:
        pass


def test_checksum_is_sum_of_bytes_mod_256():
    assert RawGDB.checksum("g") == 0x67
    assert RawGDB.checksum("qSupported:") == sum(b"qSupported:") % 256


def test_send_frames_the_packet_and_strips_the_reply():
    # Reply: an ack, then $OK#9a
    sock = FakeSocket(b"+$OK#9a")
    client = RawGDB()
    client._sock = sock
    assert client.send("g") == "OK"
    assert sock.written == b"$g#67"


def test_send_raises_on_a_bad_checksum():
    sock = FakeSocket(b"+$OK#00")
    client = RawGDB()
    client._sock = sock
    with pytest.raises(GDBProtocolError, match="checksum"):
        client.send("g")


def test_read_registers_decodes_sixteen_little_endian_words():
    # Sixteen registers, each 0x00000001 little-endian => "01000000" * 16
    payload = "01000000" * 16
    sock = FakeSocket(b"+$" + payload.encode() + b"#"
                      + f"{RawGDB.checksum(payload):02x}".encode())
    client = RawGDB()
    client._sock = sock
    assert client.read_registers() == [1] * 16


def test_read_memory_decodes_hex_to_bytes():
    sock = FakeSocket(b"+$ebfe#" + f"{RawGDB.checksum('ebfe'):02x}".encode())
    client = RawGDB()
    client._sock = sock
    assert client.read_memory(0x20000, 2) == b"\xeb\xfe"
    assert sock.written == b"$m20000,2#" + f"{RawGDB.checksum('m20000,2'):02x}".encode()


def test_set_breakpoint_sends_a_linear_address():
    sock = FakeSocket(b"+$OK#9a")
    client = RawGDB()
    client._sock = sock
    assert client.set_breakpoint(0x20000) is True
    assert sock.written == b"$Z0,20000,1#" + f"{RawGDB.checksum('Z0,20000,1'):02x}".encode()


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-v"]))
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `uv run tests/integration/test_protocol_gdb_unit.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'protocol'`

- [ ] **Step 3: Create the package marker**

```bash
touch tests/integration/protocol/__init__.py
```

- [ ] **Step 4: Write the client**

Create `tests/integration/protocol/gdb.py`:

```python
"""Raw GDB remote serial protocol client. Standard library only.

This exists to ASSERT WHAT THE SERVER DOES, not to be pleasant to use. It
frames packets and decodes replies; it deliberately offers no register
dataclasses, address conveniences, or session management. Those belong in
dbxdebug, which this repository cannot depend on (Polyform Shield versus
GPLv2 -- see docs/superpowers/specs/2026-09-03-dosbox-debug-harness-design.md).

This client never sends `+` acks. The stub sends them but does not wait for
ours (`gdbserver.cpp:254`), and the previous client in this directory ran
without acking for its whole life.

ADDRESSES ARE LINEAR EVERYWHERE. `m`, `M`, `Z0` and `z0` all take a linear
address. If a breakpoint set above 0x10000 answers OK and never fires, the
emulator predates that fix; check for `dosbox-x-linear-bp+` in qSupported.
"""

import socket
import time


class GDBProtocolError(Exception):
    """The stub sent something that is not a well-formed RSP reply."""


class RawGDB:
    def __init__(self, host: str = "127.0.0.1", port: int = 2159,
                 timeout: float = 5.0):
        self.host = host
        self.port = port
        self.timeout = timeout
        self._sock = None
        self._no_ack = False

    def __enter__(self):
        self.connect()
        return self

    def __exit__(self, exc_type, exc, tb):
        self.close()
        return False

    # -- framing -----------------------------------------------------------

    @staticmethod
    def checksum(data: str) -> int:
        return sum(data.encode()) % 256

    def connect(self) -> None:
        self._sock = socket.create_connection((self.host, self.port),
                                              timeout=self.timeout)
        self._sock.settimeout(self.timeout)

    def close(self) -> None:
        if self._sock is not None:
            try:
                self._sock.close()
            finally:
                self._sock = None

    def _read_exact(self, n: int) -> bytes:
        out = b""
        while len(out) < n:
            chunk = self._sock.recv(n - len(out))
            if not chunk:
                raise GDBProtocolError("connection closed mid-packet")
            out += chunk
        return out

    def _read_packet(self) -> str:
        # Skip acks and any leading noise until '$'.
        while True:
            byte = self._read_exact(1)
            if byte == b"$":
                break
            if byte not in (b"+", b"-"):
                raise GDBProtocolError(f"unexpected byte before packet: {byte!r}")
        body = b""
        while True:
            byte = self._read_exact(1)
            if byte == b"#":
                break
            body += byte
        got = self._read_exact(2).decode()
        want = f"{self.checksum(body.decode()):02x}"
        if got.lower() != want:
            raise GDBProtocolError(
                f"bad checksum on {body!r}: stub said {got}, computed {want}")
        return body.decode()

    def send(self, data: str) -> str:
        """Send one packet and return the reply body."""
        self._sock.sendall(f"${data}#{self.checksum(data):02x}".encode())
        return self._read_packet()

    def send_no_reply(self, data: str) -> None:
        """Send a packet whose reply arrives later (`c`, `s`)."""
        self._sock.sendall(f"${data}#{self.checksum(data):02x}".encode())

    # -- commands ----------------------------------------------------------

    def supported(self) -> str:
        return self.send("qSupported:multiprocess+")

    def start_no_ack(self) -> bool:
        ok = self.send("QStartNoAckMode") == "OK"
        self._no_ack = ok
        return ok

    def halt_reason(self) -> str:
        return self.send("?")

    def read_registers(self) -> list:
        raw = self.send("g")
        if len(raw) < 16 * 8:
            raise GDBProtocolError(f"short register block: {raw!r}")
        out = []
        for i in range(16):
            word = raw[i * 8:(i + 1) * 8]
            out.append(int.from_bytes(bytes.fromhex(word), "little"))
        return out

    def write_register(self, index: int, value: int) -> bool:
        """Write ONE register via `P`. Note the g/G asymmetry on register 8:
        reading gives SegPhys(cs)+eip, writing sets eip. Never round-trip."""
        hex_val = value.to_bytes(4, "little").hex()
        return self.send(f"P{index:x}={hex_val}") == "OK"

    def read_memory(self, addr: int, size: int) -> bytes:
        reply = self.send(f"m{addr:x},{size:x}")
        if reply.startswith("E"):
            raise GDBProtocolError(f"m returned {reply}")
        return bytes.fromhex(reply)

    def write_memory(self, addr: int, data: bytes) -> bool:
        return self.send(f"M{addr:x},{len(data):x}:{data.hex()}") == "OK"

    def set_breakpoint(self, addr: int) -> bool:
        return self.send(f"Z0,{addr:x},1") == "OK"

    def remove_breakpoint(self, addr: int) -> bool:
        return self.send(f"z0,{addr:x},1") == "OK"

    def cont(self) -> None:
        self.send_no_reply("c")

    def step(self) -> None:
        self.send_no_reply("s")

    def halt(self) -> str:
        """Interrupt a running guest and return its stop reply.

        A bare 0x03 byte, not a packet -- `gdbserver.cpp:183` checks for it
        at the head of the receive buffer. Returns "" if the guest was
        already stopped and sends nothing back.
        """
        self._sock.sendall(b"\x03")
        try:
            return self.wait_for_stop(timeout=5.0)
        except GDBProtocolError:
            return ""

    def wait_for_stop(self, timeout: float = 10.0) -> str:
        """Block for the stop reply that follows `c`, `s` or an interrupt."""
        deadline = time.time() + timeout
        old = self._sock.gettimeout()
        try:
            while time.time() < deadline:
                self._sock.settimeout(max(0.1, deadline - time.time()))
                try:
                    return self._read_packet()
                except socket.timeout:
                    continue
            return ""
        finally:
            self._sock.settimeout(old)

    def detach(self) -> None:
        self.send("D")
```

- [ ] **Step 5: Run the test to verify it passes**

Run: `uv run tests/integration/test_protocol_gdb_unit.py -v`
Expected: PASS, 6 tests.

Note `P` is used by `write_register` but is not in the stub's dispatch (`gdbserver.cpp:305-338` handles `p` but not `P`). Task 4's conformance test asserts that, and Task 4 adds `P` if it is missing. Do not add it here.

- [ ] **Step 6: Commit**

```bash
git add tests/integration/protocol/__init__.py tests/integration/protocol/gdb.py tests/integration/test_protocol_gdb_unit.py
git commit -m "test: add dependency-free raw GDB RSP client"
```

---

### Task 2: Raw QMP client

**Files:**
- Create: `tests/integration/protocol/qmp.py`
- Test: `tests/integration/test_protocol_qmp_unit.py`

**Interfaces:**
- Consumes: nothing.
- Produces: `RawQMP(host: str = "127.0.0.1", port: int = 4444, timeout: float = 5.0)` as a context manager, with `connect() -> dict` (performs the greeting and `qmp_capabilities` handshake, returns the greeting), `close() -> None`, `execute(command: str, arguments: dict | None = None) -> dict` (returns the parsed `return` object, raises `QMPProtocolError` on an `error` reply), and `execute_raw(command: str, arguments: dict | None = None) -> dict` (returns the whole reply object without raising, for asserting error shapes).

- [ ] **Step 1: Write the failing unit test**

```python
#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.11"
# dependencies = [
#     "pytest>=8.0",
# ]
# ///
"""Offline unit tests for the raw QMP client framing."""

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent))

from protocol.qmp import RawQMP, QMPProtocolError


class FakeSocket:
    def __init__(self, to_read: bytes):
        self._to_read = to_read
        self._pos = 0
        self.written = b""

    def sendall(self, data: bytes) -> None:
        self.written += data

    def recv(self, n: int) -> bytes:
        chunk = self._to_read[self._pos:self._pos + n]
        self._pos += len(chunk)
        return chunk

    def settimeout(self, _t) -> None:
        pass

    def gettimeout(self):
        return None

    def close(self) -> None:
        pass


def test_execute_sends_a_command_and_returns_the_return_object():
    sock = FakeSocket(b'{"return": {"size": 4}}\r\n')
    client = RawQMP()
    client._sock = sock
    assert client.execute("memdump", {"address": 0, "size": 4}) == {"size": 4}
    sent = json.loads(sock.written.decode())
    assert sent == {"execute": "memdump",
                    "arguments": {"address": 0, "size": 4}}


def test_execute_raises_on_an_error_reply():
    sock = FakeSocket(b'{"error": {"class": "GenericError", "desc": "nope"}}\r\n')
    client = RawQMP()
    client._sock = sock
    with pytest.raises(QMPProtocolError, match="nope"):
        client.execute("memdump")


def test_execute_raw_returns_the_error_without_raising():
    sock = FakeSocket(b'{"error": {"class": "GenericError", "desc": "nope"}}\r\n')
    client = RawQMP()
    client._sock = sock
    reply = client.execute_raw("memdump")
    assert reply["error"]["class"] == "GenericError"


def test_omits_the_arguments_key_when_there_are_none():
    sock = FakeSocket(b'{"return": {}}\r\n')
    client = RawQMP()
    client._sock = sock
    client.execute("query-status")
    assert json.loads(sock.written.decode()) == {"execute": "query-status"}


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-v"]))
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `uv run tests/integration/test_protocol_qmp_unit.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'protocol.qmp'`

- [ ] **Step 3: Write the client**

Create `tests/integration/protocol/qmp.py`:

```python
"""Raw QMP client. Standard library only.

Asserts what the QMP server does. Ergonomics belong in dbxdebug, which this
repository cannot depend on -- see the note at the top of gdb.py.
"""

import json
import socket
import time


class QMPProtocolError(Exception):
    """The server returned an error reply, or something unparseable."""


class RawQMP:
    def __init__(self, host: str = "127.0.0.1", port: int = 4444,
                 timeout: float = 5.0):
        self.host = host
        self.port = port
        self.timeout = timeout
        self._sock = None
        self._buf = b""

    def __enter__(self):
        self.connect()
        return self

    def __exit__(self, exc_type, exc, tb):
        self.close()
        return False

    def connect(self) -> dict:
        self._sock = socket.create_connection((self.host, self.port),
                                              timeout=self.timeout)
        self._sock.settimeout(self.timeout)
        greeting = self._recv_json()
        self.execute("qmp_capabilities")
        return greeting

    def close(self) -> None:
        if self._sock is not None:
            try:
                self._sock.close()
            finally:
                self._sock = None

    def _recv_json(self) -> dict:
        deadline = time.time() + self.timeout
        while time.time() < deadline:
            if b"\n" in self._buf:
                line, self._buf = self._buf.split(b"\n", 1)
                line = line.strip()
                if not line:
                    continue
                try:
                    return json.loads(line.decode())
                except json.JSONDecodeError as exc:
                    raise QMPProtocolError(
                        f"not JSON: {line!r}") from exc
            chunk = self._sock.recv(65536)
            if not chunk:
                raise QMPProtocolError("connection closed")
            self._buf += chunk
        raise QMPProtocolError("timed out waiting for a reply")

    def execute_raw(self, command: str, arguments: dict = None) -> dict:
        payload = {"execute": command}
        if arguments is not None:
            payload["arguments"] = arguments
        self._sock.sendall(json.dumps(payload).encode())
        while True:
            reply = self._recv_json()
            # Skip asynchronous events; only replies carry return/error.
            if "return" in reply or "error" in reply:
                return reply

    def execute(self, command: str, arguments: dict = None) -> dict:
        reply = self.execute_raw(command, arguments)
        if "error" in reply:
            err = reply["error"]
            raise QMPProtocolError(
                f"{err.get('class', '?')}: {err.get('desc', '?')}")
        return reply["return"]
```

- [ ] **Step 4: Run the test to verify it passes**

Run: `uv run tests/integration/test_protocol_qmp_unit.py -v`
Expected: PASS, 4 tests.

- [ ] **Step 5: Commit**

```bash
git add tests/integration/protocol/qmp.py tests/integration/test_protocol_qmp_unit.py
git commit -m "test: add dependency-free raw QMP client"
```

---

### Task 3: Emulator launcher and pytest fixtures

A single-instance spawner for CI. Dynamic ports so a developer's own emulator is not disturbed, `killpg` teardown, and **no `pkill`** — the old `DOSBoxInstance._kill_existing()` ran `pkill -9 -f dosbox-x`, which kills every other emulator on the machine.

**Files:**
- Create: `tests/integration/launcher.py`
- Create: `tests/integration/conftest.py`
- Test: `tests/integration/test_launcher.py`

**Interfaces:**
- Consumes: `protocol.gdb.RawGDB`, `protocol.qmp.RawQMP` from Tasks 1 and 2.
- Produces: `Emulator(executable: str | None = None, extra_conf: str = "", mounts: dict | None = None, boot_settle: float = 2.5)` as a context manager, with attributes `gdb_port: int`, `qmp_port: int`, `pid: int`, `workdir: Path`, `conf_path: Path`, and methods `start() -> "Emulator"`, `stop() -> None`, `gdb() -> RawGDB`, `qmp() -> RawQMP`. Module function `free_port() -> int`. Pytest fixtures `emulator` (function-scoped `Emulator`), `gdb` (a connected `RawGDB`), `qmp` (a connected `RawQMP`).

- [ ] **Step 1: Write the failing test**

```python
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
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `uv run tests/integration/test_launcher.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'launcher'`

- [ ] **Step 3: Write the launcher**

Create `tests/integration/launcher.py`:

```python
"""Start one DOSBox-X for a test, and guarantee it dies.

Deliberately NOT a session manager. It allocates ports, writes a conf,
spawns one emulator in its own process group, and tears the group down. Any
richer lifecycle -- registries, orphan reaping, concurrent fleets -- belongs
in dbxdebug, which this repository cannot depend on.

It does NOT run `pkill -f dosbox-x`. Its predecessor did, which killed every
other emulator on the machine including other developers' and other agents'.
"""

import os
import shutil
import signal
import socket
import subprocess
import tempfile
import time
from pathlib import Path

from protocol.gdb import RawGDB
from protocol.qmp import RawQMP

REPO_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_EXECUTABLE = REPO_ROOT / "src" / "dosbox-x"

CONF_TEMPLATE = """\
[sdl]
autolock = false
output = surface

[dosbox]
quit warning = false
gdbserver = true
gdbserver port = {gdb_port}
qmpserver = true
qmpserver port = {qmp_port}

[cpu]
core = normal
cycles = fixed 20000

[autoexec]
{autoexec}
"""


def free_port() -> int:
    """Ask the kernel for an unused port, then release it.

    There is a race between releasing and the emulator binding. start()
    retries with fresh ports rather than pretending there is not.
    """
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def port_is_listening(port: int, host: str = "127.0.0.1",
                      timeout: float = 0.3) -> bool:
    with socket.socket() as s:
        s.settimeout(timeout)
        return s.connect_ex((host, port)) == 0


class EmulatorLaunchError(RuntimeError):
    pass


class Emulator:
    START_TIMEOUT = 30.0
    PORT_RETRIES = 3

    def __init__(self, executable=None, extra_conf: str = "",
                 mounts: dict = None, boot_settle: float = 2.5):
        self.executable = Path(executable or DEFAULT_EXECUTABLE)
        self.extra_conf = extra_conf
        self.mounts = dict(mounts or {})
        self.boot_settle = boot_settle
        self.gdb_port = None
        self.qmp_port = None
        self.pid = None
        self.workdir = None
        self.conf_path = None
        self._proc = None
        self._gdb = None
        self._qmp = None
        self._stopped = False

    def __enter__(self):
        return self.start()

    def __exit__(self, exc_type, exc, tb):
        self.stop()
        return False

    # -- setup -------------------------------------------------------------

    def _allocate_ports(self) -> None:
        self.gdb_port = free_port()
        self.qmp_port = free_port()
        while self.qmp_port == self.gdb_port:
            self.qmp_port = free_port()

    def _write_conf(self) -> None:
        lines = [f"mount {drive} {path}" for drive, path in self.mounts.items()]
        conf = CONF_TEMPLATE.format(gdb_port=self.gdb_port,
                                    qmp_port=self.qmp_port,
                                    autoexec="\n".join(lines))
        if self.extra_conf:
            conf += "\n" + self.extra_conf + "\n"
        self.conf_path = self.workdir / "test.conf"
        self.conf_path.write_text(conf)

    # -- lifecycle ---------------------------------------------------------

    def start(self) -> "Emulator":
        if not self.executable.exists():
            raise EmulatorLaunchError(
                f"no emulator at {self.executable}. "
                f"Build it with ./build-debug --enable-remotedebug")
        last = None
        for _ in range(self.PORT_RETRIES):
            self.workdir = Path(tempfile.mkdtemp(prefix="dbx-conformance-"))
            self._allocate_ports()
            self._write_conf()
            try:
                self._spawn()
                self._wait_for_ports()
                time.sleep(self.boot_settle)
                self._stopped = False
                return self
            except EmulatorLaunchError as exc:
                last = exc
                self.stop()
                self._stopped = False
        raise EmulatorLaunchError(f"could not start after retries: {last}")

    def _spawn(self) -> None:
        env = dict(os.environ)
        env.setdefault("SDL_VIDEODRIVER", "dummy")
        self._proc = subprocess.Popen(
            [str(self.executable), "-conf", str(self.conf_path)],
            cwd=str(self.workdir),
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
            env=env,
        )
        self.pid = self._proc.pid

    def _wait_for_ports(self) -> None:
        deadline = time.time() + self.START_TIMEOUT
        while time.time() < deadline:
            if self._proc.poll() is not None:
                raise EmulatorLaunchError(
                    f"emulator exited with {self._proc.returncode}")
            if (port_is_listening(self.gdb_port)
                    and port_is_listening(self.qmp_port)):
                return
            time.sleep(0.1)
        raise EmulatorLaunchError(
            f"ports {self.gdb_port}/{self.qmp_port} never accepted")

    def stop(self) -> None:
        if self._stopped:
            return
        self._stopped = True
        for client in (self._gdb, self._qmp):
            if client is not None:
                try:
                    client.close()
                except Exception:
                    pass
        self._gdb = self._qmp = None
        if self._proc is not None:
            try:
                pgid = os.getpgid(self._proc.pid)
                os.killpg(pgid, signal.SIGTERM)
                deadline = time.time() + 5.0
                while time.time() < deadline and self._proc.poll() is None:
                    time.sleep(0.05)
                if self._proc.poll() is None:
                    os.killpg(pgid, signal.SIGKILL)
                self._proc.wait(timeout=5.0)
            except (ProcessLookupError, PermissionError):
                pass
            self._proc = None
        if self.workdir is not None and self.workdir.exists():
            # shutil, never a shell `rm -rf`: sandboxes refuse the latter.
            shutil.rmtree(self.workdir, ignore_errors=True)

    # -- clients -----------------------------------------------------------

    def gdb(self) -> RawGDB:
        if self._gdb is None:
            self._gdb = RawGDB(port=self.gdb_port, timeout=10.0)
            self._gdb.connect()
        return self._gdb

    def qmp(self) -> RawQMP:
        if self._qmp is None:
            self._qmp = RawQMP(port=self.qmp_port, timeout=10.0)
            self._qmp.connect()
        return self._qmp
```

- [ ] **Step 4: Write the fixtures**

Create `tests/integration/conftest.py`:

```python
"""Shared fixtures. Every conformance test gets its own emulator.

Function scope is deliberate: these tests halt the CPU, write guest memory
and move the program counter, so sharing one emulator between them would
make failures depend on execution order.
"""

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent))

from launcher import Emulator


@pytest.fixture
def emulator():
    with Emulator() as emu:
        yield emu


@pytest.fixture
def gdb(emulator):
    return emulator.gdb()


@pytest.fixture
def qmp(emulator):
    return emulator.qmp()
```

- [ ] **Step 5: Build the emulator, then run the test**

Run: `./build-debug --enable-remotedebug`
Then: `uv run tests/integration/test_launcher.py -v`
Expected: PASS, 5 tests.

- [ ] **Step 6: Commit**

```bash
git add tests/integration/launcher.py tests/integration/conftest.py tests/integration/test_launcher.py
git commit -F - <<'EOF'
test: add isolated emulator launcher for conformance tests

Allocates dynamic ports so a test never binds 2159/4444 and never talks
to a developer's own emulator, writes its conf with the spaced option
names the server actually reads, and tears the process group down.

Unlike DOSBoxInstance it does not run pkill -f dosbox-x, which killed
every other emulator on the machine.
EOF
```

---

### Task 4: Linear breakpoint addresses

The core fix. `gdbserver.cpp:448` hands the `Z0` argument to `DEBUG_SetBreakpoint`, which splits it as a far pointer, while `m` treats the same form as linear. Breakpoints above `0x10000` answer `OK` and never fire.

**Files:**
- Modify: `src/debug/debug.cpp:6253-6267`
- Test: `tests/integration/test_gdb_conformance.py`

**Interfaces:**
- Consumes: `Emulator`, `RawGDB`, the `emulator`/`gdb` fixtures.
- Produces: nothing later tasks import. `DEBUG_SetBreakpoint(uint32_t linear)` and `DEBUG_RemoveBreakpoint(uint32_t linear)` keep their signatures; only their interpretation changes.

- [ ] **Step 1: Write the failing test**

The test writes a two-byte infinite loop (`EB FE`, `jmp $`) at linear `0x30000`, points the CPU at it, sets a breakpoint on that linear address and continues. Under the old code `Z0,30000,1` decodes as segment `0x0003`, offset `0x0000`, so the breakpoint lands at physical `0x30` and never fires. Corrupting memory at `0x30000` and hijacking the PC is fine — the emulator is destroyed when the fixture exits.

```python
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
"""

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent))

from protocol.gdb import GDBProtocolError  # noqa: F401  (used by other tests)

# A linear address comfortably above 0x10000, which is where the packed and
# linear interpretations of a Z0 argument stop coinciding.
LOOP_ADDR = 0x30000
JMP_SELF = b"\xeb\xfe"


def _park_cpu_in_a_loop(gdb, addr: int = LOOP_ADDR) -> None:
    """Halt, plant `jmp $` at `addr`, and point CS:IP at it.

    Register 8 is asymmetric -- reading gives SegPhys(cs)+eip, writing sets
    eip -- so CS and EIP are written separately and never round-tripped.
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
    assert regs[8] == LOOP_ADDR, (
        f"stopped at 0x{regs[8]:X}, expected 0x{LOOP_ADDR:X}")


def test_breakpoint_and_memory_agree_on_what_an_address_is(gdb):
    """The byte `m` reads at L is the byte execution stops on at L."""
    _park_cpu_in_a_loop(gdb)
    assert gdb.read_memory(LOOP_ADDR, 2) == JMP_SELF
    assert gdb.set_breakpoint(LOOP_ADDR) is True
    gdb.cont()
    assert gdb.wait_for_stop(timeout=15.0).startswith("S05")
    regs = gdb.read_registers()
    assert gdb.read_memory(regs[8], 2) == JMP_SELF


def test_removing_a_breakpoint_lets_execution_continue(gdb):
    _park_cpu_in_a_loop(gdb)
    assert gdb.set_breakpoint(LOOP_ADDR) is True
    gdb.cont()
    assert gdb.wait_for_stop(timeout=15.0).startswith("S05")

    assert gdb.remove_breakpoint(LOOP_ADDR) is True
    gdb.cont()
    assert gdb.wait_for_stop(timeout=3.0) == "", (
        "execution stopped again after the breakpoint was removed")


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-v"]))
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `uv run --with pytest pytest tests/integration/test_gdb_conformance.py -v`
Expected: FAIL — `test_breakpoint_above_64k_fires_at_the_linear_address` reports "never fired (got '')". If `write_register` fails with an empty reply, the stub has no `P` packet; go to Step 3b.

- [ ] **Step 3: Fix the breakpoint address interpretation**

In `src/debug/debug.cpp`, replace lines 6253-6267 (the `FP_SEG`/`FP_OFF` macros and both functions) with:

```c
 /* The GDB remote serial protocol's Z0/z0 address is LINEAR, the same as the
  * m and M packets. Segment zero makes GetAddress(0, off) == off in real
  * mode (see GetAddress above), so the stored breakpoint location is exactly
  * the linear address, and CheckBreakpoint compares it against the true
  * physical PC, GetAddress(SegValue(cs), reg_eip).
  *
  * This used to split the argument as a far pointer with FP_SEG(x) = x >> 16.
  * Any breakpoint above 0x10000 answered OK and never fired; below 0x10000
  * the two interpretations coincide, which is why it looked like it worked. */
 bool DEBUG_SetBreakpoint(uint32_t address) {
     DEBUG_ShowMsg("Adding Breakpoint at linear %x", address);
     return CBreakpoint::AddBreakpoint(0, address, false) != NULL;
 }

 bool DEBUG_RemoveBreakpoint(uint32_t address) {
     DEBUG_ShowMsg("Removing Breakpoint at linear %x", address);
     return CBreakpoint::DeleteBreakpoint(0, address);
 }
```

Note `AddBreakpoint` returns `CBreakpoint*`, so the comparison against `NULL` is required to keep the `bool` return; the old code relied on an implicit pointer-to-bool conversion.

- [ ] **Step 3b: Add the `P` packet if the stub lacks it**

`gdbserver.cpp` handles `p` (read one register) but not `P` (write one). If Step 2 showed `write_register` returning an empty reply, add a handler. In `src/debug/gdbserver.cpp`, in the dispatch chain, immediately after the `} else if (cmd.substr(0, 1) == "p") {` branch, add:

```c
    } else if (cmd.substr(0, 1) == "P") {
        handle_write_register(cmd.substr(1));
```

Declare `void handle_write_register(const std::string& args);` in `include/gdbserver.h` beside `handle_write_registers`, and define it next to `handle_write_registers` in `src/debug/gdbserver.cpp`:

```c
void GDBServer::handle_write_register(const std::string& args) {
    size_t eq = args.find('=');
    if (eq == std::string::npos) {
        send_packet("E01");
        return;
    }
    int reg = (int)std::stoul(args.substr(0, eq), nullptr, 16);
    uint32_t value = (uint32_t)std::stoul(args.substr(eq + 1), nullptr, 16);
    DEBUG_SetRegister(reg, swap32(value));
    send_packet("OK");
}
```

- [ ] **Step 4: Rebuild and run the test**

Run: `./build-debug --enable-remotedebug`
Then: `uv run --with pytest pytest tests/integration/test_gdb_conformance.py -v`
Expected: PASS, 3 tests.

- [ ] **Step 5: Commit**

```bash
git add src/debug/debug.cpp src/debug/gdbserver.cpp include/gdbserver.h tests/integration/test_gdb_conformance.py
git commit -F - <<'EOF'
fix(gdbserver): make Z0 and z0 take a linear address

DEBUG_SetBreakpoint split its argument as a far pointer via
FP_SEG(x) = x >> 16, while DEBUG_ReadMemory treated the same form as
linear. The two disagreed about what an address is, so a breakpoint set
above 0x10000 answered OK and never fired -- real gdb could not drive
this stub. Below 0x10000 the interpretations coincide, which is why it
appeared to work.

Both functions now take the linear address the protocol specifies.
Segment zero makes GetAddress(0, off) == off in real mode, so the stored
location is exactly the linear address and CheckBreakpoint's comparison
against the physical PC is unchanged.

gdbserver.cpp is the only caller of either function and the FP_SEG and
FP_OFF macros were used nowhere else, so both are removed.

Adds conformance tests that plant a jmp-self at linear 0x30000, point
CS:IP at it, and require the breakpoint to fire there.
EOF
```

---

### Task 5: Advertise the fix through `qSupported`

Clients need to tell a fixed stub from an old one. `Z0` answers `OK` either way, so without this the change is silent in both directions.

**Files:**
- Modify: `src/debug/gdbserver.cpp:467`
- Modify: `tests/integration/test_gdb_conformance.py`

**Interfaces:**
- Consumes: the `gdb` fixture.
- Produces: the wire feature string `dosbox-x-linear-bp+`, which dbxdebug checks at connect in Stage 2.

- [ ] **Step 1: Write the failing test**

Append to `tests/integration/test_gdb_conformance.py`:

```python
def test_qsupported_advertises_linear_breakpoints(gdb):
    """Z0 answers OK whichever way it reads the address, so a client cannot
    detect the semantics by probing. It has to be advertised."""
    features = gdb.supported()
    assert "dosbox-x-linear-bp+" in features, (
        f"qSupported did not advertise linear breakpoints: {features!r}")


def test_qsupported_still_advertises_the_stock_features(gdb):
    features = gdb.supported()
    for feature in ("PacketSize=", "swbreak+", "hwbreak+",
                    "vContSupported+", "QStartNoAckMode+"):
        assert feature in features, f"lost {feature} from qSupported"
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `uv run --with pytest pytest tests/integration/test_gdb_conformance.py -v -k qsupported`
Expected: FAIL on `test_qsupported_advertises_linear_breakpoints`; the second test passes.

- [ ] **Step 3: Add the vendor feature**

In `src/debug/gdbserver.cpp`, in `handle_query`, replace the `qSupported` reply:

```c
    if (cmd.substr(0, 10) == "Supported:") {
        /* dosbox-x-linear-bp+ is a vendor feature. It tells a client that
         * Z0/z0 take a LINEAR address, as the protocol specifies, rather
         * than the packed far pointer older builds expected. Real gdb
         * ignores features it does not recognise, so this stays RSP-legal.
         * Clients need it because Z0 answers OK under either reading. */
        send_packet("PacketSize=3fff;swbreak+;hwbreak+;vContSupported+;"
                    "QStartNoAckMode+;dosbox-x-linear-bp+");
```

- [ ] **Step 4: Rebuild and run the test**

Run: `./build-debug --enable-remotedebug`
Then: `uv run --with pytest pytest tests/integration/test_gdb_conformance.py -v`
Expected: PASS, 5 tests.

- [ ] **Step 5: Commit**

```bash
git add src/debug/gdbserver.cpp tests/integration/test_gdb_conformance.py
git commit -F - <<'EOF'
feat(gdbserver): advertise dosbox-x-linear-bp+ in qSupported

Z0 answers OK whether it reads its argument as linear or as a packed far
pointer, so a client cannot detect which semantics a build has by
probing. Without an advertisement the change is silent in both
directions: a new client against an old stub, and an old client against
a new one, both set breakpoints that never fire.

Real gdb ignores qSupported features it does not recognise, so the
vendor feature is protocol-legal.
EOF
```

---

### Task 6: Service pending QMP work while halted for GDB

`DEBUG_CheckGDBStep()` returns true while `gdb_cpu_paused`, and `Normal_Loop` returns at `dosbox.cpp:471` — before the drains at `:474-478`. So while stopped at a breakpoint, `savestate` waits its full 30 second timeout and errors, and queued keystrokes are never delivered.

**Files:**
- Modify: `src/dosbox.cpp:467-478`
- Create: `tests/integration/test_qmp_conformance.py`

**Interfaces:**
- Consumes: `Emulator`, the `emulator` fixture, `RawGDB`, `RawQMP`.
- Produces: nothing later tasks import.

- [ ] **Step 1: Write the failing test**

```python
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
    assert status.get("debug-paused") is True or status.get("debug_paused") is True, (
        f"query-status did not report the debug pause: {status}")


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-v"]))
```

Read `handle_query_status` at `src/debug/qmp.cpp:1036` before running, and change the two key names in `test_query_status_reports_the_debug_pause` to whatever it actually emits — assert the real field, do not leave the `or`.

- [ ] **Step 2: Run the test to verify it fails**

Run: `uv run --with pytest pytest tests/integration/test_qmp_conformance.py -v`
Expected: FAIL — `test_savestate_completes_while_halted_for_gdb` takes about 30 seconds and errors.

- [ ] **Step 3: Drain on the halted path**

In `src/dosbox.cpp`, replace the `DEBUG_CheckGDBStep()` block:

```c
            // Check for GDB step/continue requests from the GDB server thread
            if (DEBUG_CheckGDBStep()) {
                /* Halted for GDB, or a step just completed. The drains below
                 * are unreachable on this path, so pending QMP work has to be
                 * serviced here or it never runs while stopped at a
                 * breakpoint: savestate waits out its timeout and queued
                 * keystrokes are dropped. */
                SAVESTATE_CheckPendingRequest();
                EMULATOR_CheckPendingControl();
                QMP_ProcessPendingInputEvents();
                // Step was executed, return to allow loop to be called again
                return 0;
            }
```

Leave lines 474-478 exactly where they are. Hoisting them above `DEBUG_CheckGDBStep()` would reorder every iteration of the emulation hot loop, which is a behavior change in the running path bought to fix the halted one.

- [ ] **Step 4: Rebuild and run the test**

Run: `./build-debug --enable-remotedebug`
Then: `uv run --with pytest pytest tests/integration/test_qmp_conformance.py -v`
Expected: PASS, 2 tests. `savestate` should now return in well under a second.

- [ ] **Step 5: Commit**

```bash
git add src/dosbox.cpp tests/integration/test_qmp_conformance.py
git commit -F - <<'EOF'
fix(qmp): drain pending requests while halted for GDB

DEBUG_CheckGDBStep() returns true whenever the CPU is paused for GDB, and
Normal_Loop returned immediately on that path -- before
SAVESTATE_CheckPendingRequest, EMULATOR_CheckPendingControl and
QMP_ProcessPendingInputEvents. None of them ran while stopped at a
breakpoint, so savestate waited out its full 30 second timeout and
returned an error, and keystrokes queued at a breakpoint were dropped.

The drains are duplicated onto the halted path rather than hoisted above
the check, so the running path keeps its existing ordering.
EOF
```

---

### Task 7: Close the `memdump` race

`memdump` is the only QMP handler that touches guest state from the socket thread; every other one defers to the emulation thread. When the CPU is provably stopped nothing is mutating memory, so a direct read is safe.

**Files:**
- Modify: `src/debug/qmp.cpp` (`handle_memdump`, around line 705)
- Modify: `tests/integration/test_qmp_conformance.py`

**Interfaces:**
- Consumes: `DEBUG_IsCpuPausedForDebug()`, declared at `include/debug.h:53`.
- Produces: nothing later tasks import.

- [ ] **Step 1: Write the failing test**

Append to `tests/integration/test_qmp_conformance.py`:

```python
import base64


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
```

- [ ] **Step 2: Run the test to verify the first one is the interesting case**

Run: `uv run --with pytest pytest tests/integration/test_qmp_conformance.py -v -k memdump`
Expected: all three PASS. They pass before the change too — `memdump` already produces correct bytes, it just does so unsafely. They are the regression net that proves the guard did not break the command. Record that in the commit rather than pretending they were red.

- [ ] **Step 3: Add the halted-read guard**

In `src/debug/qmp.cpp`, in `handle_memdump`, replace the call to `DEBUG_SaveMemoryBin` with a guarded version:

```c
    /* memdump is the only QMP handler that reaches into guest state from
     * the socket thread; savestate, screendump and the input queue all
     * defer to the emulation thread. When the CPU is stopped for debugging
     * no guest code is executing, so memory is quiescent and reading it
     * here races nothing. When it is running, defer.
     *
     * The deferred path currently reports busy rather than queueing. A
     * request/response marshal with condition-variable signalling is
     * deliberately not built yet: the existing SAVESTATE_* idiom polls at
     * 100ms, which would destroy the 30-60Hz use case this command exists
     * for, and dumping a RUNNING guest at that rate has no measured
     * consumer. See the Stage 1 plan and DBX ticket for memdump-while-running. */
    if (!DEBUG_IsCpuPausedForDebug()) {
        if (use_temp) unlink(filepath.c_str());
        send_error("GenericError",
                   "memdump requires the CPU to be stopped for debugging; "
                   "halt via GDB or QMP stop first");
        return;
    }

    if (!DEBUG_SaveMemoryBin(filepath.c_str(), (uint32_t)address, (uint32_t)size)) {
```

Confirm `include/debug.h` is included by `qmp.cpp`; if it is not, add `#include "debug.h"` with the other includes at the top of the file.

- [ ] **Step 4: Write the test for the new refusal**

Append to `tests/integration/test_qmp_conformance.py`:

```python
def test_memdump_refuses_while_the_guest_is_running(emulator):
    """Reading guest memory from the QMP socket thread while the emulation
    thread is executing is a data race. The command refuses rather than
    returning bytes that were never coherent."""
    qmp = emulator.qmp()
    reply = qmp.execute_raw("memdump", {"address": 0x30000, "size": 16})
    assert "error" in reply
    assert "stopped" in reply["error"]["desc"].lower()
```

Note this test must not use the `gdb` fixture, because connecting a GDB client does not by itself pause the CPU — only an explicit halt does.

- [ ] **Step 5: Rebuild and run the tests**

Run: `./build-debug --enable-remotedebug`
Then: `uv run --with pytest pytest tests/integration/test_qmp_conformance.py -v`
Expected: PASS, 6 tests.

- [ ] **Step 6: Commit**

```bash
git add src/debug/qmp.cpp tests/integration/test_qmp_conformance.py
git commit -F - <<'EOF'
fix(qmp): only read guest memory in memdump when the CPU is stopped

memdump was the one QMP handler reaching into guest state from the
socket thread. savestate and loadstate go through SAVESTATE_Request*,
stop, cont and system_reset through EMULATOR_CheckPendingControl,
screendump through CAPTURE_TakeScreenshot, and input through the
mutex-guarded queue; memdump called DEBUG_SaveMemoryBin directly, racing
the emulation thread.

When DEBUG_IsCpuPausedForDebug() is true no guest code is executing and
memory is quiescent, so the direct read is safe and keeps its latency,
which is what the command exists for. Running-guest dumps now refuse
with a clear message instead of returning bytes that were never
coherent.

A deferred path for running-guest dumps is not built here: the existing
SAVESTATE_* idiom polls at 100ms, which would defeat the 30-60Hz use
case, and no consumer needs it yet.

The memdump/m agreement and argument-validation tests passed before this
change as well; they are the regression net for the guard.
EOF
```

---

### Task 8: Cover the rest of the QMP dispatch

`qmp.cpp:404` dispatches twelve commands. Tasks 6 and 7 cover four. Assert the rest answer, so a future refactor cannot quietly drop one.

**Files:**
- Modify: `tests/integration/test_qmp_conformance.py`

**Interfaces:**
- Consumes: the `emulator` and `qmp` fixtures.
- Produces: nothing.

- [ ] **Step 1: Write the tests**

Append to `tests/integration/test_qmp_conformance.py`:

```python
def test_query_commands_lists_every_dispatched_command(qmp):
    """Guards against a command being added to the dispatch at qmp.cpp:404
    without being announced, which makes it undiscoverable."""
    listed = {entry["name"] for entry in qmp.execute("query-commands")}
    expected = {
        "qmp_capabilities", "send-key", "input-send-event", "query-commands",
        "memdump", "screendump", "savestate", "loadstate", "stop", "cont",
        "system_reset", "query-status", "debug-break-on-exec",
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


def test_debug_break_on_exec_toggles(qmp):
    assert qmp.execute("debug-break-on-exec", {"enabled": True}) is not None
    assert qmp.execute("debug-break-on-exec", {"enabled": False}) is not None


def test_an_unknown_command_is_an_error_not_a_hang(qmp):
    reply = qmp.execute_raw("no-such-command")
    assert "error" in reply
```

Before running, read `handle_query_status` (`qmp.cpp:1036`) and `handle_query_commands` (`qmp.cpp:~530`) and correct the field names in `test_stop_then_cont_round_trips` and `test_query_commands_lists_every_dispatched_command` to match what they actually emit. If `query-commands` returns bare strings rather than objects with a `name` key, adjust the set comprehension.

- [ ] **Step 2: Run the tests**

Run: `uv run --with pytest pytest tests/integration/test_qmp_conformance.py -v`
Expected: PASS. Any failure here is a real gap in the server or in the expectation — investigate rather than deleting the assertion. If `loadstate` cannot be exercised without a prior save, add a test that saves then loads, rather than skipping it.

- [ ] **Step 3: Commit**

```bash
git add tests/integration/test_qmp_conformance.py
git commit -m "test: cover the remaining QMP dispatch entries"
```

---

### Task 9: Pin the config option names

`gdbport`/`qmpport` are not options. Writing them means the server silently falls back to 2159/4444 and a client connects to somebody else's emulator.

**Files:**
- Create: `tests/integration/test_config.py`

**Interfaces:**
- Consumes: `Emulator`, `port_is_listening` from `launcher`.
- Produces: nothing.

- [ ] **Step 1: Write the test**

```python
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
```

`test_the_spaceless_option_names_are_silently_ignored` binds the stock 2159. If a developer's own emulator already holds that port the test cannot distinguish, so guard it: skip with `pytest.skip("2159 already in use")` when `port_is_listening(2159)` is true before spawning.

- [ ] **Step 2: Run the test**

Run: `uv run --with pytest pytest tests/integration/test_config.py -v`
Expected: PASS, 3 tests.

- [ ] **Step 3: Commit**

```bash
git add tests/integration/test_config.py
git commit -m "test: pin the gdbserver/qmpserver port option names"
```

---

### Task 10: Rebase the surviving behavioral tests

`test_video_tools.py` imports `DOSVideoTools`, which does not exist in `dosbox_debug.py` — the file cannot be collected today. `run_all.py` declares a `dbxdebug>=0.2.1` PEP 723 dependency that nothing imports, which violates the licensing constraint even unused.

**Files:**
- Modify: `tests/integration/run_all.py`
- Rewrite: `tests/integration/test_video_tools.py`
- Modify: `tests/integration/test_debugbox.py`
- Modify: `tests/integration/README.md`

**Interfaces:**
- Consumes: `RawGDB`, the `emulator`/`gdb` fixtures.
- Produces: nothing.

- [ ] **Step 1: Confirm the broken import**

Run: `uv run --with pytest pytest tests/integration/test_video_tools.py --collect-only`
Expected: collection error, `ImportError: cannot import name 'DOSVideoTools'`.

- [ ] **Step 2: Remove the vestigial dependency**

In `tests/integration/run_all.py`, delete the `"dbxdebug>=0.2.1",` line from the PEP 723 block, leaving only `"pytest>=8.0",`. Update the module docstring: the suite now starts its own emulator, so the "Prerequisites: DOSBox-X running with..." section is replaced by "Prerequisites: DOSBox-X built with ./build-debug --enable-remotedebug".

- [ ] **Step 3: Rewrite the video tests against the raw client**

Replace `tests/integration/test_video_tools.py` entirely. Text-mode video memory starts at linear `0xB8000`, two bytes per cell, character then attribute.

```python
#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.11"
# dependencies = [
#     "pytest>=8.0",
# ]
# ///
"""Text-mode video memory reads, against the raw GDB client.

The old version of this file imported DOSVideoTools from dosbox_debug,
which never existed there, so it could not be collected. Screen-reading
convenience belongs in dbxdebug; what this repository needs to assert is
that the m packet reads video memory correctly.
"""

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent))

TEXT_VRAM = 0xB8000
COLS = 80
ROWS = 25


def read_screen(gdb, cols: int = COLS, rows: int = ROWS) -> list:
    raw = gdb.read_memory(TEXT_VRAM, cols * rows * 2)
    lines = []
    for row in range(rows):
        chars = []
        for col in range(cols):
            offset = (row * cols + col) * 2
            chars.append(chr(raw[offset]) if 32 <= raw[offset] < 127 else " ")
        lines.append("".join(chars).rstrip())
    return lines


def test_video_memory_is_readable(gdb):
    raw = gdb.read_memory(TEXT_VRAM, COLS * ROWS * 2)
    assert len(raw) == COLS * ROWS * 2


def test_the_booted_guest_has_text_on_screen(gdb):
    lines = read_screen(gdb)
    assert any(line.strip() for line in lines), (
        "every row was blank; the guest may not have finished booting")


def test_written_characters_read_back(gdb):
    gdb.halt()
    # 'X' with attribute 0x07 at row 0, column 0.
    assert gdb.write_memory(TEXT_VRAM, b"X\x07") is True
    assert gdb.read_memory(TEXT_VRAM, 2) == b"X\x07"
    assert read_screen(gdb)[0].startswith("X")


def test_attribute_bytes_decode_to_foreground_and_background(gdb):
    gdb.halt()
    # 0x1F = bright white on blue.
    assert gdb.write_memory(TEXT_VRAM, b"A\x1f") is True
    attr = gdb.read_memory(TEXT_VRAM + 1, 1)[0]
    assert attr & 0x0F == 0x0F      # foreground
    assert (attr >> 4) & 0x07 == 0x01  # background


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-v"]))
```

- [ ] **Step 4: Rebase test_debugbox.py onto the fixtures**

`test_debugbox.py:31` imports `DOSBoxInstance, GDBClient, QMPClient` from `dosbox_debug`. Replace that import and any `DOSBoxInstance(...)` construction with the `emulator`, `gdb` and `qmp` fixtures from `conftest.py`, and replace `dosbox_debug` method calls with their `RawGDB`/`RawQMP` equivalents: `read_registers()` now returns a list of 16 ints rather than a `Registers` object, so index it (register 8 is the linear PC, 10 is CS, 12 is DS). Leave the DEBUGBOX behavior each test asserts unchanged.

- [ ] **Step 5: Update the README**

In `tests/integration/README.md`, replace the "Quick Start" steps 2 and 3. The suite no longer needs a manually started emulator: it builds nothing and starts its own. Document that the suite is standard library plus pytest only, and why — `dbxdebug` is Polyform Shield and this repository is GPLv2. Point readers at `dbxdebug` for automation work and at the spec for the reasoning.

- [ ] **Step 6: Run the whole suite**

Run: `uv run tests/integration/run_all.py -v`
Expected: PASS across `test_protocol_gdb_unit.py`, `test_protocol_qmp_unit.py`, `test_launcher.py`, `test_gdb_conformance.py`, `test_qmp_conformance.py`, `test_config.py`, `test_video_tools.py`, `test_debugbox.py`.

- [ ] **Step 7: Commit**

```bash
git add tests/integration/run_all.py tests/integration/test_video_tools.py tests/integration/test_debugbox.py tests/integration/README.md
git commit -F - <<'EOF'
test: rebase behavioral tests onto the conformance harness

test_video_tools.py imported DOSVideoTools from dosbox_debug, which was
never defined there, so the file could not be collected at all. It is
rewritten against the raw GDB client, reading text-mode video memory at
0xB8000 directly.

test_debugbox.py moves onto the emulator fixtures, so it starts its own
isolated emulator instead of requiring one to be running on 2159/4444.

run_all.py declared a dbxdebug>=0.2.1 dependency that nothing imported.
dbxdebug is Polyform Shield 1.0.0 and this project is GPLv2, so it
cannot be a test dependency here even unused.
EOF
```

---

### Task 11: Deprecate `dosbox_debug.py` and guard packed addresses

The file stays until Stage 2 ships `dbxdebug`, because `powerbasic-decompile` imports its clients by path. But its consumers pack `(seg << 16) | off`, which is correct against the old stub and wrong from Task 4 onwards.

**Files:**
- Modify: `tests/integration/dosbox_debug.py` (module docstring, `set_breakpoint`, `remove_breakpoint`)
- Test: `tests/integration/test_dosbox_debug_guard.py`

**Interfaces:**
- Consumes: nothing.
- Produces: `PackedAddressError`, exported from `dosbox_debug`.

- [ ] **Step 1: Write the failing test**

```python
#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.11"
# dependencies = [
#     "pytest>=8.0",
# ]
# ///
"""dosbox_debug.py is deprecated but still imported by downstream projects
until dbxdebug ships. Its own (seg << 4) + off conversion is the linear
address, so it becomes CORRECT when the stub changes. Its callers are the
problem: a caller that packed (seg << 16) | off was right against the old
stub and is wrong against the new one, and Z0 answers OK either way."""

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent))

from dosbox_debug import GDBClient, PackedAddressError


def test_a_packed_far_pointer_is_rejected():
    client = GDBClient()
    # 0824:5A90 packed. Real-mode linear addresses never reach here.
    with pytest.raises(PackedAddressError, match="packed far pointer"):
        client.set_breakpoint(0x08245A90)


def test_remove_breakpoint_rejects_it_too():
    client = GDBClient()
    with pytest.raises(PackedAddressError):
        client.remove_breakpoint(0x08245A90)


def test_a_normal_linear_address_is_not_rejected():
    """0x30000 is a legitimate conventional-memory address. The guard must
    not fire on it -- it only rejects values above the real-mode ceiling."""
    client = GDBClient()
    with pytest.raises(AttributeError):
        # No socket: the guard passed and it reached the send.
        client.set_breakpoint(0x30000)


def test_the_seg_off_string_form_still_converts_linearly():
    client = GDBClient()
    with pytest.raises(AttributeError):
        client.set_breakpoint("0824:5a90")  # -> 0x8340, under the ceiling


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-v"]))
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `uv run tests/integration/test_dosbox_debug_guard.py -v`
Expected: FAIL — `ImportError: cannot import name 'PackedAddressError'`

- [ ] **Step 3: Add the deprecation notice and the guard**

At the top of `tests/integration/dosbox_debug.py`, after the existing docstring, add:

```python
DEPRECATED. This module is superseded by `dbxdebug`, which carries the same
protocol clients plus session lifecycle, and is maintained on its own release
cycle. It survives only until dbxdebug ships a replacement, because
powerbasic-decompile imports GDBClient and QMPClient from it by path.

New code in this repository should use tests/integration/protocol/ instead.
See docs/superpowers/specs/2026-09-03-dosbox-debug-harness-design.md.
```

Then add the exception class beside `GDBError`:

```python
class PackedAddressError(ValueError):
    """A breakpoint address that looks like a packed far pointer.

    Z0/z0 now take a LINEAR address. A caller that packed (seg << 16) | off
    was correct against older builds and is wrong against this one, and the
    stub answers OK either way -- the breakpoint simply never fires. Real-mode
    linear addresses stop just past 1 MB including the HMA, so anything at or
    above 0x110000 is a packed pair rather than an address.
    """
```

Add the guard as a module-level helper:

```python
REAL_MODE_CEILING = 0x110000


def _check_linear(addr: int) -> int:
    if addr >= REAL_MODE_CEILING:
        seg, off = addr >> 16, addr & 0xFFFF
        raise PackedAddressError(
            f"0x{addr:X} looks like a packed far pointer ({seg:04X}:{off:04X}). "
            f"Breakpoint addresses are linear: pass {seg * 16 + off:#x} "
            f"(seg * 16 + off) instead.")
    return addr
```

Then call it in both breakpoint methods, after the existing `seg:off` string conversion and before the packet is sent:

```python
    def set_breakpoint(self, addr: Union[int, str]) -> bool:
        """Set a software breakpoint.

        Args:
            addr: Linear address (int) or seg:off string
        """
        if isinstance(addr, str) and ":" in addr:
            seg, off = addr.split(":")
            addr = (int(seg, 16) << 4) + int(off, 16)

        addr = _check_linear(addr)

        # Z0 = software breakpoint, kind=1 for x86
        response = self._send_packet(f"Z0,{addr:x},1")
        return response == "OK"
```

Apply the same single added line to `remove_breakpoint`.

- [ ] **Step 4: Run the test to verify it passes**

Run: `uv run tests/integration/test_dosbox_debug_guard.py -v`
Expected: PASS, 4 tests.

- [ ] **Step 5: Commit**

```bash
git add tests/integration/dosbox_debug.py tests/integration/test_dosbox_debug_guard.py
git commit -F - <<'EOF'
fix: reject packed far pointers in dosbox_debug breakpoints

Z0 and z0 now take a linear address, so a caller that packed
(seg << 16) | off was correct against older builds and is wrong against
this one. The stub answers OK either way and the breakpoint simply never
fires, which is the silent failure this whole change set exists to
remove.

Breakpoint addresses at or above 0x110000 are therefore rejected: real
mode tops out just past 1 MB including the HMA, so nothing legitimate
lands there, while a packed 0824:5A90 arrives as 0x08245A90.

The module's own (seg << 4) + off conversion is already the linear
address and needs no change -- its breakpoints start working correctly
above 64 KB for the first time.

Also marks the module deprecated in favour of dbxdebug. It stays until
dbxdebug ships, because powerbasic-decompile imports its clients by path.
EOF
```

---

### Task 12: Documentation and follow-up tickets

**Files:**
- Modify: `docs/REMOTEDEBUG.md`
- Beads: new issues

**Interfaces:**
- Consumes: nothing.
- Produces: nothing.

- [ ] **Step 1: Correct the addressing section**

In `docs/REMOTEDEBUG.md`, find the "Memory Addressing" section (around line 77) and the "Limitations" section (around line 81). State that `m`, `M`, `Z0` and `z0` all take linear addresses; that a `seg:off` pair converts as `seg * 16 + off`; and that builds advertising `dosbox-x-linear-bp+` in `qSupported` have this behavior while older ones split the `Z0` argument as a far pointer and silently fail above 64 KB.

- [ ] **Step 2: Correct the Python examples**

The "Python Automation Library" section (around line 332) documents `DOSBoxInstance`, `GDBClient` and `QMPClient` from `dosbox_debug`. Add a note at the head of that section marking it deprecated, pointing at `dbxdebug` for automation and at `tests/integration/protocol/` for in-repo conformance work. Do not delete the examples — the module still exists this stage.

- [ ] **Step 3: Document the new memdump precondition**

Add to the QMP `memdump` documentation that the CPU must be stopped for debugging, and why: the command reads guest memory from the QMP thread, which is only safe when the emulation thread is not executing.

- [ ] **Step 4: File the follow-up tickets**

```bash
bd create "Fix g/G register asymmetry on EIP" \
  -d "DEBUG_GetRegister(8) returns SegPhys(cs)+reg_eip, a linear PC, while DEBUG_SetRegister(8) writes reg_eip, an offset (debug.cpp:6133 vs :6155). A g then G round-trip therefore corrupts EIP. Out of scope for the Stage 1 PR; the conformance client works around it by writing CS and EIP separately via P." \
  -p 2 -t bug

bd create "Deferred memdump path for a running guest" \
  -d "memdump now refuses unless the CPU is stopped for debugging. A request/response marshal to the emulation thread with condition-variable signalling would let a running guest be dumped race-free, but the existing SAVESTATE_* idiom polls at 100ms which would defeat the 30-60Hz use case. Build when a consumer needs it. See docs/superpowers/specs/2026-09-03-dosbox-debug-harness-design.md section 3.1." \
  -p 3 -t feature
```

- [ ] **Step 5: Run the full suite one last time**

Run: `uv run tests/integration/run_all.py -v`
Expected: PASS.

- [ ] **Step 6: Commit**

```bash
git add docs/REMOTEDEBUG.md .beads/issues.jsonl
git commit -F - <<'EOF'
docs: correct the remote debug addressing rules

m, M, Z0 and z0 all take linear addresses now. Records that builds
advertising dosbox-x-linear-bp+ in qSupported behave this way, and that
older ones split the Z0 argument as a far pointer and fail silently
above 64 KB.

Marks the dosbox_debug.py section deprecated in favour of dbxdebug, and
documents that memdump requires the CPU to be stopped.
EOF
```

---

## Stage 1 exit criteria

- `uv run tests/integration/run_all.py` passes with no third-party imports beyond pytest.
- A breakpoint set at a linear address above `0x10000` fires there.
- `qSupported` advertises `dosbox-x-linear-bp+`.
- `savestate` completes in under a second while halted at a GDB breakpoint.
- `memdump` refuses while the guest is running and agrees byte for byte with `m` while halted.
- `dosbox_debug.py` still imports and still works for `powerbasic-decompile`, and raises on packed breakpoint addresses.
- No file under `~/projects/lokkju/powerbasic-decompile` has been modified.
