# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## IMPORTANT: Issue Tracking

Issue tracking has moved out of this repository. The `.beads/` directory is
no longer part of the source tree — it is ignored by git and stays local to
each checkout. No replacement tracker has been chosen yet.

## Project Overview

DOSBox-X is a cross-platform DOS emulator based on DOSBox. It emulates IBM PC/XT/AT compatible systems, NEC PC-98, Tandy, and other systems for running DOS games and applications, including Windows 3.x/9x/ME. The codebase is C++ requiring C++11 support.

## Build Commands

### Linux (SDL1 - recommended for development)
```bash
./build-debug          # Full debug build with heavy debugging enabled
sudo make install      # Optional: install to /usr
```

### Linux (SDL2)
```bash
./build-debug-sdl2
```

### Windows (Visual Studio)
Open `vs/dosbox-x.sln` in Visual Studio 2017/2019/2022.

### Windows (MinGW/MSYS2)
```bash
./build-mingw          # SDL1
./build-mingw-sdl2     # SDL2
```

### macOS
```bash
./build-macos          # SDL1
./build-macos-sdl2     # SDL2
./build-macos universal  # Universal binary (Apple Silicon only)
make dosbox-x.app      # Create app bundle
```

### Clean Build
```bash
./cleantree            # Remove all generated files
./autogen.sh           # Regenerate autoconf files
```

## Running the Emulator

After building, the executable is at `src/dosbox-x`. The internal debugger can be activated with Alt+Pause when running with `--enable-debug`.

## Architecture

### Entry Point and Main Loop
- **src/gui/sdlmain.cpp**: Entry point (`main()`), emulator setup, SDL event loop, GFX management, menu handling
- **src/dosbox.cpp**: Configuration sections, `Normal_Loop()` tick execution, VM event callbacks

### CPU Emulation (`src/cpu/`)
- **core_normal.cpp**: Normal CPU interpreter core
- **core_dynamic.cpp** / **core_dyn_x86.cpp**: Dynamic recompiler for x86 hosts
- **core_prefetch.cpp**: Prefetch-accurate core for copy protection
- **cpu.cpp**: Protected mode, exceptions, task switching, MSRs
- **callback.cpp**: Callback system bridging emulated x86 code to native handlers (0xFE 0x38 instruction)

### Hardware Emulation (`src/hardware/`)
- **vga*.cpp**: VGA/SVGA emulation (S3, Tseng, Paradise chipsets)
- **vga_pc98*.cpp**: NEC PC-98 graphics emulation
- **sblaster.cpp**: Sound Blaster 1.0-16, ESS688, SC400
- **adlib.cpp**, **opl.cpp**, **nukedopl.cpp**: OPL2/OPL3 FM synthesis
- **mixer.cpp**: Audio mixing framework (1ms tick-based)
- **memory.cpp**: Memory mapping, A20 gate
- **pci_bus.cpp**: PCI bus emulation
- **voodoo*.cpp**, **glide.cpp**: 3dfx Voodoo emulation

### DOS/BIOS (`src/dos/`, `src/ints/`)
- **src/dos/dos.cpp**: DOS kernel, INT 21h handler
- **src/ints/**: BIOS interrupt handlers
- **src/shell/**: Command interpreter (COMMAND.COM emulation)

### GUI and Rendering (`src/gui/`)
- **menu.cpp**: Cross-platform menu framework (Windows HMENU, macOS NSMenu, SDL-drawn)
- **render.cpp**: Scaler selection, aspect ratio
- **sdl_mapper.cpp**: Input mapping system
- **sdl_gui.cpp**: Configuration GUI dialogs

### Output Backends (`src/output/`)
Surface, OpenGL, Direct3D, TTF rendering backends.

## Key Concepts

### Timing Model
- Time is measured in 1ms "ticks" synchronized to `SDL_GetTicks()`
- `cycles=N` setting executes N CPU cycles per millisecond
- Events scheduled via `PIC_AddEvent()` in `src/hardware/timer.cpp`
- Per-tick handlers via `TIMER_AddTickHandler()`

### Callback System
Native code hooks into emulation via callback instructions (0xFE 0x38 + uint16_t index). DOS/BIOS interrupts are implemented as callbacks to C++ handler functions that manipulate CPU registers and memory.

### Configuration
- Global `control` pointer provides access to sections/settings
- Sections defined in `src/dosbox.cpp` via `DOSBox_SetupConfigSections()`
- Reference configs: `dosbox-x.reference.conf`, `dosbox-x.reference.full.conf`

### VGA Emulation
- Mode determined by register state, not INT 10h mode number
- Video modes enumerated as `M_*` constants in `include/vga.h`
- Planar memory stored as uint32_t array (one byte per plane per address)
- Per-scanline rendering for raster effect accuracy

### Audio Mixer
- All sources render to 16-bit stereo at user-selected sample rate
- Call `MIXER_FillUp()` before state changes for sample-accurate audio
- Slew rate and lowpass filter options simulate DAC characteristics

## Testing

Unit tests are in `tests/` using Google Test framework:
- `dos_files_tests.cpp`, `drives_tests.cpp`
- `shell_cmds_tests.cpp`, `shell_redirection_tests.cpp`

## Code Style Notes

- Use `uint32_t`, `int16_t` etc. for specific-width integers (never assume `int`/`long` sizes)
- Use `uintptr_t` for pointer arithmetic
- Use `size_t`/`ssize_t` for sizes, `off_t` for file offsets
- Code assumes little-endian host (VGA planar operations)
- printf long long: use `%llu`/`%llx` on Linux, `%I64u`/`%I64d` on Windows
