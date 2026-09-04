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
        """Write ONE register via `P`. Register 8 is EIP, an offset within CS,
        in both directions -- a `g`/`G` round-trip is safe on builds that
        advertise `dosbox-x-eip-offset+`. Older builds returned
        SegPhys(cs)+reg_eip from `g` while `G` wrote reg_eip, so a round-trip
        silently moved the program counter there."""
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
