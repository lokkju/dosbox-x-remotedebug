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

#include "dosbox.h"

#if C_REMOTEDEBUG

#include <iostream>
#include <thread>
#include <chrono>
#include <cstring>
#include <netinet/tcp.h>
#include <algorithm>
#include <fstream>
#include <sstream>
#include "qmp.h"
#include "logging.h"
#include "debug.h"
#include "hardware.h"
#include "mouse.h"

static QMPServer* qmpServer = nullptr;

// Base64 encoding table
static const char base64_chars[] =
    "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789+/";

// Simple base64 encoder for binary data
static std::string base64_encode(const std::vector<uint8_t>& data) {
    std::string result;
    result.reserve(((data.size() + 2) / 3) * 4);

    size_t i = 0;
    while (i < data.size()) {
        uint32_t octet_a = i < data.size() ? data[i++] : 0;
        uint32_t octet_b = i < data.size() ? data[i++] : 0;
        uint32_t octet_c = i < data.size() ? data[i++] : 0;

        uint32_t triple = (octet_a << 16) + (octet_b << 8) + octet_c;

        result.push_back(base64_chars[(triple >> 18) & 0x3F]);
        result.push_back(base64_chars[(triple >> 12) & 0x3F]);
        result.push_back(base64_chars[(triple >> 6) & 0x3F]);
        result.push_back(base64_chars[triple & 0x3F]);
    }

    // Add padding
    size_t mod = data.size() % 3;
    if (mod == 1) {
        result[result.size() - 2] = '=';
        result[result.size() - 1] = '=';
    } else if (mod == 2) {
        result[result.size() - 1] = '=';
    }

    return result;
}

// Key mapping from QEMU QKeyCode names to DOSBox KBD_KEYS
const std::map<std::string, KBD_KEYS>& QMPServer::get_keymap() {
    static const std::map<std::string, KBD_KEYS> keymap = {
        // Numbers
        {"1", KBD_1}, {"2", KBD_2}, {"3", KBD_3}, {"4", KBD_4}, {"5", KBD_5},
        {"6", KBD_6}, {"7", KBD_7}, {"8", KBD_8}, {"9", KBD_9}, {"0", KBD_0},

        // Letters (QEMU uses lowercase)
        {"a", KBD_a}, {"b", KBD_b}, {"c", KBD_c}, {"d", KBD_d}, {"e", KBD_e},
        {"f", KBD_f}, {"g", KBD_g}, {"h", KBD_h}, {"i", KBD_i}, {"j", KBD_j},
        {"k", KBD_k}, {"l", KBD_l}, {"m", KBD_m}, {"n", KBD_n}, {"o", KBD_o},
        {"p", KBD_p}, {"q", KBD_q}, {"r", KBD_r}, {"s", KBD_s}, {"t", KBD_t},
        {"u", KBD_u}, {"v", KBD_v}, {"w", KBD_w}, {"x", KBD_x}, {"y", KBD_y},
        {"z", KBD_z},

        // Function keys
        {"f1", KBD_f1}, {"f2", KBD_f2}, {"f3", KBD_f3}, {"f4", KBD_f4},
        {"f5", KBD_f5}, {"f6", KBD_f6}, {"f7", KBD_f7}, {"f8", KBD_f8},
        {"f9", KBD_f9}, {"f10", KBD_f10}, {"f11", KBD_f11}, {"f12", KBD_f12},
        {"f13", KBD_f13}, {"f14", KBD_f14}, {"f15", KBD_f15}, {"f16", KBD_f16},
        {"f17", KBD_f17}, {"f18", KBD_f18}, {"f19", KBD_f19}, {"f20", KBD_f20},
        {"f21", KBD_f21}, {"f22", KBD_f22}, {"f23", KBD_f23}, {"f24", KBD_f24},

        // Modifiers
        {"shift", KBD_leftshift}, {"shift_r", KBD_rightshift},
        {"ctrl", KBD_leftctrl}, {"ctrl_r", KBD_rightctrl},
        {"alt", KBD_leftalt}, {"alt_r", KBD_rightalt},
        {"meta_l", KBD_lwindows}, {"meta_r", KBD_rwindows},
        {"menu", KBD_rwinmenu},

        // Special keys
        {"esc", KBD_esc}, {"tab", KBD_tab}, {"backspace", KBD_backspace},
        {"ret", KBD_enter}, {"spc", KBD_space},
        {"caps_lock", KBD_capslock}, {"num_lock", KBD_numlock},
        {"scroll_lock", KBD_scrolllock},

        // Punctuation and symbols
        {"grave_accent", KBD_grave}, {"minus", KBD_minus}, {"equal", KBD_equals},
        {"backslash", KBD_backslash}, {"bracket_left", KBD_leftbracket},
        {"bracket_right", KBD_rightbracket}, {"semicolon", KBD_semicolon},
        {"apostrophe", KBD_quote}, {"comma", KBD_comma}, {"dot", KBD_period},
        {"slash", KBD_slash}, {"less", KBD_extra_lt_gt},

        // Navigation
        {"insert", KBD_insert}, {"delete", KBD_delete},
        {"home", KBD_home}, {"end", KBD_end},
        {"pgup", KBD_pageup}, {"pgdn", KBD_pagedown},
        {"left", KBD_left}, {"right", KBD_right},
        {"up", KBD_up}, {"down", KBD_down},

        // Keypad
        {"kp_0", KBD_kp0}, {"kp_1", KBD_kp1}, {"kp_2", KBD_kp2}, {"kp_3", KBD_kp3},
        {"kp_4", KBD_kp4}, {"kp_5", KBD_kp5}, {"kp_6", KBD_kp6}, {"kp_7", KBD_kp7},
        {"kp_8", KBD_kp8}, {"kp_9", KBD_kp9},
        {"kp_divide", KBD_kpdivide}, {"kp_multiply", KBD_kpmultiply},
        {"kp_subtract", KBD_kpminus}, {"kp_add", KBD_kpplus},
        {"kp_enter", KBD_kpenter}, {"kp_decimal", KBD_kpperiod},
        {"kp_equals", KBD_kpequals}, {"kp_comma", KBD_kpcomma},

        // System keys
        {"print", KBD_printscreen}, {"sysrq", KBD_printscreen},
        {"pause", KBD_pause},

        // Japanese keys
        {"henkan", KBD_jp_henkan}, {"muhenkan", KBD_jp_muhenkan},
        {"hiragana", KBD_jp_hiragana}, {"yen", KBD_yen}, {"ro", KBD_jp_ro},
    };
    return keymap;
}

KBD_KEYS QMPServer::qcode_to_kbd(const std::string& qcode) {
    const auto& keymap = get_keymap();
    auto it = keymap.find(qcode);
    if (it != keymap.end()) {
        return it->second;
    }
    return KBD_NONE;
}

// Minimal JSON helpers - just enough for QMP

/* Every string this server puts on the wire goes through here first.
 * Replies interpolated their strings raw, so a single '"' or '\' anywhere
 * in a caller- or filesystem-supplied value -- an error message, a save
 * path, a screenshot destination -- terminated the JSON string early and
 * emitted a document the client cannot parse. The reply is then lost
 * entirely, not merely ugly. Windows-style paths reach the backslash case
 * without anyone trying.
 *
 * RFC 8259 requires escaping '"', '\' and everything below 0x20. Bytes at
 * 0x80 and above are passed through: the payloads here are filesystem paths
 * and log messages, which are already UTF-8 on every platform this builds
 * for, and re-encoding them would corrupt non-ASCII filenames. */
static std::string json_escape(const std::string& in) {
    std::string out;
    out.reserve(in.size());
    for (unsigned char c : in) {
        switch (c) {
            case '"':  out += "\\\""; break;
            case '\\': out += "\\\\"; break;
            case '\b': out += "\\b"; break;
            case '\f': out += "\\f"; break;
            case '\n': out += "\\n"; break;
            case '\r': out += "\\r"; break;
            case '\t': out += "\\t"; break;
            default:
                if (c < 0x20) {
                    char esc[7];
                    snprintf(esc, sizeof(esc), "\\u%04x", c);
                    out += esc;
                } else {
                    out += static_cast<char>(c);
                }
        }
    }
    return out;
}

/* The inverse, for the values pulled out of an incoming command. A JSON
 * string ends at the first unescaped '"', and \" and \\ inside it stand for
 * one character each. Scanning for a bare '"' and returning the bytes
 * verbatim -- which is what this used to do -- truncated any path
 * containing a quote at the quote, and doubled every backslash in a
 * Windows-style path. Escaping the replies is only half a fix if the
 * request cannot carry the value in the first place. */
static std::string json_unescape(const std::string& in, size_t start,
                                 size_t& end_out) {
    std::string out;
    size_t i = start;
    while (i < in.size()) {
        char c = in[i];
        if (c == '"') { end_out = i; return out; }
        if (c != '\\') { out += c; ++i; continue; }
        if (++i >= in.size()) break;
        switch (in[i]) {
            case '"':  out += '"';  ++i; break;
            case '\\': out += '\\'; ++i; break;
            case '/':  out += '/';  ++i; break;
            case 'b':  out += '\b'; ++i; break;
            case 'f':  out += '\f'; ++i; break;
            case 'n':  out += '\n'; ++i; break;
            case 'r':  out += '\r'; ++i; break;
            case 't':  out += '\t'; ++i; break;
            case 'u': {
                /* \uXXXX. Only the Basic Latin range is decoded to a byte;
                 * anything above 0x7F would need UTF-8 re-encoding and
                 * surrogate pairing, which no command here has a use for,
                 * so those are passed through as written rather than
                 * silently mangled. */
                if (i + 4 < in.size()) {
                    const std::string hex = in.substr(i + 1, 4);
                    if (hex.find_first_not_of("0123456789abcdefABCDEF")
                        == std::string::npos) {
                        const unsigned long cp = strtoul(hex.c_str(), NULL, 16);
                        if (cp <= 0x7F) {
                            out += static_cast<char>(cp);
                            i += 5;
                            break;
                        }
                    }
                }
                out += "\\u";
                ++i;
                break;
            }
            default: out += '\\'; break;  // not an escape; keep it literal
        }
    }
    end_out = std::string::npos;
    return "";  // unterminated string
}

std::string QMPServer::extract_string(const std::string& json, const std::string& key) {
    std::string search = "\"" + key + "\"";
    size_t pos = json.find(search);
    if (pos == std::string::npos) return "";

    pos = json.find(":", pos);
    if (pos == std::string::npos) return "";

    // Skip whitespace
    pos = json.find_first_not_of(" \t\n\r", pos + 1);
    if (pos == std::string::npos) return "";

    if (json[pos] == '"') {
        size_t end = std::string::npos;
        std::string value = json_unescape(json, pos + 1, end);
        if (end != std::string::npos) {
            return value;
        }
    }
    return "";
}

int QMPServer::extract_int(const std::string& json, const std::string& key, int default_val) {
    std::string search = "\"" + key + "\"";
    size_t pos = json.find(search);
    if (pos == std::string::npos) return default_val;

    pos = json.find(":", pos);
    if (pos == std::string::npos) return default_val;

    pos = json.find_first_not_of(" \t\n\r", pos + 1);
    if (pos == std::string::npos) return default_val;

    try {
        return std::stoi(json.substr(pos));
    } catch (...) {
        return default_val;
    }
}

bool QMPServer::extract_bool(const std::string& json, const std::string& key, bool default_val) {
    std::string search = "\"" + key + "\"";
    size_t pos = json.find(search);
    if (pos == std::string::npos) return default_val;

    pos = json.find(":", pos);
    if (pos == std::string::npos) return default_val;

    pos = json.find_first_not_of(" \t\n\r", pos + 1);
    if (pos == std::string::npos) return default_val;

    if (json.substr(pos, 4) == "true") return true;
    if (json.substr(pos, 5) == "false") return false;
    return default_val;
}

std::vector<std::string> QMPServer::extract_array(const std::string& json, const std::string& key) {
    std::vector<std::string> result;
    std::string search = "\"" + key + "\"";
    size_t pos = json.find(search);
    if (pos == std::string::npos) return result;

    pos = json.find("[", pos);
    if (pos == std::string::npos) return result;

    size_t end = json.find("]", pos);
    if (end == std::string::npos) return result;

    std::string arr = json.substr(pos + 1, end - pos - 1);

    // Extract objects from array
    size_t obj_start = 0;
    int depth = 0;
    for (size_t i = 0; i < arr.size(); i++) {
        if (arr[i] == '{') {
            if (depth == 0) obj_start = i;
            depth++;
        } else if (arr[i] == '}') {
            depth--;
            if (depth == 0) {
                result.push_back(arr.substr(obj_start, i - obj_start + 1));
            }
        }
    }
    return result;
}

void QMPServer::start() {
    if (running.load()) {
        LOG(LOG_REMOTE, LOG_WARN)("QMP: Server already running");
        return;
    }
    // Set running before spawning thread so is_running() returns true immediately
    running.store(true);
    server_thread = std::thread(&QMPServer::run, this);
}

void QMPServer::run() {
    LOG(LOG_REMOTE, LOG_NORMAL)("QMP: Starting server...");
    setup_socket();

    while (running.load()) {
        wait_for_client();
        if (running.load() && client_fd != -1) {
            handle_client();
        }
    }
    LOG(LOG_REMOTE, LOG_NORMAL)("QMP: Server stopped");
}

void QMPServer::stop() {
    if (!running.load()) return;
    running.store(false);
    LOG(LOG_REMOTE, LOG_NORMAL)("QMP: Stopping server...");
    // Use shutdown to unblock any blocking recv/accept calls
    if (client_fd != -1) {
        shutdown(client_fd, SHUT_RDWR);
        close(client_fd);
        client_fd = -1;
    }
    if (server_fd != -1) {
        shutdown(server_fd, SHUT_RDWR);
        close(server_fd);
        server_fd = -1;
    }
    // Wait for server thread to finish
    if (server_thread.joinable()) {
        server_thread.join();
    }
}

void QMPServer::setup_socket() {
    struct sockaddr_in address;
    int opt = 1;

    if ((server_fd = socket(AF_INET, SOCK_STREAM, 0)) == 0) {
        LOG(LOG_REMOTE, LOG_ERROR)("QMP: socket failed");
        return;
    }

    if (setsockopt(server_fd, SOL_SOCKET, SO_REUSEADDR | SO_REUSEPORT, &opt, sizeof(opt))) {
        LOG(LOG_REMOTE, LOG_ERROR)("QMP: setsockopt failed");
        return;
    }

    address.sin_family = AF_INET;
    address.sin_addr.s_addr = INADDR_ANY;
    address.sin_port = htons(port);

    if (bind(server_fd, (struct sockaddr *)&address, sizeof(address)) < 0) {
        LOG(LOG_REMOTE, LOG_ERROR)("QMP: bind failed on port %d", port);
        return;
    }

    if (listen(server_fd, 1) < 0) {
        LOG(LOG_REMOTE, LOG_ERROR)("QMP: listen failed");
        return;
    }

    LOG(LOG_REMOTE, LOG_NORMAL)("QMP: Listening on port %d", port);
}

void QMPServer::wait_for_client() {
    struct sockaddr_in address;
    socklen_t addrlen = sizeof(address);

    client_fd = accept(server_fd, (struct sockaddr *)&address, &addrlen);
    if (client_fd < 0) {
        if (running.load()) {
            LOG(LOG_REMOTE, LOG_ERROR)("QMP: accept failed");
        }
        return;
    }
    /* The RSP is strictly request/response with tiny packets, which is the
     * workload Nagle punishes worst: each side holds a small write waiting for
     * an ACK the peer has delayed, costing ~40ms per direction. Measured
     * ~82ms per round-trip with it on, ~41ms with only the client fixed, so
     * both ends have to set it. Cost is per round-trip regardless of size, so
     * it falls entirely on trip count. */
    int nodelay = 1;
    setsockopt(client_fd, IPPROTO_TCP, TCP_NODELAY, &nodelay, sizeof(nodelay));

    LOG(LOG_REMOTE, LOG_NORMAL)("QMP: Client connected");
}

void QMPServer::handle_client() {
    send_greeting();

    while (running.load() && client_fd != -1) {
        std::string cmd = receive_command();
        if (cmd.empty()) {
            break;
        }
        process_command(cmd);
    }

    if (client_fd != -1) {
        close(client_fd);
        client_fd = -1;
    }
    LOG(LOG_REMOTE, LOG_NORMAL)("QMP: Client disconnected");
}

void QMPServer::send_greeting() {
    /* QMP greeting - tells the client what capabilities are available.
     *
     * The list is empty on purpose. It used to claim "oob", which in QMP
     * means the server accepts out-of-band commands: ones carrying an "id"
     * that it executes while another command is still in flight. This
     * server processes commands strictly in order on one thread, and no
     * handler reads or echoes "id", so a client that trusted the
     * advertisement would wait for a reply that cannot arrive. Add "oob"
     * back only alongside an out-of-band path and id echoing. */
    std::string greeting = "{\"QMP\": {\"version\": {\"qemu\": {\"micro\": 0, \"minor\": 0, \"major\": 0}, "
                          "\"package\": \"DOSBox-X\"}, \"capabilities\": []}}\r\n";
    send_response(greeting);
}

void QMPServer::send_response(const std::string& response) {
    if (client_fd != -1) {
        send(client_fd, response.c_str(), response.length(), 0);
    }
}

void QMPServer::send_success() {
    send_response("{\"return\": {}}\r\n");
}

void QMPServer::send_error(const std::string& error_class, const std::string& desc) {
    std::string response = "{\"error\": {\"class\": \"" + json_escape(error_class)
                         + "\", \"desc\": \"" + json_escape(desc) + "\"}}\r\n";
    send_response(response);
}

std::string QMPServer::receive_command() {
    char buffer[4096];
    std::string cmd;

    while (running.load()) {
        ssize_t bytes = recv(client_fd, buffer, sizeof(buffer) - 1, 0);
        if (bytes <= 0) {
            return "";
        }
        buffer[bytes] = '\0';
        cmd += buffer;

        // Look for complete JSON object (ends with } possibly followed by whitespace)
        size_t brace_count = 0;
        bool in_string = false;
        for (size_t i = 0; i < cmd.size(); i++) {
            if (cmd[i] == '"' && (i == 0 || cmd[i-1] != '\\')) {
                in_string = !in_string;
            } else if (!in_string) {
                if (cmd[i] == '{') brace_count++;
                else if (cmd[i] == '}') {
                    brace_count--;
                    if (brace_count == 0) {
                        return cmd.substr(0, i + 1);
                    }
                }
            }
        }
    }
    return "";
}

const std::vector<QMPServer::CommandTableEntry>& QMPServer::command_table() {
    /* Defined inside a member function so the lambdas inherit the class's
     * access to the private handlers. Captureless lambdas convert to the
     * plain function pointer the entry holds. */
    static const std::vector<CommandTableEntry> table = {
        {"qmp_capabilities",
         [](QMPServer& s, const std::string&) { s.handle_qmp_capabilities(); }},
        {"send-key",
         [](QMPServer& s, const std::string& c) { s.handle_send_key(c); }},
        {"input-send-event",
         [](QMPServer& s, const std::string& c) { s.handle_input_send_event(c); }},
        {"query-commands",
         [](QMPServer& s, const std::string&) { s.handle_query_commands(); }},
        {"query-status",
         [](QMPServer& s, const std::string&) { s.handle_query_status(); }},
        {"memdump",
         [](QMPServer& s, const std::string& c) { s.handle_memdump(c); }},
        {"screendump",
         [](QMPServer& s, const std::string& c) { s.handle_screendump(c); }},
        {"savestate",
         [](QMPServer& s, const std::string& c) { s.handle_savestate(c); }},
        {"loadstate",
         [](QMPServer& s, const std::string& c) { s.handle_loadstate(c); }},
        {"stop",
         [](QMPServer& s, const std::string&) { s.handle_stop(); }},
        {"cont",
         [](QMPServer& s, const std::string&) { s.handle_cont(); }},
        {"system_reset",
         [](QMPServer& s, const std::string& c) { s.handle_system_reset(c); }},
        {"debug-break-on-exec",
         [](QMPServer& s, const std::string& c) { s.handle_debug_break_on_exec(c); }},
        {"quit",
         [](QMPServer& s, const std::string&) { s.handle_acknowledged_no_op(); }},
        {"system_powerdown",
         [](QMPServer& s, const std::string&) { s.handle_acknowledged_no_op(); }},
    };
    return table;
}

void QMPServer::process_command(const std::string& cmd) {
    const std::string execute = extract_string(cmd, "execute");
    if (execute.empty()) {
        send_error("GenericError", "Invalid command format");
        return;
    }

    for (const CommandTableEntry& entry : command_table()) {
        if (execute == entry.name) {
            entry.invoke(*this, cmd);
            return;
        }
    }

    send_error("CommandNotFound", "Command not found: " + execute);
}

void QMPServer::handle_qmp_capabilities() {
    // Client acknowledges capabilities, we're now in command mode
    send_success();
}

void QMPServer::handle_acknowledged_no_op() {
    /* quit and system_powerdown are acknowledged but not acted on: a debug
     * client must not be able to take the emulator down out from under the
     * user. They are dispatched, so they are advertised -- a client that
     * does not find them in query-commands concludes they would be
     * rejected, which is a different lie from the one they tell now. */
    send_success();
}

void QMPServer::handle_query_commands() {
    /* Built from the dispatch table, never from a second list of names. */
    std::string response = "{\"return\": [";
    bool first = true;
    for (const CommandTableEntry& entry : command_table()) {
        if (!first) response += ",";
        first = false;
        response += "{\"name\": \"";
        response += json_escape(entry.name);
        response += "\"}";
    }
    response += "]}\r\n";
    send_response(response);
}

void QMPServer::queue_input_event(const QMPInputEvent& event) {
    std::lock_guard<std::mutex> lock(input_queue_mutex);
    input_queue.push(event);
}

void QMPServer::process_pending_input_events() {
    // Process all queued events - this runs on the main thread
    std::queue<QMPInputEvent> events_to_process;

    {
        std::lock_guard<std::mutex> lock(input_queue_mutex);
        std::swap(events_to_process, input_queue);
    }

    while (!events_to_process.empty()) {
        const QMPInputEvent& event = events_to_process.front();

        switch (event.type) {
            case QMPInputEventType::KeyPress:
                KEYBOARD_AddKey(event.key, true);
                break;
            case QMPInputEventType::KeyRelease:
                KEYBOARD_AddKey(event.key, false);
                break;
            case QMPInputEventType::MouseButtonPress:
                Mouse_ButtonPressed(event.button);
                break;
            case QMPInputEventType::MouseButtonRelease:
                Mouse_ButtonReleased(event.button);
                break;
            case QMPInputEventType::MouseMove:
                Mouse_CursorMoved(event.move.x, event.move.y, 0, 0, true);
                break;
        }

        events_to_process.pop();
    }
}

void QMPServer::handle_send_key(const std::string& cmd) {
    // Extract hold-time (default 100ms per QEMU spec)
    // Note: hold-time is now handled by queuing press and release events
    // The actual timing between press/release depends on main loop frequency
    (void)extract_int(cmd, "hold-time", 100);  // Acknowledged but not used for blocking

    // Extract keys array
    std::vector<std::string> keys = extract_array(cmd, "keys");
    if (keys.empty()) {
        send_error("GenericError", "No keys specified");
        return;
    }

    // Collect all keys to press
    std::vector<KBD_KEYS> kbd_keys;
    for (const auto& key_obj : keys) {
        std::string type = extract_string(key_obj, "type");
        std::string data = extract_string(key_obj, "data");

        if (type == "qcode" && !data.empty()) {
            KBD_KEYS kbd = qcode_to_kbd(data);
            if (kbd != KBD_NONE) {
                kbd_keys.push_back(kbd);
            } else {
                LOG(LOG_REMOTE, LOG_WARN)("QMP: Unknown qcode: %s", data.c_str());
            }
        }
    }

    // Queue key press events
    for (auto key : kbd_keys) {
        QMPInputEvent event;
        event.type = QMPInputEventType::KeyPress;
        event.key = key;
        queue_input_event(event);
    }

    // Queue key release events (reverse order)
    for (auto it = kbd_keys.rbegin(); it != kbd_keys.rend(); ++it) {
        QMPInputEvent event;
        event.type = QMPInputEventType::KeyRelease;
        event.key = *it;
        queue_input_event(event);
    }

    send_success();
}

void QMPServer::handle_input_send_event(const std::string& cmd) {
    std::vector<std::string> events = extract_array(cmd, "events");
    if (events.empty()) {
        send_error("GenericError", "No events specified");
        return;
    }

    // Accumulate relative mouse movements to send as a single event
    float mouse_xrel = 0, mouse_yrel = 0;
    bool has_mouse_move = false;

    for (const auto& event_json : events) {
        std::string type = extract_string(event_json, "type");

        // Find the data object - it's nested
        size_t data_pos = event_json.find("\"data\"");
        if (data_pos == std::string::npos) continue;

        size_t data_start = event_json.find("{", data_pos);
        if (data_start == std::string::npos) continue;

        std::string data_str = event_json.substr(data_start);

        if (type == "key") {
            // Keyboard event
            bool down = extract_bool(data_str, "down", true);

            // Find the key object within data
            size_t key_pos = data_str.find("\"key\"");
            if (key_pos == std::string::npos) continue;

            size_t key_start = data_str.find("{", key_pos);
            if (key_start == std::string::npos) continue;

            std::string key_str = data_str.substr(key_start);
            std::string key_type = extract_string(key_str, "type");
            std::string key_data = extract_string(key_str, "data");

            if (key_type == "qcode" && !key_data.empty()) {
                KBD_KEYS kbd = qcode_to_kbd(key_data);
                if (kbd != KBD_NONE) {
                    QMPInputEvent input_event;
                    input_event.type = down ? QMPInputEventType::KeyPress : QMPInputEventType::KeyRelease;
                    input_event.key = kbd;
                    queue_input_event(input_event);
                } else {
                    LOG(LOG_REMOTE, LOG_WARN)("QMP: Unknown qcode: %s", key_data.c_str());
                }
            }
        } else if (type == "rel") {
            // Relative mouse movement
            std::string axis = extract_string(data_str, "axis");
            int value = extract_int(data_str, "value", 0);

            if (axis == "x") {
                mouse_xrel += static_cast<float>(value);
                has_mouse_move = true;
            } else if (axis == "y") {
                mouse_yrel += static_cast<float>(value);
                has_mouse_move = true;
            }
        } else if (type == "btn") {
            // Mouse button event
            std::string button = extract_string(data_str, "button");
            bool down = extract_bool(data_str, "down", true);

            uint8_t btn_id = 0;
            if (button == "left") {
                btn_id = 0;
            } else if (button == "right") {
                btn_id = 1;
            } else if (button == "middle") {
                btn_id = 2;
            } else {
                LOG(LOG_REMOTE, LOG_WARN)("QMP: Unknown mouse button: %s", button.c_str());
                continue;
            }

            QMPInputEvent input_event;
            input_event.type = down ? QMPInputEventType::MouseButtonPress : QMPInputEventType::MouseButtonRelease;
            input_event.button = btn_id;
            queue_input_event(input_event);
        }
    }

    // Queue accumulated mouse movement as a single event
    if (has_mouse_move) {
        QMPInputEvent input_event;
        input_event.type = QMPInputEventType::MouseMove;
        input_event.move.x = mouse_xrel;
        input_event.move.y = mouse_yrel;
        queue_input_event(input_event);
    }

    send_success();
}

void QMPServer::handle_memdump(const std::string& cmd) {
    // Extract arguments
    std::string args_str = extract_string(cmd, "arguments");
    if (args_str.empty()) {
        // Try to find arguments object directly in cmd
        size_t args_pos = cmd.find("\"arguments\"");
        if (args_pos != std::string::npos) {
            size_t brace = cmd.find("{", args_pos);
            if (brace != std::string::npos) {
                int depth = 1;
                size_t end = brace + 1;
                while (end < cmd.size() && depth > 0) {
                    if (cmd[end] == '{') depth++;
                    else if (cmd[end] == '}') depth--;
                    end++;
                }
                args_str = cmd.substr(brace, end - brace);
            }
        }
    }

    // Parse address and size
    int address = extract_int(args_str, "address", -1);
    int size = extract_int(args_str, "size", -1);
    std::string file = extract_string(args_str, "file");

    if (address < 0 || size <= 0) {
        send_error("GenericError", "Missing or invalid 'address' and/or 'size' arguments");
        return;
    }

    if (size > 16 * 1024 * 1024) {  // Limit to 16MB
        send_error("GenericError", "Size too large (max 16MB)");
        return;
    }

    std::string filepath;
    bool use_temp = file.empty();

    if (use_temp) {
        // Create temp file
        filepath = "/tmp/dosbox_memdump_XXXXXX";
        char* temp_path = strdup(filepath.c_str());
        int fd = mkstemp(temp_path);
        if (fd < 0) {
            free(temp_path);
            send_error("GenericError", "Failed to create temp file");
            return;
        }
        close(fd);
        filepath = temp_path;
        free(temp_path);
    } else {
        filepath = file;
    }

    /* memdump is the only QMP handler that reaches into guest state from
     * the socket thread; savestate, screendump and the input queue all
     * defer to the emulation thread. When the CPU is stopped for debugging
     * no guest code is executing, so memory is quiescent and reading it
     * here races nothing. Two distinct states qualify and they are tracked by disjoint
     * flags: DEBUG_IsCpuPausedForDebug() covers the interactive debugger and
     * a GDB halt, EMULATOR_IsPaused() covers a QMP `stop`, which parks the
     * emulation thread in PauseDOSBoxLoop. Both leave memory quiescent.
     * When the guest is actually running, refuse.
     *
     * The deferred path currently reports busy rather than queueing. A
     * request/response marshal with condition-variable signalling is
     * deliberately not built yet: the existing SAVESTATE_* idiom polls at
     * 100ms, which would destroy the 30-60Hz use case this command exists
     * for, and dumping a RUNNING guest at that rate has no measured
     * consumer. See section 3.1 of
     * docs/superpowers/specs/2026-09-03-dosbox-debug-harness-design.md. */
    if (!DEBUG_IsCpuPausedForDebug() && !EMULATOR_IsPaused()) {
        if (use_temp) unlink(filepath.c_str());
        send_error("GenericError",
                   "memdump requires the CPU to be stopped for debugging; "
                   "halt via GDB or QMP stop first");
        return;
    }

    // Perform the memory dump
    if (!DEBUG_SaveMemoryBin(filepath.c_str(), (uint32_t)address, (uint32_t)size)) {
        if (use_temp) unlink(filepath.c_str());
        send_error("GenericError", "Failed to dump memory");
        return;
    }

    std::ostringstream response;
    if (use_temp) {
        // Read file and return as base64
        std::ifstream infile(filepath, std::ios::binary);
        if (!infile) {
            unlink(filepath.c_str());
            send_error("GenericError", "Failed to read dump file");
            return;
        }

        std::vector<uint8_t> data((std::istreambuf_iterator<char>(infile)),
                                   std::istreambuf_iterator<char>());
        infile.close();
        unlink(filepath.c_str());

        std::string b64 = base64_encode(data);
        response << "{\"return\": {\"data\": \"" << b64 << "\", \"size\": " << size << "}}\r\n";
    } else {
        // Return file path
        response << "{\"return\": {\"file\": \"" << json_escape(file)
                 << "\", \"size\": " << size << "}}\r\n";
    }

    send_response(response.str());
}

void QMPServer::handle_screendump(const std::string& cmd) {
    // Extract optional file argument
    std::string args_str = extract_string(cmd, "arguments");
    if (args_str.empty()) {
        size_t args_pos = cmd.find("\"arguments\"");
        if (args_pos != std::string::npos) {
            size_t brace = cmd.find("{", args_pos);
            if (brace != std::string::npos) {
                int depth = 1;
                size_t end = brace + 1;
                while (end < cmd.size() && depth > 0) {
                    if (cmd[end] == '{') depth++;
                    else if (cmd[end] == '}') depth--;
                    end++;
                }
                args_str = cmd.substr(brace, end - brace);
            }
        }
    }

    std::string file = extract_string(args_str, "file");

    // Clear any previous screenshot path before triggering new capture
    CAPTURE_ClearLastScreenshotPath();

    // Trigger screenshot capture
    CAPTURE_TakeScreenshot();

    // Wait for screenshot to complete (poll with timeout)
    const int timeout_ms = 5000;  // 5 second timeout
    const int poll_interval_ms = 50;
    int waited = 0;

    while (CAPTURE_IsScreenshotPending() && waited < timeout_ms) {
        std::this_thread::sleep_for(std::chrono::milliseconds(poll_interval_ms));
        waited += poll_interval_ms;
    }

    if (waited >= timeout_ms) {
        send_error("GenericError", "Screenshot capture timed out");
        return;
    }

    // Give a little extra time for the path to be set
    std::this_thread::sleep_for(std::chrono::milliseconds(50));

    // Get the screenshot path
    std::string screenshot_path = CAPTURE_GetLastScreenshotPath();
    if (screenshot_path.empty()) {
        send_error("GenericError", "Screenshot capture failed - no file created");
        return;
    }

    std::ostringstream response;
    if (file.empty()) {
        // Return base64-encoded screenshot data
        std::ifstream infile(screenshot_path, std::ios::binary);
        if (!infile) {
            send_error("GenericError", "Failed to read screenshot file");
            return;
        }

        std::vector<uint8_t> data((std::istreambuf_iterator<char>(infile)),
                                   std::istreambuf_iterator<char>());
        infile.close();

        std::string b64 = base64_encode(data);
        response << "{\"return\": {\"data\": \"" << b64 << "\", \"size\": " << data.size()
                 << ", \"format\": \"png\", \"file\": \"" << json_escape(screenshot_path)
                 << "\"}}\r\n";
    } else {
        // Copy to requested file path
        std::ifstream src(screenshot_path, std::ios::binary);
        std::ofstream dst(file, std::ios::binary);
        if (!src || !dst) {
            send_error("GenericError", "Failed to copy screenshot to " + file);
            return;
        }
        dst << src.rdbuf();
        src.close();
        dst.close();

        // Get file size
        std::ifstream check(file, std::ios::binary | std::ios::ate);
        size_t size = check.tellg();
        check.close();

        response << "{\"return\": {\"file\": \"" << json_escape(file)
                 << "\", \"size\": " << size
                 << ", \"format\": \"png\"}}\r\n";
    }

    send_response(response.str());
}

void QMPServer::handle_savestate(const std::string& cmd) {
    // Extract file argument (required)
    std::string args_str = extract_string(cmd, "arguments");
    if (args_str.empty()) {
        size_t args_pos = cmd.find("\"arguments\"");
        if (args_pos != std::string::npos) {
            size_t brace = cmd.find("{", args_pos);
            if (brace != std::string::npos) {
                int depth = 1;
                size_t end = brace + 1;
                while (end < cmd.size() && depth > 0) {
                    if (cmd[end] == '{') depth++;
                    else if (cmd[end] == '}') depth--;
                    end++;
                }
                args_str = cmd.substr(brace, end - brace);
            }
        }
    }

    std::string file = extract_string(args_str, "file");
    if (file.empty()) {
        send_error("GenericError", "Missing required 'file' argument");
        return;
    }

    // Request save state (async, processed by main thread)
    SAVESTATE_RequestSave(file);

    // Wait for completion with timeout
    const int timeout_ms = 30000;  // 30 second timeout for save
    const int poll_interval_ms = 100;
    int waited = 0;

    while (SAVESTATE_IsPending() && waited < timeout_ms) {
        std::this_thread::sleep_for(std::chrono::milliseconds(poll_interval_ms));
        waited += poll_interval_ms;
    }

    if (waited >= timeout_ms) {
        send_error("GenericError", "Save state operation timed out");
        return;
    }

    // Check result
    std::string error;
    if (SAVESTATE_IsComplete(error)) {
        if (error.empty()) {
            std::ostringstream response;
            response << "{\"return\": {\"file\": \"" << json_escape(file) << "\"}}\r\n";
            send_response(response.str());
        } else {
            send_error("GenericError", error);
        }
    } else {
        send_error("GenericError", "Save state failed - unknown error");
    }
}

void QMPServer::handle_loadstate(const std::string& cmd) {
    // Extract file argument (required)
    std::string args_str = extract_string(cmd, "arguments");
    if (args_str.empty()) {
        size_t args_pos = cmd.find("\"arguments\"");
        if (args_pos != std::string::npos) {
            size_t brace = cmd.find("{", args_pos);
            if (brace != std::string::npos) {
                int depth = 1;
                size_t end = brace + 1;
                while (end < cmd.size() && depth > 0) {
                    if (cmd[end] == '{') depth++;
                    else if (cmd[end] == '}') depth--;
                    end++;
                }
                args_str = cmd.substr(brace, end - brace);
            }
        }
    }

    std::string file = extract_string(args_str, "file");
    if (file.empty()) {
        send_error("GenericError", "Missing required 'file' argument");
        return;
    }

    // Check if file exists
    std::ifstream check(file);
    if (!check.good()) {
        send_error("GenericError", "State file not found: " + file);
        return;
    }
    check.close();

    // Request load state (async, processed by main thread)
    SAVESTATE_RequestLoad(file);

    // Wait for completion with timeout
    const int timeout_ms = 30000;  // 30 second timeout for load
    const int poll_interval_ms = 100;
    int waited = 0;

    while (SAVESTATE_IsPending() && waited < timeout_ms) {
        std::this_thread::sleep_for(std::chrono::milliseconds(poll_interval_ms));
        waited += poll_interval_ms;
    }

    if (waited >= timeout_ms) {
        send_error("GenericError", "Load state operation timed out");
        return;
    }

    // Check result
    std::string error;
    if (SAVESTATE_IsComplete(error)) {
        if (error.empty()) {
            std::ostringstream response;
            response << "{\"return\": {\"file\": \"" << json_escape(file) << "\"}}\r\n";
            send_response(response.str());
        } else {
            send_error("GenericError", error);
        }
    } else {
        send_error("GenericError", "Load state failed - unknown error");
    }
}

void QMPServer::handle_stop() {
    // Pause the emulator
    if (EMULATOR_IsPaused()) {
        // Already paused - return success anyway for idempotency
        send_success();
        return;
    }

    EMULATOR_RequestPause();

    // Wait briefly for pause to take effect
    const int timeout_ms = 1000;
    const int poll_interval_ms = 10;
    int waited = 0;

    while (!EMULATOR_IsPaused() && waited < timeout_ms) {
        std::this_thread::sleep_for(std::chrono::milliseconds(poll_interval_ms));
        waited += poll_interval_ms;
    }

    if (EMULATOR_IsPaused()) {
        send_success();
    } else {
        send_error("GenericError", "Failed to pause emulator");
    }
}

void QMPServer::handle_cont() {
    // Resume the emulator
    if (!EMULATOR_IsPaused()) {
        // Already running - return success anyway for idempotency
        send_success();
        return;
    }

    EMULATOR_RequestResume();

    // Wait briefly for resume to take effect
    const int timeout_ms = 1000;
    const int poll_interval_ms = 10;
    int waited = 0;

    while (EMULATOR_IsPaused() && waited < timeout_ms) {
        std::this_thread::sleep_for(std::chrono::milliseconds(poll_interval_ms));
        waited += poll_interval_ms;
    }

    if (!EMULATOR_IsPaused()) {
        send_success();
    } else {
        send_error("GenericError", "Failed to resume emulator");
    }
}

void QMPServer::handle_system_reset(const std::string& cmd) {
    // Extract optional dos_only argument
    std::string args_str;
    size_t args_pos = cmd.find("\"arguments\"");
    if (args_pos != std::string::npos) {
        size_t brace = cmd.find("{", args_pos);
        if (brace != std::string::npos) {
            int depth = 1;
            size_t end = brace + 1;
            while (end < cmd.size() && depth > 0) {
                if (cmd[end] == '{') depth++;
                else if (cmd[end] == '}') depth--;
                end++;
            }
            args_str = cmd.substr(brace, end - brace);
        }
    }

    bool dos_only = extract_bool(args_str, "dos_only", false);

    /* handle_memdump refuses when it cannot produce a coherent result; this
     * handler is reachable on the same GDB-halted path (see the drain fix
     * referenced in handle_memdump above) and must not answer differently.
     * Rebooting the guest while a GDB client still believes it is attached
     * at a halt leaves that client staring at a session -- registers,
     * memory, breakpoints -- that no longer exists.
     *
     * The alternative -- reset anyway and then somehow notify the GDB
     * client -- was considered and rejected: what "notify" should mean here
     * (a synthetic stop reply? forcibly detaching? something else?) is not
     * obvious, and refusing is honest where guessing would not be.
     *
     * EMULATOR_IsPaused() is deliberately NOT part of this condition. A
     * plain QMP `stop` has no debug client attached to confuse, so a reset
     * from there stays allowed -- unlike memdump, there is no coherency
     * concern: system_reset does not read guest state, it just requests a
     * reset that the main thread will perform. */
    if (DEBUG_IsCpuPausedForDebug()) {
        send_error("GenericError",
                   "system_reset refused: the CPU is halted for debugging; "
                   "continue or detach the debug client first");
        return;
    }

    // Request reset (will be processed by main thread)
    EMULATOR_RequestReset(dos_only);

    // Send success immediately - reset happens asynchronously
    send_success();
}

void QMPServer::handle_query_status() {
    // Report both emulator state and debugger state separately
    bool emu_paused = EMULATOR_IsPaused();
    bool debug_active = DEBUG_IsDebuggerActive();
    bool debug_paused = DEBUG_IsCpuPausedForDebug();
    const char* debug_reason = DEBUG_GetDebuggerPauseReason();

    // Overall status: paused if either emulator or debugger has paused CPU
    std::string status = (emu_paused || debug_paused) ? "paused" : "running";
    bool running = !(emu_paused || debug_paused);

    std::ostringstream response;
    response << "{\"return\": {"
             << "\"status\": \"" << json_escape(status) << "\", "
             << "\"running\": " << (running ? "true" : "false") << ", "
             << "\"emulator-paused\": " << (emu_paused ? "true" : "false") << ", "
             << "\"debug\": {"
             << "\"active\": " << (debug_active ? "true" : "false") << ", "
             << "\"paused\": " << (debug_paused ? "true" : "false");
    if (debug_reason) {
        response << ", \"reason\": \"" << json_escape(debug_reason) << "\"";
    }
    response << "}}}\r\n";
    send_response(response.str());
}

void QMPServer::handle_debug_break_on_exec(const std::string& cmd) {
    // Set or clear the break-on-exec flag for debugging program entry points
    // When enabled, the debugger will break at the entry point of the next
    // program executed via DOS

    // Extract arguments
    std::string args_str;
    size_t args_pos = cmd.find("\"arguments\"");
    if (args_pos != std::string::npos) {
        size_t brace = cmd.find("{", args_pos);
        if (brace != std::string::npos) {
            int depth = 1;
            size_t end = brace + 1;
            while (end < cmd.size() && depth > 0) {
                if (cmd[end] == '{') depth++;
                else if (cmd[end] == '}') depth--;
                end++;
            }
            args_str = cmd.substr(brace, end - brace);
        }
    }

    // Get the enabled flag (defaults to true if not specified)
    bool enabled = extract_bool(args_str, "enabled", true);

    LOG(LOG_REMOTE, LOG_NORMAL)("QMP: debug-break-on-exec enabled=%s", enabled ? "true" : "false");

    // Set the flag in the debugger
    DEBUG_SetGDBBreakOnExec(enabled);

    // Return current state
    std::ostringstream response;
    response << "{\"return\": {\"enabled\": " << (enabled ? "true" : "false") << "}}\r\n";
    send_response(response.str());
}

// Public interface
void QMP_StartServer(int port) {
    if (qmpServer != nullptr) {
        LOG(LOG_REMOTE, LOG_WARN)("QMP: Server already running");
        return;
    }

    qmpServer = new QMPServer(port);
    qmpServer->start();
}

void QMP_StopServer() {
    if (qmpServer == nullptr) return;

    qmpServer->stop();
    delete qmpServer;
    qmpServer = nullptr;
}

bool QMP_IsServerRunning() {
    return qmpServer != nullptr && qmpServer->is_running();
}

void QMP_ProcessPendingInputEvents() {
    if (qmpServer != nullptr) {
        qmpServer->process_pending_input_events();
    }
}

#endif /* C_REMOTEDEBUG */
