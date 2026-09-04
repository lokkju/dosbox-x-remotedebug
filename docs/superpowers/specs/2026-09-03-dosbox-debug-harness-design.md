# DOSBox-X Remote Debug Harness — Design

Date: 2026-09-03
Status: approved, ready for implementation planning
Repos in scope: `dosbox-x` (this repo), `dbxdebug`, `powerbasic-decompile`

## 1. Why

The Python debug harness in `tests/integration/dosbox_debug.py` has been
forked twice and has a silent correctness bug. This design fixes the bug at
its source, splits the harness into two layers with a defensible boundary,
and ships the operating knowledge as skills so it stops being rediscovered.

Evidence comes from reviewing the DOSBox-X usage in
`powerbasic-decompile`'s Claude session logs: one 21k-line session plus 210
subagent transcripts, 1,757 DOSBox-related tool calls. Every claim below was
re-verified against this repo's source at HEAD.

### 1.1 What the review found

**F1 — `Z0` and `m` disagree about what an address is.**
`GDBServer::handle_breakpoint` parses the `Z0` address and passes it to
`DEBUG_SetBreakpoint`, which splits it as a far pointer via
`FP_SEG(x) = x >> 16`. `handle_read_memory` calls
`DEBUG_ReadMemory(address + i)`, which is `mem_readb_checked`
— genuinely linear. (Fixed in Stage 1; see §3.1 item 1 below.
`DEBUG_SetBreakpoint` now takes a linear address too, and the `FP_SEG`/
`FP_OFF` split no longer exists.) The stub is internally inconsistent and non-conformant
with the GDB remote serial protocol: real `gdb` setting a breakpoint above
`0x10000` gets `OK` and never stops. `dosbox_debug.py` uses
`(seg << 4) + off` for both, so its breakpoints are wrong above 64 KB, where
they fail *silently*.

**F2 — `DOSBoxInstance` is hostile to concurrency.** `_kill_existing()` runs
`pkill -9 -f dosbox-x`, killing every other emulator on the box. Fixed ports
2159/4444, no workdir isolation, no record of what it started, teardown only
via `__exit__`. Measured downstream cost in one week: ten orphaned emulators
running 80 minutes (load average 67-79, an unrelated suite going 2.5 min to
24 min); a QMP bind race losing 1 trial in 27.

**F3 — `QMPClient` wraps about half the server.** `QMPServer::process_command`
(`qmp.cpp:401`) dispatches
`memdump`, `screendump`, `savestate`, `loadstate`, `system_reset` and `quit`;
the Python client exposes none of them. `memdump` returns base64 up to 16 MB
and is the answer to a downstream segment scan that cost 7,168 GDB
round-trips.

**F4 — The config option names are a trap.** They are `gdbserver port` and
`qmpserver port`, with a space (`dosbox.cpp:1734`, `:1740`). `gdbport` /
`qmpport` are not options; they are ignored, the server falls back to
2159/4444, and the client connects to a *different agent's emulator*. This
happened and produced `QMPError: Failed to receive valid JSON: b''`.

**F5 — Readiness is measured wrong.** `_wait_for_servers()` waits for the
ports to accept, which happens well before DOS reaches a prompt. Keys typed
in that window are lost and the capture runs against the DOSBox-X welcome
banner — which is text, so a "screen is not blank" gate passes it. Two
downstream capture scripts shipped 24 frames of banner.

**F6 — Pending QMP work is not serviced while GDB-halted.**
`DEBUG_CheckGDBStep()` returns true while `gdb_cpu_paused`, and `Normal_Loop`
returned before reaching the drains further down its loop body. So
`SAVESTATE_CheckPendingRequest()`, `EMULATOR_CheckPendingControl()` and
`QMP_ProcessPendingInputEvents()` never ran while stopped at a breakpoint.
`savestate` at a breakpoint waited 30 s and errored; keys queued at a
breakpoint were never delivered. (Fixed in Stage 1; see §3.1 item 3 below --
`Normal_Loop` (`dosbox.cpp:469`) now calls all three drains inside the
`DEBUG_CheckGDBStep()` branch before returning.)

**F7 — `memdump` races the emulation thread.** It is the only QMP handler
that touches guest state from the socket thread. `savestate`/`loadstate`,
`stop`/`cont`/`system_reset`, `screendump` and input events all defer to the
emulation thread and poll for a result. `memdump` calls
`DEBUG_SaveMemoryBin` directly.

**F8 — Three implementations, no shared home.** `dosbox_debug.py` (931 lines,
protocol plus a weak launcher); `dbxdebug` (3,013 lines, protocol and CLI, no
launcher); `powerbasic-decompile/tools/dosbox/session.py` (46 KB, launcher
only, importing this repo's clients). 38 files there run on `DosboxSession`;
8 still hardcode ports.

## 2. The seam

Three layers, dependencies pointing one way:

| Layer | Repo | License | May depend on |
| --- | --- | --- | --- |
| Servers + conformance suite | `dosbox-x` | GPLv2 | stdlib, pytest |
| Library | `dbxdebug` | Polyform Shield 1.0.0 | stdlib, `click`, `loguru` |
| Domain | `powerbasic-decompile` | — | `dbxdebug` |

**The in-repo Python layer must stay dependency-free.** `dbxdebug` is
Polyform Shield, which is neither OSI nor GPL-compatible; a GPLv2 project
cannot take it as a test dependency. This is not a stylistic preference and
it is what forces the boundary.

`dbxdebug` already ships `click` and `loguru`. `click` is CLI-only and the
library import path should stay free of it, so that `import dbxdebug` in a
capture script pulls in nothing it does not use.

**The in-repo layer's job changes.** It is no longer a convenience library —
that role is what invited two forks. Its job is to *prove the servers do what
they claim*. That reframing keeps it small, keeps the upstream diff
defensible, and stops it duplicating `dbxdebug`.

## 3. Stage 1 — `dosbox-x`

### 3.1 C++ changes

1. **Linear breakpoint addresses.** `DEBUG_SetBreakpoint` /
   `DEBUG_RemoveBreakpoint` (`debug.cpp:6267-6275`) take a linear address,
   matching `DEBUG_ReadMemory`. Implement as `AddBreakpoint(0, linear, false)`:
   `GetAddress(0, off)` returns `(0 << 4) + off` in real mode
   (`debug.cpp:450`), so the stored `location` is exactly `linear`, and
   `CheckBreakpoint` already compares against the true physical
   `SegPhys(cs) + eip`. Delete the `FP_SEG` / `FP_OFF` macros.
   `gdbserver.cpp` is the only caller of either function and the macros are
   used nowhere else, so this is six lines with one call site each.

2. **Capability handshake.** `handle_query` (`gdbserver.cpp:479`) appends a
   vendor feature: `…;QStartNoAckMode+;dosbox-x-linear-bp+`. Real `gdb`
   ignores unknown `qSupported` features, so this stays RSP-legal. It lets a
   client refuse a pre-fix stub loudly instead of silently missing every
   breakpoint above 64 KB.

3. **Service pending QMP work while GDB-halted (F6).** Drain
   `SAVESTATE_CheckPendingRequest()`, `EMULATOR_CheckPendingControl()` and
   `QMP_ProcessPendingInputEvents()` inside the `DEBUG_CheckGDBStep()`
   `return true` branch in `Normal_Loop`, immediately before `return 0`:

   ```c
   if (DEBUG_CheckGDBStep()) {
       // Halted for GDB, or a step just completed. The drains below are
       // unreachable on this path, so service pending QMP work here.
       SAVESTATE_CheckPendingRequest();
       EMULATOR_CheckPendingControl();
       QMP_ProcessPendingInputEvents();
       return 0;
   }
   ```

   Decided against hoisting `dosbox.cpp:482-486` above the
   `DEBUG_CheckGDBStep()` call. Hoisting changes ordering on *every*
   iteration of the emulation hot loop -- pending QMP control would be
   handled before the GDB step check on every instruction batch -- trading a
   behavior change in the running path for a fix in the halted path. The
   branch version leaves the running path bit-identical to today and adds
   behavior only where there is none, which is a far easier four lines to
   defend in review. It also drains after a completed step, which is wanted.
   Latency is fine: while halted `Normal_Loop` returns immediately and is
   re-entered, so the drain runs at spin frequency rather than on a poll
   interval.

4. **Close the `memdump` race (F7), minimally.** Guard `handle_memdump`: when
   `DEBUG_IsCpuPausedForDebug()` **or** `EMULATOR_IsPaused()` is true the
   guest is not executing and memory is quiescent, so read directly;
   otherwise refuse. The two flags are disjoint and both leave memory
   quiescent: `DEBUG_IsCpuPausedForDebug()` covers the interactive debugger
   and a GDB halt, `EMULATOR_IsPaused()` covers a QMP `stop`, which parks the
   emulation thread in `PauseDOSBoxLoop`. This is slightly wider than
   originally scoped here (which named only the GDB/debugger flag), because
   a QMP `stop` quiesces the guest just as completely and gates the running
   guest case at essentially no extra cost. Dumping at a breakpoint — the
   common case — keeps zero added latency. The refusal path returns an
   error rather than deferring to the emulation thread; no request/poll
   idiom is implemented for `memdump`.

   Explicitly deferred: a condition-variable path for high-rate dumps of a
   *running* guest. The idiom's 50–100 ms sleep-poll would defeat the 30–60 Hz
   use case, but that use case is rare enough to wait for a measured need.

### 3.2 The conformance suite

`tests/integration/` is rewritten around a different purpose:

- `protocol/gdb.py`, `protocol/qmp.py` — minimal raw clients, stdlib only, no
  ergonomics. Enough to send a packet and assert the reply.
- `launcher.py` — single-instance spawner for CI: dynamic ports, `killpg`
  teardown, **no `pkill`**. Roughly 120 lines. Not a session manager.
- `test_gdb_conformance.py` — one assertion per advertised packet
  (`qSupported`, `QStartNoAckMode`, `?`, `H`, `p`, `g`, `G`, `m`, `M`, `Z`,
  `z`, `s`, `c`, `vCont`, `D`), plus the address-semantics contract: a
  breakpoint set at linear `L` above `0x10000` fires at `L`, and `m L` reads
  the byte it is on. This is the test that would have caught F1.
- `test_qmp_conformance.py` — one assertion per entry in the `qmp.cpp:401`
  dispatch, including error shapes, plus a regression test for F6 (savestate
  and queued keys complete while GDB-halted).
- `test_config.py` — a conf written with `gdbserver port = N` results in a
  listener on `N` and not on 2159. This is F4 turned into a test.
- Existing behavioral tests (`test_debugbox.py`, `test_video_tools.py`) stay,
  rebased onto `launcher.py`.
- `test_gdb_server.py` and `test_qmp_server.py` are deleted. They are the
  protocol suites the conformance tests replace; they connect to the hardcoded
  2159/4444 and therefore `pytest.skip` all fifty of their tests on every run,
  contributing no coverage while looking like fifty tests. Any behaviour of
  theirs the conformance suite does not already assert is ported before they go.

### 3.4 `dosbox_debug.py` survives Stage 1

`dosbox_debug.py` is **not** deleted in Stage 1. `powerbasic-decompile`'s
`session.py` imports `GDBClient` / `QMPClient` from it by path
(`_import_clients()`), so deleting it before `dbxdebug` ships the replacement
breaks pb for the whole gap. It is deleted at the end of Stage 2, once
`dbxdebug` is released and pb has somewhere to go.

Two changes it does get in Stage 1:

- **A deprecation notice** naming `dbxdebug` as the replacement and this spec
  as the reason.
- **A packed-far-pointer guard** in `set_breakpoint` / `remove_breakpoint`:
  raise on any address `>= 0x110000`. Real-mode linear addresses stop just
  past 1 MB including the HMA, so nothing legitimate lands there, whereas a
  packed `0x0824:5A90` arrives as `0x08245A90`.

The guard exists for pb specifically. Its `bp_addr` packs `(seg << 16) | off`,
which is correct against today's stub and *wrong* the moment Stage 1 lands —
and pb reaches the stub through this client, which has no `qSupported`
handshake. Without the guard pb's breakpoints would go quietly dead on the
next rebuild, which is the exact failure mode this whole design exists to
remove.

Note the inverse, which needs no action: `dosbox_debug.py`'s own
`(seg << 4) + off` conversion *is* the linear address, so its breakpoints
become correct for the first time when Stage 1 lands.

`DOSBoxInstance` is not worth preserving and goes with the file in Stage 2.
Nothing in this repo depends on it after §3.2; `launcher.py` covers CI.

### 3.3 Upstream PR

The remote-debug subsystem is fork-local, added in fork commit `2cc007655`
("first pass at adding a gdb server"). `master` has none of it: no
`src/debug/gdbserver.cpp`, `src/debug/qmp.cpp`, `include/gdbserver.h`,
`include/qmp.h`, `docs/REMOTEDEBUG.md`, or `tests/integration/`, and no
`C_REMOTEDEBUG` build flag. Upstream has no GDB server for Stage 1 to fix.

That changes what "the PR" would mean. Stage 1's fixes -- the protocol-
conformance bug plus two threading bugs, and the tests that pin them -- are
corrections to a fork-local subsystem, not a patch against something
upstream already has. Contributing them upstream would mean contributing the
entire subsystem: roughly 7,000 lines including the QMP and GDB servers, not
a small conformance patch. Whether that contribution is worth proposing, and
in what form, is an open framing decision, not one this stage settles.

## 4. Stage 2 — `dbxdebug`

### 4.1 Layout

```
src/dbxdebug/
  gdb.py         protocol client + capability handshake at connect
  qmp.py         protocol client + the six unwrapped commands
  session.py     DosboxSession                       <- from pb, near-verbatim
  registry.py    registry, list/reap, killpg         <- split out of session.py
  addressing.py  linear / "seg:off" conversion + trap history
  video.py       screen reading (existing)
  cli.py         + `dbxdebug session --list/--reap`, `dbxdebug doctor`
```

Splitting `registry.py` out is the one structural change to pb's file: at
46 KB it does lifecycle *and* cross-process bookkeeping, and `--reap` is
useful with no session object in hand.

### 4.2 Behavior carried over from `session.py`

Ephemeral ports with retry (the bind race is measured, not theoretical); one
isolated workdir per session with `files=` staging; teardown through three
independent paths (`__exit__`, `atexit`, SIGINT/SIGTERM) converging on an
idempotent `stop()` that does `killpg` SIGTERM then SIGKILL and
`shutil.rmtree` from Python, never a shell `rm`; a registry keyed on
`/proc` pid+starttime so a recycled pid cannot be mistaken for a live
session; `boot_settle` and `wait_for_text` so readiness is observed rather
than assumed (F5); `--list` / `--reap`.

`session.stop()` gains a graceful first step: QMP `quit` before escalating to
signals.

### 4.3 Breaking changes (0.x minor bump; `major_version_zero = true`)

- **Addressing collapses to one rule.** With `Z0` linear, `set_breakpoint` and
  `read_memory` mean the same thing by the same encoding. Both accept an int
  (linear) or a `"seg:off"` string, and `"seg:off"` is `seg*16+off`
  everywhere.
- **`bp_addr` becomes a raising shim, exported from `dbxdebug.addressing`.**
  pb's `bp_addr(seg, off)` packs
  `(seg << 16) | off` and its guardrail — separate arguments, no single-number
  overload — exists because pre-added values were silently wrong. After the
  fix, pre-added is correct and the packed form is wrong. `dbxdebug` exports
  the name so that a pb call site missed in Stage 3 raises on import-and-call
  with a pointer to the migration note, rather than silently packing an
  address that no longer means anything. It must never keep encoding quietly.
- **`connect()` refuses a pre-fix stub.** `GDBClient.connect()` reads
  `qSupported`; without `dosbox-x-linear-bp+` it raises unless the caller
  passes an explicit override. This is what stops pb's 38 call sites from
  silently mis-breaking against a stale build.
- **The unwrapped QMP commands land:** `memdump`, `screendump`, `savestate`,
  `loadstate`, `system_reset`, `quit`.

### 4.4 What else moves from pb

Moves: `free_port`, `port_is_listening`, `wait_ports_free`, `render_conf`,
`wait_for_text`, `screen_lines`; the hand-rolled `memdump`, `read_screen`,
`u16`, `read_word`, `read_block`, `registers_to_dict` helpers (they become
client methods); `probe_concurrency.py` as `dbxdebug doctor`;
`steps_out` / `walk_frames`, since 16-bit real-mode frame walking is generic
x86 and not PowerBASIC; and the batch fan-out *shell* — run N programs across
M concurrent sessions, collect artifacts, respect host capacity.

Stays in pb: `capture_stage.py` (PBMAIN versus MZ entry is PowerBASIC
startup semantics), `find_data_segment`, `pb_encode` / `pb_decode`,
`decode_glyph`, `rnd_value`, the `capture_hhfe_*` and `trace_*` scripts,
corpus-write policy, and `assert_meaningful` / `VacuousComparison` — that is
measurement discipline coupled to how pb builds paired corpora. The narrow
emulator-specific piece does move: a `session.assert_screen_readable()` that
rejects an all-blank or still-at-the-banner capture, because that failure
belongs to DOSBox and not to pb.

### 4.5 Tests

`dbxdebug`'s existing unit tests stay. Integration tests require a built
`dosbox-x` and are skipped without one, selected by an env var naming the
binary. The conformance assertions live upstream, not here; `dbxdebug` tests
its own behavior against a conforming stub.

## 5. Stage 3 — `powerbasic-decompile` migration

**Constraint: that working tree is edited by other agents concurrently.
Nothing there is modified without explicit confirmation from the user first.**
The migration is prepared as a described diff and applied as one reviewable
sweep, timed by the user.

`tools/dosbox/__init__.py` already re-exports the whole public surface, so the
migration is:

1. Rewrite `tools/dosbox/__init__.py` as a re-export of `dbxdebug`. The 38
   call sites keep working untouched.
2. Rewrite call sites to import `dbxdebug` directly.
3. Delete `tools/dosbox/session.py` and the shim. `dosbox_debug.py` is
   deleted upstream once this step is reached (see 3.4).
4. Convert the 8 files still hardcoding 2159/4444 in the same sweep, so no
   second class of caller survives.
5. Replace `bp_addr` uses with linear addresses.

Step 5 is the risky one and is why the `qSupported` handshake exists: a missed
site fails loudly at connect rather than silently mis-breaking.

## 6. Stage 4 — Skills

Two skills, shipped from the `dbxdebug` repo so they version with the library
they document, referenced from the personal marketplace by github source.
Both follow **Agent Plugins 1.0.0** (`agent-plugins.org`) and the **Agent
Skills** spec (`agentskills.io`), not the older marketplace-plugin shape.

```
dbxdebug/
  plugin.json                       $schema 1.0.0, name: dbxdebug
  skills/
    debug-dos-programs/
      SKILL.md
      references/{troubleshooting.md,recipes.md}
    dosbox-x-debug-protocol/
      SKILL.md
      references/{packets.md,qmp-commands.md,history.md}
```

`plugin.json` sits at the repo root; the spec ignores files it does not
recognize, so `src/`, `pyproject.toml` and `skills/` coexist. Each `SKILL.md`
stays under 500 lines with detail pushed into `references/` for progressive
disclosure. Validate with `skills-ref validate`.

**`debug-dos-programs`** — audience: anyone driving a DOS program under
emulation. Triggers on running or automating a DOS program, capturing memory,
registers or screen, and setting breakpoints. Body covers the load-bearing
non-obvious rules: always a `DosboxSession` context manager, never a
hand-rolled `Popen`, never `pkill`; ports accept before DOS reaches a prompt,
so `wait_for_text` and not a bare sleep; arm break-on-exec before typing the
program name; bulk reads through `memdump` rather than a loop of `m` packets;
`dbxdebug session --list` / `--reap` when things look stuck; `doctor` before
fanning out. `references/troubleshooting.md` indexes the traps by *symptom*
("breakpoint returns OK and never fires", "capture is all banner", "connected
to someone else's emulator"), because that is how people arrive at it.

**`dosbox-x-debug-protocol`** — audience: anyone editing `gdbserver.cpp`,
`qmp.cpp` or `debug.cpp`. Triggers on changing the servers or explaining stub
behavior. Body covers the threading contract (QMP runs on its own thread;
never touch guest state from it; use the request/poll idiom at
`dosbox.cpp:482-486`; that drain has to be duplicated inside the
`DEBUG_CheckGDBStep()` branch too (`dosbox.cpp:475-477`), or it silently
stops running the moment the CPU halts for GDB, per F6), the
address-semantics contract, the exact config option names, and a checklist
for adding a QMP command that ends at "and a conformance test, and a client
method, and the skill doc".
`references/history.md` carries the bug archaeology so the `FP_SEG` story is
not re-derived from source a third time.

## 7. Sequencing

Correctness first, in four stages: Stage 1 (`dosbox-x`, and the PR), Stage 2
(`dbxdebug`), Stage 3 (pb migration, on the user's timing), Stage 4 (skills).

Stage 1 first because F1 is the only defect here that produces confidently
wrong results rather than visible failures. The `qSupported` handshake means
fixing it first does not strand pb: the breakage becomes loud instead of
silent.

Stage 4 lands last but its `references/` are written throughout — the trap
list and the history are byproducts of Stages 1 to 3, not a separate research
effort.

## 8. Risks

- **Stage 1 touches `dosbox.cpp`'s main loop**, which is more sensitive than
  `gdbserver.cpp`. The change is a reordering of existing calls, and the
  conformance suite pins the behavior, but it is the part of the PR most
  likely to draw review.
- **The pb migration runs against a concurrently-edited tree.** Mitigated by
  preparing it as one reviewable sweep and applying it only on confirmation.
- **`AddBreakpoint(0, linear)` is verified for real mode.** `GetAddress`
  branches on `cpu.pmode` (`debug.cpp:450`); protected-mode breakpoint
  addressing is out of scope here and the conformance suite asserts real mode
  only.
- **Upstream may decline the PR.** The layering survives either way: the
  conformance suite and the C++ fixes are useful in the fork, and `dbxdebug`
  does not depend on the PR being merged — only on the handshake existing in
  whichever build is in use.

## 9. Out of scope

Protected-mode breakpoint semantics (`GetAddress(0, off)` routes through
`LinMakeProt(0, off)` in protected mode, which rejects selector 0, so a
GDB-set breakpoint stored against segment 0 never matches there -- this is
pre-existing, the old packed-far-pointer form was also broken in protected
mode for a different reason, and the conformance suite asserts real mode
only); hardware watchpoints (`Z1`-`Z4`, not implemented); binary `X` writes
and `qXfer`; the condition-variable path for high-rate dumps of a running
guest; a QMP `system_reset` issued while the CPU is halted for GDB now runs
instead of sitting pending forever, but the GDB client is not notified, so
it is left attached to a stale halt against a rebooted memory image --
notifying it is left for a follow-up; retiring `dosbox_debug.py`'s callers
outside these three repos, of which there are none known.
