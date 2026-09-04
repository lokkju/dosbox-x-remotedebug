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
