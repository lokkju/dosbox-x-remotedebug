"""
Simple GDB and QMP client implementations for DOSBox-X integration tests.

This module provides lightweight client implementations that don't rely on
external dependencies, avoiding issues with third-party libraries.

DEPRECATED. This module is superseded by `dbxdebug`, which carries the same
protocol clients plus session lifecycle, and is maintained on its own release
cycle. It survives only until dbxdebug ships a replacement, because
powerbasic-decompile imports GDBClient and QMPClient from it by path.

New code in this repository should use tests/integration/protocol/ instead.
See docs/superpowers/specs/2026-09-03-dosbox-debug-harness-design.md.
"""

import json
import socket
import time
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Optional, Union


class GDBError(Exception):
    """GDB protocol error."""
    pass


class QMPError(Exception):
    """QMP protocol error."""
    pass


class PackedAddressError(ValueError):
    """A breakpoint address that looks like a packed far pointer.

    Z0/z0 now take a LINEAR address. A caller that packed (seg << 16) | off
    was correct against older builds and is wrong against this one, and the
    stub answers OK either way -- the breakpoint simply never fires. Real-mode
    linear addresses stop just past 1 MB including the HMA, so anything at or
    above 0x110000 is a packed pair rather than an address.

    This catches the common case and not every case. A packed pair with a
    small segment is indistinguishable from a legitimate linear address --
    `0010:0000` arrives as 0x00100000 and passes, meaning linear 0x100000
    rather than the intended 0x100. Real DOS programs load well above
    segment 0, so the realistic packing is caught.
    """


@dataclass
class Registers:
    """CPU registers from GDB.

    IMPORTANT -- `eip` CHANGED MEANING. On builds advertising
    `dosbox-x-eip-offset+` in qSupported, GDB register 8 is EIP: an offset
    within CS. Older builds returned SegPhys(cs) + reg_eip, a linear
    address. Compute the linear PC as `cs * 16 + eip`. Code written against
    an older build that used `eip` directly as a linear address is now
    silently wrong.
    """
    eax: int = 0
    ecx: int = 0
    edx: int = 0
    ebx: int = 0
    esp: int = 0
    ebp: int = 0
    esi: int = 0
    edi: int = 0
    eip: int = 0
    eflags: int = 0
    cs: int = 0
    ss: int = 0
    ds: int = 0
    es: int = 0
    fs: int = 0
    gs: int = 0

    def __getitem__(self, key):
        """Allow dict-like access for compatibility."""
        return getattr(self, key)

    def __contains__(self, key):
        """Support 'in' operator for dict-like compatibility."""
        return key in self.keys()

    def __iter__(self):
        """Allow iteration over register names."""
        return iter(self.keys())

    def get(self, key, default=None):
        """Allow dict-like get() for compatibility."""
        return getattr(self, key, default)

    def keys(self):
        """Return register names."""
        return ["eax", "ecx", "edx", "ebx", "esp", "ebp", "esi", "edi",
                "eip", "eflags", "cs", "ss", "ds", "es", "fs", "gs"]


REAL_MODE_CEILING = 0x110000


def _check_linear(addr: int) -> int:
    if addr >= REAL_MODE_CEILING:
        seg, off = addr >> 16, addr & 0xFFFF
        raise PackedAddressError(
            f"0x{addr:X} looks like a packed far pointer ({seg:04X}:{off:04X}). "
            f"Breakpoint addresses are linear: pass {seg * 16 + off:#x} "
            f"(seg * 16 + off) instead.")
    return addr


class GDBClient:
    """Simple GDB Remote Serial Protocol client with DOS video memory support."""

    # VGA text mode video memory
    VIDEO_MEM_ADDR = 0xB8000
    VIDEO_MEM_SIZE = 4000  # 80x25x2 bytes

    # BIOS data area addresses
    BIOS_TIMER_TICKS = 0x46C  # 32-bit timer tick count
    BIOS_VIDEO_MODE = 0x449  # Current video mode

    def __init__(self, host: str = "localhost", port: int = 2159, timeout: float = 5.0):
        self.host = host
        self.port = port
        self.timeout = timeout
        self._socket: Optional[socket.socket] = None

    def __enter__(self):
        self.connect()
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        self.close()
        return False

    def connect(self):
        """Connect to the GDB server."""
        self._socket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self._socket.settimeout(self.timeout)
        self._socket.connect((self.host, self.port))

    def close(self):
        """Close the connection."""
        if self._socket:
            try:
                # Send detach before closing
                self._send_packet("D")
            except:
                pass
            self._socket.close()
            self._socket = None

    def _checksum(self, data: str) -> int:
        """Calculate GDB packet checksum."""
        return sum(ord(c) for c in data) % 256

    def _send_packet(self, data: str) -> str:
        """Send a GDB packet and receive response."""
        if not self._socket:
            raise GDBError("Not connected")

        # Build packet: $data#checksum
        checksum = self._checksum(data)
        packet = f"${data}#{checksum:02x}"
        self._socket.send(packet.encode())

        # Read response
        response = b""
        while True:
            chunk = self._socket.recv(4096)
            if not chunk:
                break
            response += chunk
            # Look for end of packet
            if b"#" in response:
                # Check if we have the full checksum
                idx = response.rfind(b"#")
                if len(response) >= idx + 3:
                    break

        response_str = response.decode()

        # Strip ACK if present
        if response_str.startswith("+"):
            response_str = response_str[1:]

        # Parse packet
        if response_str.startswith("$") and "#" in response_str:
            # Extract data between $ and #
            start = response_str.index("$") + 1
            end = response_str.rindex("#")
            return response_str[start:end]

        return response_str

    def halt(self) -> str:
        """Send halt/break signal (Ctrl+C)."""
        if not self._socket:
            raise GDBError("Not connected")
        # Send break character
        self._socket.send(b"\x03")
        time.sleep(0.1)
        # Read stop reply
        response = self._socket.recv(4096).decode()
        if response.startswith("+"):
            response = response[1:]
        if "$" in response and "#" in response:
            start = response.index("$") + 1
            end = response.rindex("#")
            return response[start:end]
        return response

    def enable_no_ack_mode(self) -> bool:
        """Request no-ack mode (QStartNoAckMode)."""
        response = self._send_packet("QStartNoAckMode")
        return response == "OK"

    def query_halt_reason(self) -> str:
        """Query why the target halted."""
        return self._send_packet("?")

    def read_registers(self) -> Registers:
        """Read all CPU registers.

        `eip` is register 8: an offset within CS on builds advertising
        `dosbox-x-eip-offset+`, not a linear address. Compute the linear PC
        as `cs * 16 + eip`; see the `Registers` docstring for detail.
        """
        response = self._send_packet("g")

        if response.startswith("E"):
            raise GDBError(f"Error reading registers: {response}")

        # Parse 32-bit registers (8 hex chars each)
        # Order: EAX, ECX, EDX, EBX, ESP, EBP, ESI, EDI, EIP, EFLAGS, CS, SS, DS, ES, FS, GS
        def get_reg(offset: int, size: int = 8) -> int:
            hex_str = response[offset:offset + size]
            if len(hex_str) < size:
                return 0
            # GDB sends little-endian, need to swap bytes
            bytes_list = [hex_str[i:i+2] for i in range(0, len(hex_str), 2)]
            bytes_list.reverse()
            return int("".join(bytes_list), 16)

        return Registers(
            eax=get_reg(0),
            ecx=get_reg(8),
            edx=get_reg(16),
            ebx=get_reg(24),
            esp=get_reg(32),
            ebp=get_reg(40),
            esi=get_reg(48),
            edi=get_reg(56),
            eip=get_reg(64),
            eflags=get_reg(72),
            cs=get_reg(80),
            ss=get_reg(88),
            ds=get_reg(96),
            es=get_reg(104),
            fs=get_reg(112),
            gs=get_reg(120),
        )

    def read_register(self, reg_num: int) -> int:
        """Read a single register by index.

        Args:
            reg_num: Register index (0=EAX, 1=ECX, ..., 8=EIP, 9=EFLAGS, 10-15=segments)
        """
        response = self._send_packet(f"p{reg_num:x}")

        if response.startswith("E"):
            raise GDBError(f"Error reading register {reg_num}: {response}")

        # GDB sends little-endian hex, need to swap bytes
        if len(response) >= 2:
            bytes_list = [response[i:i+2] for i in range(0, len(response), 2)]
            bytes_list.reverse()
            return int("".join(bytes_list), 16)
        return 0

    def read_memory(self, addr: Union[int, str], size: int) -> bytes:
        """Read memory from target.

        Args:
            addr: Linear address (int) or seg:off string (e.g., "b800:0000")
            size: Number of bytes to read
        """
        if isinstance(addr, str) and ":" in addr:
            # Convert seg:off to linear
            seg, off = addr.split(":")
            addr = (int(seg, 16) << 4) + int(off, 16)

        response = self._send_packet(f"m{addr:x},{size:x}")

        if response.startswith("E"):
            raise GDBError(f"Error reading memory: {response}")

        # Convert hex string to bytes
        return bytes.fromhex(response)

    def write_memory(self, addr: Union[int, str], data: bytes) -> bool:
        """Write memory to target.

        Args:
            addr: Linear address (int) or seg:off string
            data: Bytes to write
        """
        if isinstance(addr, str) and ":" in addr:
            seg, off = addr.split(":")
            addr = (int(seg, 16) << 4) + int(off, 16)

        hex_data = data.hex()
        response = self._send_packet(f"M{addr:x},{len(data):x}:{hex_data}")

        return response == "OK"

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

    def remove_breakpoint(self, addr: Union[int, str]) -> bool:
        """Remove a software breakpoint."""
        if isinstance(addr, str) and ":" in addr:
            seg, off = addr.split(":")
            addr = (int(seg, 16) << 4) + int(off, 16)

        addr = _check_linear(addr)

        response = self._send_packet(f"z0,{addr:x},1")
        return response == "OK"

    def step(self) -> str:
        """Single step execution."""
        return self._send_packet("s")

    def continue_(self) -> str:
        """Continue execution.

        Note: GDB 'c' command has no immediate response - it only sends S05
        when execution stops (breakpoint, halt, etc.). So we just send the
        packet and wait for ACK, not a full response.
        """
        if not self._socket:
            raise GDBError("Not connected")

        # Send continue packet
        checksum = self._checksum("c")
        packet = f"$c#{checksum:02x}"
        self._socket.send(packet.encode())

        # Just read ACK, don't wait for response packet
        old_timeout = self._socket.gettimeout()
        self._socket.settimeout(0.5)
        try:
            ack = self._socket.recv(1)
            return ack.decode() if ack == b"+" else ""
        except socket.timeout:
            return ""
        finally:
            self._socket.settimeout(old_timeout)

    def wait_for_stop(self, timeout: float = 5.0) -> str:
        """Wait for a stop reply (S05) after continue.

        Args:
            timeout: How long to wait for stop reply

        Returns:
            The stop reply (e.g., "S05") or empty string if timeout
        """
        if not self._socket:
            raise GDBError("Not connected")

        old_timeout = self._socket.gettimeout()
        self._socket.settimeout(timeout)
        try:
            response = b""
            while True:
                chunk = self._socket.recv(4096)
                if not chunk:
                    break
                response += chunk
                # Look for stop reply packet
                if b"$" in response and b"#" in response:
                    idx = response.rfind(b"#")
                    if len(response) >= idx + 3:
                        break

            response_str = response.decode()
            if response_str.startswith("+"):
                response_str = response_str[1:]
            if "$" in response_str and "#" in response_str:
                start = response_str.index("$") + 1
                end = response_str.rindex("#")
                return response_str[start:end]
            return response_str
        except socket.timeout:
            return ""
        finally:
            self._socket.settimeout(old_timeout)

    def detach(self) -> str:
        """Detach from target."""
        return self._send_packet("D")

    # =========================================================================
    # Video Memory Methods
    # =========================================================================

    def screen_raw(self) -> bytes:
        """Read raw video memory (character + attribute pairs)."""
        return self.read_memory(self.VIDEO_MEM_ADDR, self.VIDEO_MEM_SIZE)

    def screen_dump(self, width: int = 80, height: int = 25) -> list:
        """Read screen as list of text lines (characters only)."""
        raw = self.screen_raw()
        lines = []
        for row in range(height):
            line = ""
            for col in range(width):
                offset = (row * width + col) * 2
                if offset < len(raw):
                    char = raw[offset]
                    # Convert to printable character
                    if 32 <= char < 127:
                        line += chr(char)
                    else:
                        line += " "
            lines.append(line.rstrip())
        return lines

    def screen_line(self, row: int = 24, width: int = 80) -> str:
        """Get a single screen line."""
        lines = self.screen_dump(width, row + 1)
        return lines[row] if row < len(lines) else ""

    def screen_dump_with_ticks(self, width: int = 80, height: int = 25) -> tuple:
        """Read screen and timer ticks atomically."""
        lines = self.screen_dump(width, height)
        ticks = self.read_timer_ticks()
        return lines, ticks

    def screen_debug(self, width: int = 80, height: int = 25) -> list:
        """Read screen with full debug info (char, attribute, decoded)."""
        raw = self.screen_raw()
        result = []
        for row in range(height):
            row_data = []
            for col in range(width):
                offset = (row * width + col) * 2
                if offset + 1 < len(raw):
                    char = raw[offset]
                    attr = raw[offset + 1]
                    row_data.append({
                        "char": chr(char) if 32 <= char < 127 else ".",
                        "code": char,
                        "attr": attr,
                        "attr_info": decode_vga_attribute(attr),
                    })
            result.append(row_data)
        return result

    def read_video_mode(self) -> int:
        """Read current video mode from BIOS data area."""
        data = self.read_memory(self.BIOS_VIDEO_MODE, 1)
        return data[0] if data else 0

    def read_timer_ticks(self) -> int:
        """Read BIOS timer tick count (18.2 Hz)."""
        data = self.read_memory(self.BIOS_TIMER_TICKS, 4)
        if len(data) >= 4:
            return int.from_bytes(data, byteorder="little")
        return 0


class QMPClient:
    """Simple QMP (QEMU Monitor Protocol) client."""

    def __init__(self, host: str = "localhost", port: int = 4444, timeout: float = 5.0):
        self.host = host
        self.port = port
        self.timeout = timeout
        self._socket: Optional[socket.socket] = None
        self._connected = False

    def __enter__(self):
        self.connect()
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        self.close()
        return False

    def connect(self):
        """Connect to the QMP server and negotiate capabilities."""
        self._socket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self._socket.settimeout(self.timeout)
        self._socket.connect((self.host, self.port))

        # Read greeting
        greeting = self._recv_json()
        if "QMP" not in greeting:
            raise QMPError(f"Invalid QMP greeting: {greeting}")

        # Send capabilities negotiation
        self._send_command("qmp_capabilities")
        self._connected = True

    def close(self):
        """Close the connection."""
        if self._socket:
            self._socket.close()
            self._socket = None
        self._connected = False

    def _recv_json(self) -> dict:
        """Receive a JSON response."""
        if not self._socket:
            raise QMPError("Not connected")

        data = b""
        while True:
            chunk = self._socket.recv(4096)
            if not chunk:
                break
            data += chunk
            # Try to parse JSON
            try:
                return json.loads(data.decode())
            except json.JSONDecodeError:
                continue

        raise QMPError(f"Failed to receive valid JSON: {data}")

    def _send_command(self, command: str, arguments: dict = None) -> dict:
        """Send a QMP command and return the response."""
        if not self._socket:
            raise QMPError("Not connected")

        msg = {"execute": command}
        if arguments:
            msg["arguments"] = arguments

        self._socket.send((json.dumps(msg) + "\n").encode())
        return self._recv_json()

    def send_key(self, keys: list, hold_time: int = 100) -> dict:
        """Send key press via send-key command.

        Args:
            keys: List of key names (qcodes)
            hold_time: Hold time in milliseconds
        """
        key_objects = [{"type": "qcode", "data": k} for k in keys]
        return self._send_command("send-key", {
            "keys": key_objects,
            "hold-time": hold_time
        })

    def input_send_event(self, events: list) -> dict:
        """Send input events via input-send-event command."""
        return self._send_command("input-send-event", {"events": events})

    def query_commands(self) -> list:
        """Query available commands."""
        result = self._send_command("query-commands")
        return result.get("return", [])

    def key_down(self, key: str) -> dict:
        """Press a key down (without releasing)."""
        return self.input_send_event([{
            "type": "key",
            "data": {"down": True, "key": {"type": "qcode", "data": key}}
        }])

    def key_up(self, key: str) -> dict:
        """Release a key."""
        return self.input_send_event([{
            "type": "key",
            "data": {"down": False, "key": {"type": "qcode", "data": key}}
        }])

    def key_press(self, key: str, hold_time: float = 0.1) -> dict:
        """Press and release a key with hold time.

        Args:
            key: Key qcode name
            hold_time: Time in seconds to hold key
        """
        self.key_down(key)
        time.sleep(hold_time)
        return self.key_up(key)

    def type_text(self, text: str, delay: float = 0.1):
        """Type text character by character.

        This implementation sends each character separately with a delay,
        which is more reliable than batching.
        """
        # Mapping for special characters
        special_keys = {
            " ": "spc",
            "\n": "ret",
            "\r": "ret",  # Carriage return also maps to Enter
            "\t": "tab",
            ".": "dot",
            ",": "comma",
            ";": "semicolon",
            "'": "apostrophe",
            "`": "grave_accent",
            "-": "minus",
            "=": "equal",
            "[": "bracket_left",
            "]": "bracket_right",
            "\\": "backslash",
            "/": "slash",
        }

        shift_keys = {
            "!": "1", "@": "2", "#": "3", "$": "4", "%": "5",
            "^": "6", "&": "7", "*": "8", "(": "9", ")": "0",
            "_": "minus", "+": "equal", "{": "bracket_left",
            "}": "bracket_right", "|": "backslash", ":": "semicolon",
            '"': "apostrophe", "<": "comma", ">": "dot", "?": "slash",
            "~": "grave_accent",
        }

        for char in text:
            keys = []

            if char in special_keys:
                keys = [special_keys[char]]
            elif char in shift_keys:
                keys = ["shift", shift_keys[char]]
            elif char.isupper():
                keys = ["shift", char.lower()]
            elif char.isalnum():
                keys = [char.lower()]
            else:
                # Skip unknown characters
                continue

            self.send_key(keys)
            time.sleep(delay)

    def debug_break_on_exec(self, enabled: bool) -> dict:
        """Enable/disable debug-break-on-exec."""
        return self._send_command("debug-break-on-exec", {"enabled": enabled})

    def query_status(self) -> dict:
        """Query emulator status."""
        return self._send_command("query-status")

    def stop(self) -> dict:
        """Stop/pause the emulator."""
        return self._send_command("stop")

    def cont(self) -> dict:
        """Continue/resume the emulator."""
        return self._send_command("cont")


@contextmanager
def gdb_connection(host: str = "localhost", port: int = 2159, timeout: float = 5.0):
    """Context manager for GDB connection with automatic cleanup."""
    client = GDBClient(host=host, port=port, timeout=timeout)
    try:
        client.connect()
        yield client
    finally:
        client.close()


@contextmanager
def qmp_connection(host: str = "localhost", port: int = 4444, timeout: float = 5.0):
    """Context manager for QMP connection with automatic cleanup."""
    client = QMPClient(host=host, port=port, timeout=timeout)
    try:
        client.connect()
        yield client
    finally:
        client.close()


# =============================================================================
# VGA Attribute Helpers
# =============================================================================

@dataclass
class VGAAttribute:
    """Decoded VGA text mode attribute byte."""
    foreground: int
    background: int
    bright: bool
    blink: bool

    @property
    def fg_color(self) -> str:
        """Get foreground color name."""
        colors = ["black", "blue", "green", "cyan", "red", "magenta", "brown", "white"]
        base = self.foreground & 0x07
        return colors[base]

    @property
    def bg_color(self) -> str:
        """Get background color name."""
        colors = ["black", "blue", "green", "cyan", "red", "magenta", "brown", "white"]
        return colors[self.background & 0x07]


def decode_vga_attribute(attr: int) -> VGAAttribute:
    """Decode a VGA text mode attribute byte.

    Attribute byte format:
    - Bits 0-2: Foreground color (0-7)
    - Bit 3: Bright/intensity
    - Bits 4-6: Background color (0-7)
    - Bit 7: Blink (or bright background if blink disabled)
    """
    return VGAAttribute(
        foreground=attr & 0x07,
        background=(attr >> 4) & 0x07,
        bright=bool(attr & 0x08),
        blink=bool(attr & 0x80),
    )


def format_attribute_info(attr: int) -> str:
    """Format VGA attribute as human-readable string."""
    info = decode_vga_attribute(attr)
    parts = [f"fg={info.fg_color}"]
    if info.bright:
        parts.append("bright")
    parts.append(f"bg={info.bg_color}")
    if info.blink:
        parts.append("blink")
    return " ".join(parts)


# =============================================================================
# DOSBox-X Instance Manager
# =============================================================================

import subprocess
import os
import signal

class DOSBoxInstance:
    """Context manager for running DOSBox-X instances.

    Usage:
        with DOSBoxInstance() as dbx:
            dbx.gdb.read_registers()
            dbx.qmp.type_text("DIR\\r")
            lines = dbx.screen_dump()
    """

    DEFAULT_EXECUTABLE = "./src/dosbox-x"
    DEFAULT_CONFIG = "tests/integration/test.conf"
    DEFAULT_GDB_PORT = 2159
    DEFAULT_QMP_PORT = 4444
    STARTUP_TIMEOUT = 5.0

    def __init__(
        self,
        executable: str = None,
        config: str = None,
        gdb_port: int = None,
        qmp_port: int = None,
        working_dir: str = None,
    ):
        self.executable = executable or self.DEFAULT_EXECUTABLE
        self.config = config or self.DEFAULT_CONFIG
        self.gdb_port = gdb_port or self.DEFAULT_GDB_PORT
        self.qmp_port = qmp_port or self.DEFAULT_QMP_PORT
        self.working_dir = working_dir

        self._process: Optional[subprocess.Popen] = None
        self._gdb: Optional[GDBClient] = None
        self._qmp: Optional[QMPClient] = None

    def __enter__(self):
        self.start()
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        self.stop()
        return False

    def start(self):
        """Start DOSBox-X and connect clients."""
        # Kill any existing instances first
        self._kill_existing()

        # Start DOSBox-X
        cmd = [self.executable, "-conf", self.config]
        kwargs = {
            "stdout": subprocess.DEVNULL,
            "stderr": subprocess.DEVNULL,
            "start_new_session": True,
        }
        if self.working_dir:
            kwargs["cwd"] = self.working_dir

        self._process = subprocess.Popen(cmd, **kwargs)

        # Wait for servers to be available
        if not self._wait_for_servers():
            self.stop()
            raise RuntimeError("DOSBox-X failed to start or servers not available")

        # Connect clients
        self._gdb = GDBClient(port=self.gdb_port)
        self._gdb.connect()

        self._qmp = QMPClient(port=self.qmp_port)
        self._qmp.connect()

    def stop(self):
        """Stop DOSBox-X and close connections."""
        # Close clients
        if self._gdb:
            try:
                self._gdb.close()
            except:
                pass
            self._gdb = None

        if self._qmp:
            try:
                self._qmp.close()
            except:
                pass
            self._qmp = None

        # Stop process
        if self._process:
            try:
                os.killpg(os.getpgid(self._process.pid), signal.SIGKILL)
            except:
                try:
                    self._process.kill()
                except:
                    pass
            self._process = None

    def _kill_existing(self):
        """Kill any existing DOSBox-X processes."""
        try:
            subprocess.run(
                ["pkill", "-9", "-f", "dosbox-x"],
                capture_output=True,
                timeout=2.0,
            )
            time.sleep(0.5)
        except:
            pass

    def _wait_for_servers(self) -> bool:
        """Wait for GDB and QMP servers to become available."""
        deadline = time.time() + self.STARTUP_TIMEOUT

        while time.time() < deadline:
            if self._process.poll() is not None:
                # Process exited
                return False

            try:
                # Try QMP
                sock = socket.socket()
                sock.settimeout(0.5)
                sock.connect(("localhost", self.qmp_port))
                sock.close()

                # Try GDB
                sock = socket.socket()
                sock.settimeout(0.5)
                sock.connect(("localhost", self.gdb_port))
                sock.close()

                return True
            except:
                time.sleep(0.2)

        return False

    @property
    def gdb(self) -> GDBClient:
        """Get the GDB client."""
        if not self._gdb:
            raise RuntimeError("DOSBox-X not running")
        return self._gdb

    @property
    def qmp(self) -> QMPClient:
        """Get the QMP client."""
        if not self._qmp:
            raise RuntimeError("DOSBox-X not running")
        return self._qmp

    def screen_dump(self, width: int = 80, height: int = 25) -> list:
        """Read screen as list of text lines."""
        return self.gdb.screen_dump(width, height)

    def screen_line(self, row: int = 24, width: int = 80) -> str:
        """Get a single screen line."""
        return self.gdb.screen_line(row, width)

    def halt(self):
        """Halt the CPU via GDB."""
        return self.gdb.halt()

    def continue_(self):
        """Continue execution via GDB."""
        return self.gdb.continue_()

    def wait_for_stop(self, timeout: float = 5.0) -> str:
        """Wait for execution to stop (e.g., breakpoint hit)."""
        return self.gdb.wait_for_stop(timeout)

    def query_status(self) -> dict:
        """Query emulator status via QMP."""
        return self.qmp.query_status()

    def type_text(self, text: str, delay: float = 0.15):
        """Type text via QMP.

        Args:
            text: Text to type. Use \\r for Enter.
            delay: Delay between keystrokes (default 0.15s for reliability)
        """
        self.qmp.type_text(text, delay)

    def run_command(self, command: str, wait_after: float = 0.5, verify: bool = True):
        """Type a DOS command and press Enter.

        Args:
            command: The command to run (without Enter)
            wait_after: Time to wait after pressing Enter
            verify: If True, verify command appeared on screen before pressing Enter

        Raises:
            RuntimeError: If verify=True and command didn't appear on screen
        """
        # Type command
        self.type_text(command)

        if verify:
            # Halt and check screen
            time.sleep(0.2)
            self.halt()
            time.sleep(0.1)
            line = self.screen_line(24)
            self.continue_()
            time.sleep(0.1)

            # Verify command appeared
            if command.upper() not in line.upper():
                raise RuntimeError(
                    f"Command '{command}' not visible on screen. "
                    f"Screen shows: [{line}]"
                )

        # Press Enter
        self.type_text("\r")
        time.sleep(wait_after)

    def debug_break_on_exec(self, enabled: bool = True) -> dict:
        """Enable/disable break-on-exec."""
        return self.qmp.debug_break_on_exec(enabled)
