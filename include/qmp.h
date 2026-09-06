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

#ifndef DOSBOX_QMP_H
#define DOSBOX_QMP_H

#include "dosbox.h"

#if C_REMOTEDEBUG

#include <string>
#include <vector>
#include <map>
#include <atomic>
#include <thread>
#include <mutex>
#include <queue>
#include <sys/socket.h>
#include <netinet/in.h>
#include <arpa/inet.h>
#include <unistd.h>
#include <fcntl.h>

#include "keyboard.h"

// QMP Server - QEMU Monitor Protocol compatible server for keyboard input
// Implements a subset of QMP focused on send-key and input-send-event commands

// Input event types for thread-safe queuing
enum class QMPInputEventType {
    KeyPress,
    KeyRelease,
    MouseButtonPress,
    MouseButtonRelease,
    MouseMove
};

struct QMPInputEvent {
    QMPInputEventType type;
    union {
        KBD_KEYS key;        // For keyboard events
        uint8_t button;      // For mouse button events
        struct {
            float x, y;      // For mouse move events
        } move;
    };
};

class QMPServer {
public:
    QMPServer(int port) : port(port), server_fd(-1), client_fd(-1), running(false) {}
    ~QMPServer() { stop(); }

    void start();  // Start server in a new thread
    void run();    // Main server loop (called by thread)
    void stop();   // Stop server and wait for thread to exit
    bool is_running() const { return running.load(); }

    // Process pending input events (called from main thread)
    void process_pending_input_events();

private:
    int port;
    int server_fd, client_fd;
    std::atomic<bool> running{false};
    std::thread server_thread;

    // Thread-safe input event queue
    std::mutex input_queue_mutex;
    std::queue<QMPInputEvent> input_queue;

    // Queue an input event for processing on the main thread
    void queue_input_event(const QMPInputEvent& event);

    // Socket operations
    void setup_socket();
    void wait_for_client();
    void handle_client();

    // Protocol handling
    void send_greeting();
    void send_response(const std::string& response);
    void send_success();
    void send_error(const std::string& error_class, const std::string& desc);
    std::string receive_command();
    void process_command(const std::string& cmd);

    /* One table drives both the dispatch in process_command and the list
     * query-commands advertises, so the implemented set and the advertised
     * set cannot drift apart. They did: the advertised list was a second
     * hand-written copy and fell behind the dispatch. Every entry is
     * advertised -- if it is dispatched, a client is entitled to find it. */
    struct CommandTableEntry {
        const char* name;
        void (*invoke)(QMPServer& self, const std::string& cmd);
    };
    static const std::vector<CommandTableEntry>& command_table();

    // Command handlers
    void handle_qmp_capabilities();
    void handle_acknowledged_no_op();
    void handle_send_key(const std::string& cmd);
    void handle_input_send_event(const std::string& cmd);
    void handle_query_commands();
    void handle_memdump(const std::string& cmd);
    void handle_screendump(const std::string& cmd);
    void handle_savestate(const std::string& cmd);
    void handle_loadstate(const std::string& cmd);
    void handle_stop();
    void handle_cont();
    void handle_system_reset(const std::string& cmd);
    void handle_query_status();
    void handle_debug_break_on_exec(const std::string& cmd);

    // Key mapping
    static KBD_KEYS qcode_to_kbd(const std::string& qcode);
    static const std::map<std::string, KBD_KEYS>& get_keymap();

    // JSON helpers (minimal implementation)
    static std::string extract_string(const std::string& json, const std::string& key);
    static int extract_int(const std::string& json, const std::string& key, int default_val);
    static bool extract_bool(const std::string& json, const std::string& key, bool default_val);
    static std::vector<std::string> extract_array(const std::string& json, const std::string& key);
};

// Public interface for debug.cpp
void QMP_StartServer(int port);
void QMP_StopServer();
bool QMP_IsServerRunning();
void QMP_ProcessPendingInputEvents();

#endif /* C_REMOTEDEBUG */

#endif /* DOSBOX_QMP_H */
