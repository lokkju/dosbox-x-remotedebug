/*
 *  Copyright (C) 2002-2021  The DOSBox Team
 *
 *  This program is free software; you can redistribute it and/or modify
 *  it under the terms of the GNU General Public License as published by
 *  the Free Software Foundation; either version 2 of the License, or
 *  (at your option) any later version.
 *
 *  This program is distributed in the hope that it will be useful,
 *  but WITHOUT ANY WARRANTY; without even the implied warranty of
 *  MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
 *  GNU General Public License for more details.
 *
 *  You should have received a copy of the GNU General Public License along
 *  with this program; if not, write to the Free Software Foundation, Inc.,
 *  51 Franklin Street, Fifth Floor, Boston, MA 02110-1301, USA.
 */

void DEBUG_SetupConsole(void);
void DEBUG_DrawScreen(void);
bool DEBUG_Breakpoint(void);
bool DEBUG_IntBreakpoint(uint8_t intNum);
void DEBUG_Enable(bool pressed);
void DEBUG_CheckExecuteBreakpoint(uint16_t seg, uint32_t off);
bool DEBUG_ExitLoop(void);
void DEBUG_RefreshPage(char scroll);
Bitu DEBUG_EnableDebugger(void);

// Exposed for GDB server
uint32_t DEBUG_GetRegister(int reg);
void DEBUG_SetRegister(int reg, uint32_t value);
/* Returns true if the byte was read. A read of unmapped or
 * not-present memory fails; the GDB stub stops its `m` reply at the
 * first failure rather than passing a fabricated byte off as guest
 * state. Note that in real mode with paging disabled a read of an
 * address no device claims does NOT fail -- the unmapped page
 * handler answers 0xFF, the way real hardware does. */
bool DEBUG_ReadMemory(uint32_t address, uint8_t *value);
/* Returns true if the byte landed. A write to unmapped or
 * write-protected memory fails; the GDB stub reports that as E01
 * rather than telling the client guest state changed when it did not. */
bool DEBUG_WriteMemory(uint32_t address, uint8_t value);
void DEBUG_Step();
void DEBUG_Continue();
bool DEBUG_SetBreakpoint(uint32_t address);
bool DEBUG_RemoveBreakpoint(uint32_t address);
bool DEBUG_SaveMemoryBin(const char* filepath, uint32_t address, uint32_t size);
#if C_REMOTEDEBUG
void DEBUG_StartGDBServer(int port);
void DEBUG_StopGDBServer();
bool DEBUG_IsGDBServerRunning();
void DEBUG_StartQMPServer(int port);
void DEBUG_StopQMPServer();
bool DEBUG_IsQMPServerRunning();
void DEBUG_CloseDebugger();  // Close debugger UI and resume execution
// Called by main loop to check and handle GDB step/continue requests
// Returns true if a step was executed (caller should return from loop)
bool DEBUG_CheckGDBStep();

// Unified debug state queries for QMP/external tools
bool DEBUG_IsDebuggerActive();   // Returns true if debugger (interactive or GDB) is active
bool DEBUG_IsCpuPausedForDebug(); // Returns true if CPU is paused for debugging
const char* DEBUG_GetDebuggerPauseReason(); // Returns reason: "gdb", "breakpoint", "step", "user", or nullptr

// Mutual exclusion between GDB and interactive debugger
bool DEBUG_IsInteractiveDebuggerActive(); // Returns true if interactive (curses) debugger is active
bool DEBUG_IsGDBClientConnected();        // Returns true if GDB client is connected

// GDB-aware program execution (for QMP debug-execute)
void DEBUG_SetGDBBreakOnExec(bool enable); // Set flag to break at entry for GDB
bool DEBUG_IsGDBBreakOnExecPending();      // Check if waiting for GDB break on exec
#endif

extern Bitu cycle_count;
extern Bitu debugCallback;

#ifdef C_HEAVY_DEBUG
bool DEBUG_HeavyIsBreakpoint(void);
void DEBUG_HeavyWriteLogInstruction(void);
#endif
