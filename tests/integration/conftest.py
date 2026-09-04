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
