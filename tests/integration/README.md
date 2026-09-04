# Integration Tests for Remote Debugging

This directory contains integration tests for DOSBox-X's remote debugging capabilities (GDB server and QMP server).

## Requirements

- **Python 3.11+**
- **[uv](https://github.com/astral-sh/uv)** - Fast Python package manager
- DOSBox-X built with `--enable-remotedebug`

Dependencies are managed via [PEP 723](https://peps.python.org/pep-0723/) inline script metadata - no separate requirements.txt needed.

This suite depends on nothing beyond the Python standard library and
pytest. That is deliberate, not an oversight: a nicer client library
(`dbxdebug`) exists for automation work, but it is licensed under Polyform
Shield 1.0.0, and this repository is GPLv2. GPLv2 code cannot depend on a
Polyform Shield package, even a test-only one, so `protocol/gdb.py` and
`protocol/qmp.py` in this directory implement just enough of the GDB
remote serial protocol and QMP to assert what the servers do, with none of
the ergonomics (register dataclasses, address parsing, screen-reading
helpers) that make a client pleasant to use day to day. See
`docs/superpowers/specs/2026-09-03-dosbox-debug-harness-design.md` for the
full reasoning, and reach for `dbxdebug` itself (a separate project) if
you want a nicer client for scripting or exploratory debugging.

## Quick Start

1. Build DOSBox-X with remote debugging:
   ```bash
   ./build-debug --enable-remotedebug
   ```

2. Run all tests:
   ```bash
   uv run tests/integration/run_all.py
   ```

That's it. The suite does not need a manually started emulator listening
on fixed ports: every test starts its own DOSBox-X instance on dynamically
allocated ports (see `launcher.py`) and tears it down when the test ends.

## Running Individual Test Suites

```bash
# GDB and QMP conformance tests
uv run tests/integration/test_gdb_conformance.py
uv run tests/integration/test_qmp_conformance.py

# Video memory tests
uv run tests/integration/test_video_tools.py

# DEBUGBOX integration tests
uv run tests/integration/test_debugbox.py
```

## Pytest Options

Pass pytest arguments after the script:

```bash
# Verbose output
uv run tests/integration/run_all.py -v

# Stop on first failure
uv run tests/integration/run_all.py -x

# Run only tests matching pattern
uv run tests/integration/run_all.py -k "memory"
uv run tests/integration/run_all.py -k "breakpoint"

# Full tracebacks
uv run tests/integration/run_all.py --tb=long

# Show test durations
uv run tests/integration/run_all.py --durations=10
```

## Test Files

| File | Description |
|------|-------------|
| `protocol/gdb.py`, `protocol/qmp.py` | Standard-library-only clients that assert protocol behavior. Not for reuse outside this test suite. |
| `launcher.py` | Starts one DOSBox-X per test on dynamic ports and guarantees it is torn down. |
| `conftest.py` | The `emulator`/`gdb`/`qmp` fixtures every test file builds on. |
| `test_protocol_gdb_unit.py`, `test_protocol_qmp_unit.py` | Unit tests for the protocol clients themselves, no emulator required. |
| `test_launcher.py` | Unit tests for `launcher.py`. |
| `test_config.py` | Pins the `gdbserver port` / `qmpserver port` config option names. |
| `test_gdb_conformance.py` | GDB Remote Serial Protocol conformance: registers, the `P` packet, linear breakpoints, `qSupported`, single-step, `p`. |
| `test_qmp_conformance.py` | QEMU Monitor Protocol conformance: `query-status`, `memdump` and its stop-guard, the dispatch surface, save/load, send-key/input-send-event. |
| `test_video_tools.py` | Text-mode video memory reads (`m` packet against `0xB8000`). |
| `test_debugbox.py` | DEBUGBOX + remote debugging integration tests: pause states, entry point detection. |
| `run_all.py` | Test runner: builds pytest args and runs the whole directory. |

## Configuration

Each test allocates its own GDB and QMP ports at random; there are no
fixed ports to configure. `launcher.Emulator` writes a temporary config
file per test and cleans it up afterward.

## Test Categories

### GDB Conformance Tests
- **Registers**: bulk (`g`) and single (`p`) reads, the `P` write packet, EIP-as-offset semantics
- **Memory**: arbitrary sizes, zero-length and unaligned reads
- **Breakpoints**: linear addressing above 64K, independent multi-breakpoint management
- **Execution**: single-step, continue-to-breakpoint
- **qSupported**: feature advertisement, including the DOSBox-X-specific linear-breakpoint and EIP-offset extensions

### QMP Conformance Tests
- **Pause/status**: `query-status`, the debug-halted drain, `stop`/`cont`
- **Memory**: `memdump`, including its requirement that the CPU be stopped first
- **Input**: `send-key`, `input-send-event`, multi-key and error cases
- **Dispatch**: `query-commands`, save/load state, `screendump`, `system_reset`, `debug-break-on-exec`

### Video Tools Tests
- Raw video memory reads and writes at `0xB8000`
- Character/attribute byte decoding

### DEBUGBOX Integration Tests
- **Basic**: DEBUGBOX command pauses emulator
- **GDB integration**: GDB can connect during debug mode
- **Pause states**: query-status, stop/cont commands
- **Entry point**: Program breaks at entry (requires the test COM file on a mounted drive)

## Troubleshooting

**"no emulator at .../src/dosbox-x"**
- Build it first: `./build-debug --enable-remotedebug`

**Import errors**
- Run with `uv run` so PEP 723 metadata resolves `pytest` automatically.
- This suite intentionally has no other dependencies to install.

**Tests hang**
- A test's emulator may be waiting on input it never received; check that
  nothing else on the machine is holding the ports `launcher.py` picked
  (unlikely, since they are allocated fresh per test).

**DEBUGBOX entry point tests skip**
- These tests require the `assets/` directory to be readable from within
  DOSBox-X. `test_debugbox.py` mounts it as drive `T:` itself via
  `launcher.Emulator(mounts=...)`; no manual mount configuration is
  needed.
- The test COM file (`DBXTEST.COM`) is created automatically in
  `tests/integration/assets/`.
