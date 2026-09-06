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
