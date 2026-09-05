# dbxdebug Stage 2 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Turn `dbxdebug` into the single library for driving DOSBox-X remote debugging — protocol clients plus session lifecycle — on modern project tooling, so `powerbasic-decompile` can delete its own launcher and depend on a released package.

**Architecture:** `dbxdebug` currently ships protocol clients and a CLI with no launcher. Stage 2 adds the lifecycle layer that `powerbasic-decompile` proved out over 38 call sites — ephemeral ports with retry, an isolated workdir per session, teardown through three independent paths, and a `/proc`-keyed registry with list/reap — then makes the clients consume the capability flags DOSBox-X Stage 1 began advertising. The tooling is brought onto the project standard first, so a large port lands on stable CI rather than being rebased over it.

**Tech Stack:** Python 3.11+, uv, ruff (check + format), pyright, pytest, hatch-vcs, release-please, GitHub Actions.

**Spec:** `docs/superpowers/specs/2026-09-03-dosbox-debug-harness-design.md` (sections 2 and 4)

## Global Constraints

- **Work happens in `~/projects/lokkju/dbxdebug`.** That repo is on branch `main` and is clean.
- **`~/projects/lokkju/powerbasic-decompile` is READ-ONLY for this entire plan.** Other agents edit that tree. Read it freely to port code; never write to it, never commit there. Stage 3 migrates it, separately and only on the user's go-ahead.
- Python 3.11+. Modern type hints: `list[str]`, `dict[str, int]`, `X | None` — never `List`, `Dict`, `Optional`.
- Package management is `uv`. Never invoke `pip`.
- Lint and format are both ruff, and they are SEPARATE GATES: `uv run ruff check src/ tests/` does NOT catch what `uv run ruff format --check src/ tests/` catches. Run both, every task. Skipping the format gate is the most common cause of green-locally/red-in-CI.
- Type checking is pyright: `uv run pyright src/`. Not mypy.
- Google-style docstrings.
- Conventional-commit messages. **NEVER** add `Co-authored-by` or any AI/agent attribution.
- `dbxdebug` is licensed Polyform Shield 1.0.0. It must never be added as a dependency of the GPLv2 `dosbox-x` repository.

## Notes for the implementer

**Verify with the same gates CI runs**, every task, before committing:
```
uv run ruff check src/ tests/
uv run ruff format --check src/ tests/
uv run pyright src/
uv run pytest
```

**Two facts about the emulator this library drives**, established by the DOSBox-X Stage 1 work and pinned by its conformance suite:
- GDB `Z0`/`z0` take a **linear** address, the same as `m`/`M`. A `seg:off` pair is `seg * 16 + off`. Builds advertising `dosbox-x-linear-bp+` in `qSupported` behave this way; older builds split the argument as a packed far pointer and silently never fire above 64 KB.
- GDB register 8 is **EIP, an offset within CS**. The linear PC is `cs * 16 + eip`. Builds advertising `dosbox-x-eip-offset+` behave this way; older ones returned a linear PC from `g` while `G` wrote an offset.

**Integration tests need a built emulator.** `~/projects/eesystem/dosbox-x/src/dosbox-x` exists and is current, built with `--enable-remotedebug`. Tests that drive it must skip cleanly when the binary is absent, so the suite still runs on a machine without it. Each such test costs roughly 2.5 s of emulator boot.

**Never run `pkill -f dosbox-x`.** It kills every other user's and agent's emulator. The library exists partly to make that unnecessary.

---

### Task 1: Replace commitizen with release-please

**Files:**
- Create: `release-please-config.json`, `.release-please-manifest.json`
- Modify: `pyproject.toml`, `.pre-commit-config.yaml`

**Interfaces:**
- Consumes: nothing.
- Produces: a release process driven by release-please. Later tasks commit with conventional-commit messages that it will read.

- [ ] **Step 1: Record the current released version**

Run: `git tag --sort=-v:refname | head -3`
Expected: `v0.2.1` is the newest. That value seeds the manifest. Seeding `0.0.0` on a package already published to PyPI would make release-please propose a version that already exists and the publish would fail.

- [ ] **Step 2: Create the release-please config**

`release-please-config.json`:
```json
{
  "$schema": "https://raw.githubusercontent.com/googleapis/release-please/main/schemas/config.json",
  "packages": {
    ".": {
      "release-type": "python",
      "package-name": "dbxdebug",
      "changelog-path": "CHANGELOG.md",
      "bump-minor-pre-major": true,
      "bump-patch-for-minor-pre-major": false,
      "include-component-in-tag": false
    }
  }
}
```

`bump-minor-pre-major` is required: the package is in 0.x and Stage 2 makes breaking changes. Without it a `feat!:` would bump to 1.0.0, which is not intended yet.

`.release-please-manifest.json`:
```json
{
  ".": "0.2.1"
}
```

- [ ] **Step 3: Remove commitizen**

In `pyproject.toml`, delete the entire `[tool.commitizen]` section, and remove `"commitizen>=3.29.0",` from the `dev` dependency group.

In `.pre-commit-config.yaml`, delete the `https://github.com/commitizen-tools/commitizen` repo block and its `commitizen` hook.

Do NOT remove `hatch-vcs` or the `[tool.hatch.version]` block — release-please creates the tag and hatch-vcs still derives the package version from it. The two are complementary; commitizen was the piece being replaced.

- [ ] **Step 4: Sync the lockfile and verify**

```bash
uv lock
uv sync --group dev
uv run ruff check src/ tests/
uv run ruff format --check src/ tests/
uv run pyright src/
uv run pytest
```
Expected: all pass. `uv lock` should show commitizen removed.

- [ ] **Step 5: Commit**

```bash
git add pyproject.toml .pre-commit-config.yaml uv.lock release-please-config.json .release-please-manifest.json
git commit -F - <<'EOF'
build: replace commitizen with release-please

release-please maintains the release PR from conventional commits and
creates the tag; hatch-vcs still derives the package version from that
tag, so the two are complementary and only commitizen is removed.

The manifest is seeded with 0.2.1, the current released version --
seeding 0.0.0 on a published package would propose a version that
already exists. bump-minor-pre-major is set because the package is in
0.x and stage 2 makes breaking changes that should not reach 1.0.0 yet.
EOF
```

---

### Task 2: Consolidate the workflows and pin the actions

Three workflows become two. A separate lint workflow is an anti-pattern in this project's standard; lint is an inline job in the test workflow. Publishing moves into the release workflow because a tag created by `GITHUB_TOKEN` does not trigger a separate tag-triggered workflow — the current `publish.yml` would simply stop firing once release-please owns tagging.

**Files:**
- Create: `.github/workflows/release.yml`, `.github/dependabot.yml`
- Modify: `.github/workflows/test.yml`
- Delete: `.github/workflows/lint.yml`, `.github/workflows/publish.yml`

**Interfaces:**
- Consumes: the release-please config from Task 1.
- Produces: `test.yml` exposing `workflow_call` so `release.yml` can reuse it.

- [ ] **Step 1: Read what exists before changing it**

```bash
cat .github/workflows/test.yml .github/workflows/lint.yml .github/workflows/publish.yml
```
Note which jobs exist, what `test.yml` already exposes, and every `uses:` line — you will re-pin all of them.

- [ ] **Step 2: Fold lint into test.yml**

`test.yml` gains a `lint` job running, in order, `uv run ruff check src/ tests/`, `uv run ruff format --check src/ tests/`, and `uv run pyright src/`. The existing test job depends on it. Add `on: workflow_call:` alongside the existing push/PR triggers so the release workflow can reuse the whole thing. Keep the build job (`uv build`).

**The format gate is not optional and is not covered by `ruff check`.** A publish gated on a pytest-only subset ships code the formatter would reject.

- [ ] **Step 3: Write release.yml**

Triggered on push to `main`. First job runs release-please. When it reports `release_created`, the SAME run must:
1. reuse the full test workflow via `uses: ./.github/workflows/test.yml` — the whole thing, not a pytest step;
2. build and publish to PyPI via OIDC, in the `pypi` environment, with `id-token: write`.

Delete `publish.yml` and `lint.yml`.

- [ ] **Step 4: SHA-pin every action**

None of the ten actions currently in use is pinned; all are floating tags in a pipeline that publishes to PyPI. Pin each to a full commit SHA with the version in a trailing comment, e.g.:
```yaml
      - uses: actions/checkout@<sha>  # v5.0.0
```
Resolve each SHA yourself with `gh api repos/<owner>/<repo>/git/refs/tags/<tag>` or the equivalent. Do not guess a SHA — a wrong one fails the workflow at run time, which is the worst place to find it.

- [ ] **Step 5: Add dependabot**

`.github/dependabot.yml` covering the `github-actions` ecosystem weekly (this is what keeps the SHA pins current) and the `uv` / `pip` ecosystem for Python dependencies.

- [ ] **Step 6: Validate the workflow syntax**

```bash
uv run pre-commit run actionlint --all-files
```
Expected: pass. actionlint is already configured in this repo's pre-commit.

- [ ] **Step 7: Commit**

```bash
git add .github/
git commit -F - <<'EOF'
ci: consolidate to test and release workflows, pin actions

Lint becomes an inline job in test.yml rather than its own workflow, and
test.yml gains workflow_call so the release can reuse the whole gate --
lint, format, types and the pytest matrix -- rather than a subset.

Publishing moves from a tag-triggered publish.yml into release.yml,
gated on release_created. A tag created by GITHUB_TOKEN does not
trigger a separate workflow, so with release-please owning tagging the
old publish.yml would silently never fire again.

All actions are SHA-pinned and dependabot is added to keep them current.
EOF
```

- [ ] **Step 8: Report the two manual steps**

These are web-UI actions the implementer CANNOT perform. State them prominently in your report so they are not lost:
1. **PyPI Trusted Publisher must be updated to workflow `release.yml`** (environment `pypi`). It pins the workflow *filename*; publishing breaks until this is changed.
2. **"Allow GitHub Actions to create and approve pull requests"** must be enabled in Settings > Actions > General, or release-please cannot open its PR.

---

### Task 3: Addressing module

The single place that knows how an address is encoded, and the home for the history that makes it non-obvious.

**Files:**
- Create: `src/dbxdebug/addressing.py`
- Test: `tests/test_addressing.py`

**Interfaces:**
- Consumes: nothing.
- Produces: `linear(seg: int, off: int) -> int`; `parse_address(addr: int | str) -> int` accepting an int or a `"seg:off"` hex string; `linear_pc(registers: Sequence[int]) -> int` computing `registers[10] * 16 + registers[8]`; the constants `CS_INDEX = 10`, `EIP_INDEX = 8`, `REAL_MODE_CEILING = 0x110000`; `PackedAddressError(ValueError)`; and `bp_addr(seg: int, off: int)` as a shim that RAISES.

- [ ] **Step 1: Write the failing test**

```python
"""The one place that knows how an address is encoded."""

import pytest

from dbxdebug.addressing import (
    CS_INDEX,
    EIP_INDEX,
    PackedAddressError,
    bp_addr,
    linear,
    linear_pc,
    parse_address,
)


def test_linear_is_seg_times_sixteen_plus_offset():
    assert linear(0x0824, 0x5A90) == 0x0824 * 16 + 0x5A90


def test_parse_address_accepts_an_int_unchanged():
    assert parse_address(0x30000) == 0x30000


def test_parse_address_accepts_a_seg_off_string():
    assert parse_address("0824:5a90") == 0x0824 * 16 + 0x5A90


def test_parse_address_rejects_a_packed_far_pointer():
    """0824:5A90 packed is 0x08245A90. Real-mode linear addresses stop just
    past 1 MB including the HMA, so nothing legitimate reaches here."""
    with pytest.raises(PackedAddressError, match="packed far pointer"):
        parse_address(0x08245A90)


def test_parse_address_allows_the_whole_real_mode_range():
    """The HMA tops out at 0xFFFF0 + 0xFFFF = 0x10FFEF, which must pass."""
    assert parse_address(0x10FFEF) == 0x10FFEF


def test_linear_pc_uses_cs_and_eip_indices():
    regs = [0] * 16
    regs[CS_INDEX] = 0x3000
    regs[EIP_INDEX] = 0x0100
    assert linear_pc(regs) == 0x30100


def test_bp_addr_raises_rather_than_packing():
    """The old helper packed (seg << 16) | off, correct against pre-fix
    builds and wrong against current ones. It must never quietly encode."""
    with pytest.raises(PackedAddressError, match="linear"):
        bp_addr(0x0824, 0x5A90)
```

- [ ] **Step 2: Run to verify it fails**

Run: `uv run pytest tests/test_addressing.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'dbxdebug.addressing'`

- [ ] **Step 3: Write the module**

Create `src/dbxdebug/addressing.py`. Its module docstring must record why this exists: `Z0`/`z0` take a linear address, the same as `m`/`M`; older builds split the argument as a packed far pointer with `seg = addr >> 16`, so any breakpoint above `0x10000` answered OK and never fired, while below `0x10000` the two readings coincide — which is why the bug looked like it worked. Include that `bp_addr` raises deliberately, because a caller holding a pre-added value was correct against old builds and is wrong against current ones, and the stub answers OK either way.

`REAL_MODE_CEILING = 0x110000`, and `parse_address` raises `PackedAddressError` at or above it. Document the residual blind spot honestly: a packed pair with a small segment (`0010:0000` arrives as `0x00100000`) is indistinguishable from a legitimate linear address and passes.

- [ ] **Step 4: Run to verify it passes, then the gates**

```bash
uv run pytest tests/test_addressing.py -v
uv run ruff check src/ tests/ && uv run ruff format --check src/ tests/ && uv run pyright src/
```
Expected: 7 passed, all gates clean.

- [ ] **Step 5: Commit**

```bash
git add src/dbxdebug/addressing.py tests/test_addressing.py
git commit -m "feat(addressing): add the linear addressing module"
```

---

### Task 4: Capability handshake in GDBClient

**Files:**
- Modify: `src/dbxdebug/gdb.py`
- Test: `tests/test_gdb_capabilities.py`

**Interfaces:**
- Consumes: `addressing.parse_address`.
- Produces: `GDBClient.capabilities -> set[str]` populated at connect; `GDBClient.require_linear_breakpoints()` raising `IncompatibleStubError` when `dosbox-x-linear-bp+` is absent; `IncompatibleStubError(RuntimeError)`; a constructor keyword `require_capabilities: bool = True`.

- [ ] **Step 1: Write the failing test**

Use a fake socket that replays a `qSupported` reply; no emulator needed. Cover: capabilities parsed from a reply containing both vendor features; `require_linear_breakpoints()` passing when present; raising with a message naming the missing feature when absent; and `require_capabilities=False` suppressing the check.

The reply to replay is exactly:
`PacketSize=3fff;swbreak+;hwbreak+;vContSupported+;QStartNoAckMode+;dosbox-x-linear-bp+;dosbox-x-eip-offset+`

- [ ] **Step 2: Run to verify it fails**

- [ ] **Step 3: Implement**

`GDBClient` sends `qSupported:multiprocess+` on connect and parses the semicolon-separated reply into `self.capabilities`. When `require_capabilities` is true (the default) and `dosbox-x-linear-bp+` is missing, raise `IncompatibleStubError` explaining that this build splits `Z0` as a packed far pointer so breakpoints above 64 KB will answer OK and never fire, and that passing `require_capabilities=False` proceeds anyway.

Route `set_breakpoint`/`remove_breakpoint`/`read_memory`/`write_memory` addresses through `addressing.parse_address`, so a packed value raises rather than being sent.

**This is the change that makes the flags load-bearing.** Until now DOSBox-X advertises them and nothing reads them.

- [ ] **Step 4: Verify and run the gates**

- [ ] **Step 5: Commit**

```bash
git commit -m "feat(gdb)!: require dosbox-x-linear-bp+ at connect

BREAKING CHANGE: GDBClient now refuses to connect to a stub that does
not advertise dosbox-x-linear-bp+. Such builds split the Z0 argument as
a packed far pointer, so any breakpoint above 64 KB answers OK and
never fires -- silently. Pass require_capabilities=False to proceed
against an old build deliberately."
```

---

### Task 5: Register semantics in GDBClient

**Files:**
- Modify: `src/dbxdebug/gdb.py`
- Test: `tests/test_gdb_registers.py`

**Interfaces:**
- Consumes: `addressing.linear_pc`, `CS_INDEX`, `EIP_INDEX`.
- Produces: `GDBClient.read_registers() -> dict[str, int]` keeping its existing shape and key names, plus `GDBClient.read_register_list() -> list[int]` returning the raw 16 and `GDBClient.linear_pc() -> int`. `write_register(index: int, value: int) -> bool` sends `P`.

- [ ] **Step 1: Write the failing test**

Against a fake socket: `read_register_list` decodes 16 little-endian words; `read_registers`'s `eip` key holds the OFFSET, not a linear address; `linear_pc()` returns `cs * 16 + eip`; `write_register` frames `P<n>=<little-endian hex>`.

- [ ] **Step 2: Run to verify it fails**

- [ ] **Step 3: Implement**

Add the two new methods and `write_register`. `read_registers`'s `eip` entry must be documented as an offset — it changed meaning when the stub was fixed, and code written against an older build that used it as a linear address is now silently wrong.

- [ ] **Step 4: Verify and run the gates**

- [ ] **Step 5: Commit** with a `feat(gdb)!:` message whose BREAKING CHANGE footer states that `read_registers()["eip"]` is now an offset within CS and that the linear PC comes from `linear_pc()`.

---

### Task 6: The six unwrapped QMP commands

`qmp.cpp` dispatches thirteen commands; `QMPClient` wraps roughly half.

**Files:**
- Modify: `src/dbxdebug/qmp.py`
- Test: `tests/test_qmp_commands.py`

**Interfaces:**
- Consumes: nothing.
- Produces: `QMPClient.memdump(address: int, size: int, file: str | None = None) -> bytes | str`; `.screendump(file: str | None = None) -> dict`; `.savestate(file: str) -> dict`; `.loadstate(file: str) -> dict`; `.system_reset(dos_only: bool = False) -> dict`; `.quit() -> None`; `.query_status() -> dict`; `.stop() -> dict`; `.cont() -> dict`; `.debug_break_on_exec(enabled: bool) -> dict`.

- [ ] **Step 1: Write the failing test**

Against a fake socket, assert the exact JSON each method sends and how it decodes the reply. `memdump` with no `file` returns base64 decoded to `bytes`; with a `file` it returns the path. `query_status` exposes both the flat `running` boolean and the nested `debug` object with `active`/`paused`/`reason`.

- [ ] **Step 2: Run to verify it fails**

- [ ] **Step 3: Implement**

Each method sends `{"execute": ..., "arguments": ...}`, omitting `arguments` entirely when there are none.

`memdump`'s docstring must record two things: it is the bulk-read path — one call replaces thousands of GDB `m` round-trips, and a recorded segment scan cost 7,168 of them — and it REQUIRES the CPU stopped, via a GDB halt, the interactive debugger, or a QMP `stop`. It refuses otherwise, because it reads guest memory directly and would race the emulation thread.

`quit`'s docstring should note it is dispatched but NOT advertised by `query-commands`.

- [ ] **Step 4: Verify and run the gates**

- [ ] **Step 5: Commit**

```bash
git commit -m "feat(qmp): wrap memdump, screendump, savestate, loadstate, system_reset and quit"
```

---

### Task 7: Session registry

Split out of `powerbasic-decompile`'s `tools/dosbox/session.py`, lines 164-478 and 523-607. **Read that file; do not modify it.**

**Files:**
- Create: `src/dbxdebug/registry.py`
- Test: `tests/test_registry.py`

**Interfaces:**
- Consumes: nothing.
- Produces: `RegisteredSession` (properties `pid`, `pgid`, `owner_pid`, `workdir`, `gdb_port`, `qmp_port`, `started_at`, `age_s`, `alive`, `owner_alive`, `orphaned`); `registry_dir(registry: Path | None = None) -> Path`; `list_sessions(registry=None) -> list[RegisteredSession]`; `reap(registry=None, all_sessions=False, ...) -> list`; `kill_group(pgid: int, term_timeout: float = 5.0, ...)`; `format_table(sessions) -> str`; `free_port(host="127.0.0.1") -> int`; `port_is_listening(port, host="127.0.0.1", timeout=0.3) -> bool`; `wait_ports_free(ports: Iterable[int], timeout: float, host="127.0.0.1") -> bool`, which blocks until nothing is listening on any of them — the pre-flight that stops a foreign emulator being mistaken for one of ours.

- [ ] **Step 1: Read the source**

```bash
sed -n '164,478p' ~/projects/lokkju/powerbasic-decompile/tools/dosbox/session.py
sed -n '523,607p' ~/projects/lokkju/powerbasic-decompile/tools/dosbox/session.py
```
Note especially `_proc_starttime` and `_pid_alive`. **The registry is keyed on pid PLUS `/proc` starttime**, so a recycled pid cannot be mistaken for a live session. That is the point of the design; do not simplify it away.

- [ ] **Step 2: Write the failing test**

No emulator needed. Cover: `free_port` returns a bindable port and two calls differ; `port_is_listening` is false for a closed port and true for one you bind in the test; a registry entry written and read back round-trips its fields; an entry naming a pid that does not exist reports `alive` false; `format_table` renders without raising on both an empty list and a populated one; `wait_ports_free` returns immediately for a port nothing holds, and times out for one the test itself binds.

- [ ] **Step 3: Run to verify it fails**

- [ ] **Step 4: Port the code**

Convert `Optional[X]` to `X | None` and `List`/`Dict` to `list`/`dict` — the source predates this project's type-hint standard. Registry location comes from a `DBXDEBUG_REGISTRY` environment variable, defaulting to `~/.cache/dbxdebug-sessions`; the source used a project-specific name that must not carry over.

- [ ] **Step 5: Verify and run the gates**

- [ ] **Step 6: Commit**

```bash
git commit -m "feat(registry): add the session registry with list and reap"
```

---

### Task 8: DosboxSession

The lifecycle layer. Ported from `powerbasic-decompile`'s `tools/dosbox/session.py` lines 609-1101. **Read that file; do not modify it.**

**Files:**
- Create: `src/dbxdebug/session.py`
- Test: `tests/test_session.py`

**Interfaces:**
- Consumes: `registry.*`, `addressing.*`, `GDBClient`, `QMPClient`.
- Produces: `DosboxSession` as a context manager with `start()`, `stop()`, `running`, `screen_lines()`, `set_breakpoint(seg, off)`, `remove_breakpoint(seg, off)`, `wait_for_text(want, timeout=60.0, poll=0.5)`, `assert_screen_readable()`, and attributes `gdb`, `qmp`, `gdb_port`, `qmp_port`, `pid`, `workdir`, `conf_path`. Plus `DosboxLaunchError(RuntimeError)`.

- [ ] **Step 1: Read the source and its docstrings**

The dataclass docstrings in the source explain WHY each default is what it is — particularly `boot_settle`, whose comment records that converting callers dropped it and they then captured 24 frames of the DOSBox-X welcome banner and passed their own content gate. **Carry those explanations across.** They are the most valuable part of the file.

- [ ] **Step 2: Write the failing test**

Split by whether an emulator is needed. Without one: ports are allocated dynamically and are never 2159/4444; the rendered conf contains `gdbserver port` and `qmpserver port` with spaces and contains neither `gdbport` nor `qmpport`; `render_conf` substitutes known keys and leaves unknown braces alone. With one (skipped when `~/projects/eesystem/dosbox-x/src/dosbox-x` is absent): a session starts, both clients connect, `stop()` is idempotent, and afterwards the process is gone and the workdir removed.

- [ ] **Step 3: Run to verify it fails**

- [ ] **Step 4: Port the code**

Preserve every behaviour: ephemeral ports with retry on a lost bind race; a private workdir per session with `files=` staging; teardown via `__exit__`, `atexit` AND signal handlers converging on an idempotent `stop()` that does `killpg` SIGTERM then SIGKILL, and `shutil.rmtree` from Python — **never a shell `rm`**, because sandboxes refuse it; registration keyed on pid + starttime; `boot_settle` and `wait_for_text` so readiness is observed rather than assumed.

**Three changes from the source, all required:**
1. `_import_clients()` and its `sys.path` manipulation go away entirely. Import `GDBClient` and `QMPClient` from this package directly.
2. **Reconcile the client API.** The source was written against `dosbox_debug`'s clients, whose method names differ from this package's: `continue_()` here is `continue_execution()`, and `read_registers()` returns a `dict[str, int]` rather than a dataclass. Read both and fix every call site. `screen_lines()` used the old client's `screen_dump()`; implement it against `read_memory` at linear `0xB8000` (two bytes per cell, character then attribute) or this package's existing video helpers.
3. **`set_breakpoint(seg, off)` computes a LINEAR address** via `addressing.linear`, not the packed form the source used. This is the breaking change; get it right, and say so in the docstring.

Add `assert_screen_readable()`: raise when the screen is entirely blank or still shows the DOSBox-X banner. That failure belongs to the emulator's readiness, not to any consumer, and it is why two downstream capture scripts once shipped 24 frames of banner.

- [ ] **Step 5: Verify and run the gates**

- [ ] **Step 6: Commit** with a `feat(session)!:` message whose BREAKING CHANGE footer records that `set_breakpoint` now takes a linear address.

---

### Task 9: CLI — session management and doctor

**Files:**
- Modify: `src/dbxdebug/cli.py`
- Create: `src/dbxdebug/doctor.py`
- Test: `tests/test_cli_session.py`

**Interfaces:**
- Consumes: `registry.*`, `session.DosboxSession`.
- Produces: `dbxdebug session list`, `dbxdebug session reap`, `dbxdebug doctor`.

- [ ] **Step 1: Read the existing CLI** to match its structure and its framework (typer or click — follow what is there rather than introducing a second one).

- [ ] **Step 2: Write the failing test** using the CLI runner: `session list` on an empty registry prints a header and no rows and exits 0; `session reap --help` works; `doctor` runs and exits 0 without an emulator.

- [ ] **Step 3: Implement**

`session list` renders `format_table(list_sessions())`. `session reap` kills orphans and removes their workdirs, printing what it removed. `doctor` reports: whether a `dosbox-x` binary is findable and whether it has remote debugging compiled in; the host's CPU count as a rough concurrency ceiling; whether the registry directory is writable; and any orphaned sessions. Port what is useful from `powerbasic-decompile`'s `tools/dosbox/probe_concurrency.py` — **read only** — but do not carry over its project-specific measurement harness.

- [ ] **Step 4: Verify and run the gates**

- [ ] **Step 5: Commit**

```bash
git commit -m "feat(cli): add session list, session reap and doctor"
```

---

### Task 10: Real-mode stack frame walking

Generic 16-bit x86, not specific to any consumer. Ported from
`powerbasic-decompile`'s `tools/dosbox-runner/probe_frame_unwind.py` and
`probe_cleanup_walker.py`. **Read those files; do not modify them.**

**Files:**
- Create: `src/dbxdebug/frames.py`
- Test: `tests/test_frames.py`

**Interfaces:**
- Consumes: `GDBClient`, `addressing.linear`.
- Produces: `walk_frames(gdb, max_depth: int = 32) -> list[Frame]` following the
  BP chain from the current frame outward, and `steps_out(gdb, timeout: float = 10.0) -> str`,
  which single-steps until the current frame returns. `Frame` is a dataclass
  carrying `bp`, `return_seg`, `return_off` and `depth`.

- [ ] **Step 1: Read the source**

```bash
grep -n 'def walk_frames' -A40 ~/projects/lokkju/powerbasic-decompile/tools/dosbox-runner/probe_frame_unwind.py
grep -n 'def steps_out' -A30 ~/projects/lokkju/powerbasic-decompile/tools/dosbox-runner/probe_cleanup_walker.py
```

NOTE: both source files assert that a breakpoint address is a linear
`cs * 16 + off`, and both were written when that happened to work because
their addresses stayed under `0x10000`. That reasoning does not generalise and
must NOT be carried over — this package computes linear addresses through
`addressing.linear` and the stub takes them directly.

- [ ] **Step 2: Write the failing test**

Frame walking is pure arithmetic over memory reads, so test it against a fake
GDB client that returns a hand-built stack: a three-deep chain of saved BP
values terminating in zero, with known return addresses. Assert the walk finds
exactly three frames in order and stops at the terminator rather than running
to `max_depth`. Add a test that a cyclic BP chain (a frame pointing at itself)
terminates rather than looping forever.

- [ ] **Step 3: Run to verify it fails**

- [ ] **Step 4: Implement**

A real-mode frame is `[BP] = caller's BP`, `[BP+2] = return offset`, and for a
far call `[BP+4] = return segment`. Read through `SS`. Stop on a zero or
decreasing BP, on a read failure, on a repeat (cycle), or at `max_depth` —
whichever comes first. Never loop unbounded on guest data you do not control.

- [ ] **Step 5: Verify and run the gates**

- [ ] **Step 6: Commit**

```bash
git commit -m "feat(frames): add real-mode stack frame walking"
```

---

### Task 11: Integration tests against a real emulator

Unit tests use fakes and prove framing. This task proves the library actually drives DOSBox-X.

**Files:**
- Create: `tests/integration/test_live_session.py`, `tests/integration/conftest.py`

**Interfaces:**
- Consumes: everything above.
- Produces: nothing.

- [ ] **Step 1: Write the tests**

A fixture locating the emulator at `~/projects/eesystem/dosbox-x/src/dosbox-x`, overridable by a `DBXDEBUG_DOSBOX` environment variable, skipping the whole module when absent so the suite still runs without it.

Cover, each in its own session: a session starts and both clients connect; `capabilities` contains both vendor features; a breakpoint set from `seg, off` above 64 KB fires and the stopped `linear_pc()` matches; `memdump` while halted agrees byte-for-byte with `read_memory` over the same range; `memdump` refuses while running; `wait_for_text` returns an observed time rather than a timeout; and after the context manager exits, the process is gone and the workdir removed.

- [ ] **Step 2: Run them**

```bash
uv run pytest tests/integration -v
```
Expected: pass, or skip cleanly with no emulator. Afterwards confirm `pgrep -af 'src/dosbox-x'` shows nothing of yours.

- [ ] **Step 3: Wire into CI**

Integration tests must NOT run in GitHub Actions — there is no emulator there. Mark them so the default `uv run pytest` in CI skips them, and document how to run them locally.

- [ ] **Step 4: Commit**

```bash
git commit -m "test: add live integration tests against a built dosbox-x"
```

---

### Task 12: Documentation and the release PR

**Files:**
- Modify: `README.md`
- Create: `docs/migrating-from-tools-dosbox.md`

- [ ] **Step 1: Update the README** with the session lifecycle as the primary entry point, the two capability flags and what they mean, and the addressing rule stated once and plainly.

- [ ] **Step 2: Write the migration note** for consumers of `powerbasic-decompile`'s `tools/dosbox/session.py`: the import change, the client method-name differences, and — most importantly — that `set_breakpoint` now takes a linear address where the old `bp_addr` packed one, that `bp_addr` raises rather than encoding, and that `read_registers()["eip"]` is now an offset. This document is what Stage 3 follows.

- [ ] **Step 3: Verify all gates one final time**

```bash
uv run ruff check src/ tests/ && uv run ruff format --check src/ tests/ && uv run pyright src/ && uv run pytest
```

- [ ] **Step 4: Commit**

```bash
git commit -m "docs: document the session lifecycle and the migration path"
```

- [ ] **Step 5: Report what the user must do**

Restate the two web-UI steps from Task 2, and note that pushing `main` opens a release-please PR whose merge tags the version and publishes.

---

## Deliberately not in Stage 2

The spec's section 4.4 also lists the **batch fan-out shell** — running N
programs across M concurrent sessions, collecting artifacts and respecting host
capacity. It is not in this plan. The shape of a good concurrency API depends on
what `dbxdebug doctor` (Task 9) actually measures about a host's usable
parallelism, and guessing at it before that data exists would produce an
interface built on assumption. It is a natural first task for a later stage,
once `doctor` has run on real hardware.

Also not in Stage 2: `powerbasic-decompile`'s `assert_meaningful` /
`VacuousComparison` measurement gates, which stay in that project. They are
measurement discipline coupled to how it builds paired corpora, not emulator
mechanics. The narrow emulator-specific piece — rejecting an all-blank or
still-at-the-banner capture — moves as `session.assert_screen_readable()` in
Task 8.

## Stage 2 exit criteria

- `uv run ruff check`, `uv run ruff format --check`, `uv run pyright src/` and `uv run pytest` all pass.
- `GDBClient` refuses a stub lacking `dosbox-x-linear-bp+` unless the caller opts out.
- `set_breakpoint(seg, off)` produces a linear address; `bp_addr` raises.
- `read_registers()["eip"]` is an offset, and `linear_pc()` gives the linear PC.
- All thirteen dispatched QMP commands have a client method.
- A session starts on ephemeral ports, is registered, and leaves no process or workdir behind.
- `dbxdebug session list`, `session reap` and `doctor` work.
- `walk_frames` terminates on a cyclic BP chain rather than looping.
- release-please config and manifest exist, the manifest reads `0.2.1`, commitizen is gone, and exactly two workflows exist with every action SHA-pinned.
- No file under `~/projects/lokkju/powerbasic-decompile` has been modified.
