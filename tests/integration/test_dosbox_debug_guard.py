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

from dosbox_debug import GDBClient, GDBError, PackedAddressError


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
    not fire on it -- it only rejects values above the real-mode ceiling.

    An unconnected client raises GDBError("Not connected") from
    _send_packet, so reaching THAT is proof the guard let the address
    through. A PackedAddressError here would be the bug.
    """
    client = GDBClient()
    with pytest.raises(GDBError, match="Not connected"):
        client.set_breakpoint(0x30000)


def test_the_seg_off_string_form_still_converts_linearly():
    """0824:5a90 converts to 0x8340, comfortably under the ceiling."""
    client = GDBClient()
    with pytest.raises(GDBError, match="Not connected"):
        client.set_breakpoint("0824:5a90")


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-v"]))
