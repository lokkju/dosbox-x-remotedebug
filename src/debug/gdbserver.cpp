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

#include <errno.h>
#include <netinet/tcp.h>
#include <stdexcept>
#include "gdbserver.h"
#include "debug.h"
#include "logging.h"

// Helper functions

/* EAGAIN and EWOULDBLOCK are the same value on Linux, so testing both with ||
 * trips -Wlogical-op. They are permitted to differ, so both are still
 * checked, just not in one expression. */
static inline bool would_block(int err) {
    if (err == EAGAIN) return true;
#if EWOULDBLOCK != EAGAIN
    if (err == EWOULDBLOCK) return true;
#endif
    return false;
}

/* The GDB stub is best-effort on the write side: a short write or a peer that
 * vanished mid-packet is handled by the client timing out and reconnecting,
 * not by retrying here. Swallow the result explicitly so the warning does not
 * hide a real one. */
static inline void write_ignore(int fd, const void* buf, size_t len) {
    ssize_t unused = write(fd, buf, len);
    (void)unused;
}

static inline uint32_t swap32(uint32_t x) {
    return (((x >> 24) & 0x000000ff) |
            ((x >> 8) & 0x0000ff00) |
            ((x << 8) & 0x00ff0000) |
            ((x << 24) & 0xff000000));
}

void GDBServer::start() {
    if (running) {
        LOG(LOG_REMOTE, LOG_WARN)("GDBServer: Already running");
        return;
    }
    setup_socket();
    running = true;
}

void GDBServer::stop() {
    if (!running) return;

    LOG(LOG_REMOTE, LOG_NORMAL)("GDBServer: Stopping...");
    running = false;

    if (client_fd >= 0) {
        close(client_fd);
        client_fd = -1;
    }
    if (server_fd >= 0) {
        close(server_fd);
        server_fd = -1;
    }
    recv_buffer.clear();
    /* A new connection is a new session. RSP has no way to resume one,
     * so QStartNoAckMode must be renegotiated after a reconnect; this
     * reset is deliberate, not an oversight. Recorded because the
     * failure mode is silent -- a client that assumes acks stayed off
     * desyncs the framing instead of getting an error.
     * The other reset sites are try_accept() and poll(). */
    noack_mode = false;
}

void GDBServer::setup_socket() {
    struct sockaddr_in address;
    int opt = 1;

    server_fd = socket(AF_INET, SOCK_STREAM, 0);
    if (server_fd < 0) {
        LOG(LOG_REMOTE, LOG_ERROR)("GDBServer: socket failed: %s", strerror(errno));
        return;
    }

    if (setsockopt(server_fd, SOL_SOCKET, SO_REUSEADDR | SO_REUSEPORT, &opt, sizeof(opt)) < 0) {
        LOG(LOG_REMOTE, LOG_ERROR)("GDBServer: setsockopt failed: %s", strerror(errno));
        close(server_fd);
        server_fd = -1;
        return;
    }

    // Set non-blocking
    int flags = fcntl(server_fd, F_GETFL, 0);
    fcntl(server_fd, F_SETFL, flags | O_NONBLOCK);

    address.sin_family = AF_INET;
    address.sin_addr.s_addr = INADDR_ANY;
    address.sin_port = htons(port);

    if (bind(server_fd, (struct sockaddr*)&address, sizeof(address)) < 0) {
        LOG(LOG_REMOTE, LOG_ERROR)("GDBServer: bind failed: %s", strerror(errno));
        close(server_fd);
        server_fd = -1;
        return;
    }

    if (listen(server_fd, 1) < 0) {
        LOG(LOG_REMOTE, LOG_ERROR)("GDBServer: listen failed: %s", strerror(errno));
        close(server_fd);
        server_fd = -1;
        return;
    }

    LOG(LOG_REMOTE, LOG_NORMAL)("GDBServer: Listening on port %d", port);
}

bool GDBServer::try_accept() {
    if (server_fd < 0) return false;

    struct sockaddr_in address;
    socklen_t addrlen = sizeof(address);

    int new_fd = accept(server_fd, (struct sockaddr*)&address, &addrlen);
    if (new_fd < 0) {
        if (would_block(errno)) {
            return false;  // No pending connection
        }
        LOG(LOG_REMOTE, LOG_ERROR)("GDBServer: accept failed: %s", strerror(errno));
        return false;
    }

    // Check for mutual exclusion with interactive debugger
    if (DEBUG_IsInteractiveDebuggerActive()) {
        LOG(LOG_REMOTE, LOG_WARN)("GDBServer: Rejecting connection - interactive debugger is active");
        // Checksum is the low byte of the sum of the packet body: 'E'+'9'+'9'
        // == 0xb7. This is the only hand-written packet in the file -- every
        // other reply goes through send_packet(), which computes it.
        const char* error_msg = "$E99#b7";
        send(new_fd, error_msg, strlen(error_msg), 0);
        close(new_fd);
        return false;
    }

    /* The RSP is strictly request/response with tiny packets, which is the
     * workload Nagle punishes worst: each side holds a small write waiting for
     * an ACK the peer has delayed, costing ~40ms per direction. Measured
     * ~82ms per round-trip with it on, ~41ms with only the client fixed, so
     * both ends have to set it. Cost is per round-trip regardless of size, so
     * it falls entirely on trip count. */
    int nodelay = 1;
    setsockopt(new_fd, IPPROTO_TCP, TCP_NODELAY, &nodelay, sizeof(nodelay));

    // Set client socket non-blocking
    int flags = fcntl(new_fd, F_GETFL, 0);
    fcntl(new_fd, F_SETFL, flags | O_NONBLOCK);

    client_fd = new_fd;
    recv_buffer.clear();
    /* A new connection is a new session: the client renegotiates
     * QStartNoAckMode. See the note in stop(). */
    noack_mode = false;

    LOG(LOG_REMOTE, LOG_NORMAL)("GDBServer: Client connected");
    return true;
}

GDBAction GDBServer::poll() {
    if (!running) return GDBAction::NONE;

    // Try to accept new client if we don't have one
    if (client_fd < 0) {
        if (try_accept()) {
            // New client connected, wait for handshake
            // The handshake will happen on subsequent poll() calls
        }
        return GDBAction::NONE;
    }

    // Read any available data
    if (!receive_data()) {
        // Client disconnected
        LOG(LOG_REMOTE, LOG_NORMAL)("GDBServer: Client disconnected");
        close(client_fd);
        client_fd = -1;
        recv_buffer.clear();
        /* Session over; the next client starts in ACK mode. See stop(). */
        noack_mode = false;
        return GDBAction::DISCONNECT;
    }

    // Process complete packets
    while (has_complete_packet()) {
        std::string packet = extract_packet();
        if (packet.empty()) continue;

        GDBAction action = process_command(packet);
        if (action != GDBAction::NONE) {
            return action;
        }
    }

    return GDBAction::NONE;
}

bool GDBServer::receive_data() {
    char buf[1024];
    while (true) {
        ssize_t n = read(client_fd, buf, sizeof(buf));
        if (n > 0) {
            recv_buffer.append(buf, n);
        } else if (n == 0) {
            // Connection closed
            return false;
        } else {
            if (would_block(errno)) {
                // No more data available
                return true;
            }
            // Real error
            LOG(LOG_REMOTE, LOG_ERROR)("GDBServer: read error: %s", strerror(errno));
            return false;
        }
    }
}

bool GDBServer::has_complete_packet() const {
    // Check for Ctrl-C (0x03)
    if (!recv_buffer.empty() && recv_buffer[0] == 0x03) {
        return true;
    }

    // Check for complete packet: $...#xx
    size_t start = recv_buffer.find('$');
    if (start == std::string::npos) {
        return false;
    }

    size_t hash = recv_buffer.find('#', start);
    if (hash == std::string::npos) {
        return false;
    }

    // Need 2 more chars for checksum
    return recv_buffer.length() >= hash + 3;
}

std::string GDBServer::extract_packet() {
    // Handle Ctrl-C (interrupt)
    if (!recv_buffer.empty() && recv_buffer[0] == 0x03) {
        recv_buffer.erase(0, 1);
        LOG(LOG_REMOTE, LOG_DEBUG)("GDBServer: Received interrupt (Ctrl-C)");
        return "\x03";  // Return special marker
    }

    // Find packet boundaries
    size_t start = recv_buffer.find('$');
    if (start == std::string::npos) {
        recv_buffer.clear();  // Discard garbage
        return "";
    }

    // Discard anything before '$'
    if (start > 0) {
        recv_buffer.erase(0, start);
        start = 0;
    }

    size_t hash = recv_buffer.find('#', start);
    if (hash == std::string::npos || recv_buffer.length() < hash + 3) {
        return "";  // Incomplete
    }

    // Extract packet content (between $ and #)
    std::string packet = recv_buffer.substr(start + 1, hash - start - 1);

    // Extract and verify checksum
    std::string checksum_str = recv_buffer.substr(hash + 1, 2);
    uint8_t received_checksum = (hex_to_int(checksum_str[0]) << 4) | hex_to_int(checksum_str[1]);

    uint8_t calculated_checksum = 0;
    for (char c : packet) {
        calculated_checksum += static_cast<uint8_t>(c);
    }

    // Remove packet from buffer
    recv_buffer.erase(0, hash + 3);

    if (received_checksum != calculated_checksum) {
        LOG(LOG_REMOTE, LOG_WARN)("GDBServer: Checksum mismatch! received 0x%02x, calculated 0x%02x",
                                  received_checksum, calculated_checksum);
        if (!noack_mode) {
            write_ignore(client_fd, "-", 1);
        }
        return "";
    }

    // Send ACK
    if (!noack_mode) {
        write_ignore(client_fd, "+", 1);
    }

    LOG(LOG_REMOTE, LOG_DEBUG)("GDBServer: << %s", packet.c_str());
    return packet;
}

void GDBServer::send_packet(const std::string& packet) {
    if (client_fd < 0) return;

    LOG(LOG_REMOTE, LOG_DEBUG)("GDBServer: >> %s", packet.c_str());

    uint8_t checksum = 0;
    for (char c : packet) {
        checksum += static_cast<uint8_t>(c);
    }

    /* This used to be `char response[packet.length() + 5]` -- a stack VLA
     * whose size came straight off the wire. handle_read_memory took an
     * unbounded byte count from the client and answered two hex digits per
     * byte, so `m0,ffffff` asked for a ~32MB stack frame and the emulator
     * died with SIGSEGV. A std::string has no such ceiling; the requested
     * length is bounded separately in handle_read_memory. */
    char checksum_text[4];
    snprintf(checksum_text, sizeof(checksum_text), "%02x", checksum);
    const std::string response = "$" + packet + "#" + checksum_text;

    write_ignore(client_fd, response.data(), response.size());

    // In non-blocking mode, we don't wait for ACK synchronously
    // The ACK will be in recv_buffer on next poll()
    // For simplicity, we just ignore ACKs (they're discarded in extract_packet)
}

void GDBServer::send_stop_reply(int signal) {
    char reply[8];
    snprintf(reply, sizeof(reply), "S%02x", signal);
    send_packet(reply);
}

GDBAction GDBServer::process_command(const std::string& cmd) {
    // Handle Ctrl-C interrupt
    if (cmd == "\x03") {
        LOG(LOG_REMOTE, LOG_NORMAL)("GDBServer: Ctrl-C received, stopping CPU");
        send_stop_reply(5);  // SIGTRAP
        return GDBAction::STOP;  // Tell debug.cpp to pause CPU
    }

    /* Malformed packet input (bad hex, missing separators, etc.) makes
     * handle_breakpoint / handle_read_memory / handle_write_memory /
     * handle_write_registers / handle_write_register throw
     * std::invalid_argument or std::out_of_range out of std::stoul/std::stoi.
     * This runs on the main emulation thread, so an uncaught throw here
     * would call std::terminate and take the whole emulator down instead of
     * just dropping the debug connection. One catch around the whole
     * dispatch covers every handler; per-handler try/catch would be
     * redundant. */
    try {
        if (cmd == "QStartNoAckMode") {
            noack_mode = true;
            send_packet("OK");
        } else if (cmd == "vMustReplyEmpty") {
            send_packet("");
        } else if (cmd == "?") {
            // Query halt reason - GDB wants us stopped
            LOG(LOG_REMOTE, LOG_NORMAL)("GDBServer: Halt reason query, stopping CPU");
            send_stop_reply(5);  // SIGTRAP
            return GDBAction::STOP;  // Tell debug.cpp to pause CPU
        } else if (cmd.substr(0, 1) == "H") {
            send_packet("OK");
        } else if (cmd.substr(0, 1) == "p") {
            handle_read_register(cmd);
        } else if (cmd.substr(0, 1) == "P") {
            handle_write_register(cmd.substr(1));
        } else if (cmd == "g") {
            handle_read_registers();
        } else if (cmd.substr(0, 1) == "G") {
            handle_write_registers(cmd.substr(1));
        } else if (cmd.substr(0, 1) == "m") {
            handle_read_memory(cmd.substr(1));
        } else if (cmd.substr(0, 1) == "M") {
            handle_write_memory(cmd.substr(1));
        } else if (cmd.substr(0, 1) == "Z" || cmd.substr(0, 1) == "z") {
            handle_breakpoint(cmd);
        } else if (cmd == "s" || cmd.substr(0, 1) == "s") {
            // Step - return action, debugger will call send_stop_reply() when done
            return GDBAction::STEP;
        } else if (cmd == "c" || cmd.substr(0, 1) == "c") {
            // Continue - return action, debugger will call send_stop_reply() on breakpoint
            return GDBAction::CONTINUE;
        } else if (cmd.substr(0, 1) == "q") {
            handle_query(cmd.substr(1));
        } else if (cmd.substr(0, 5) == "vCont") {
            return handle_v_packets(cmd);
        } else if (cmd == "D" || cmd.substr(0, 2) == "D;") {
            LOG(LOG_REMOTE, LOG_NORMAL)("GDBServer: Client detaching");
            send_packet("OK");
            close(client_fd);
            client_fd = -1;
            return GDBAction::DISCONNECT;
        } else {
            LOG(LOG_REMOTE, LOG_DEBUG)("GDBServer: Unhandled command: %s", cmd.c_str());
            send_packet("");
        }
    } catch (const std::exception& e) {
        LOG(LOG_REMOTE, LOG_ERROR)("GDBServer: exception handling command '%s': %s",
                                    cmd.c_str(), e.what());
        send_packet("E01");
    }

    return GDBAction::NONE;
}

GDBAction GDBServer::handle_v_packets(const std::string& cmd) {
    if (cmd == "vCont?") {
        /* Only the actions the switch below dispatches. `t` was advertised
         * without a handler, so a client that took the advertisement at
         * face value got the empty "unsupported" reply and stalled. */
        send_packet("vCont;c;s");
        return GDBAction::NONE;
    }

    if (cmd.length() >= 7 && cmd.substr(0, 6) == "vCont;") {
        char action = cmd[6];
        switch (action) {
            case 'c':
                return GDBAction::CONTINUE;
            case 's':
                return GDBAction::STEP;
            default:
                send_packet("");
                return GDBAction::NONE;
        }
    }

    send_packet("");
    return GDBAction::NONE;
}

void GDBServer::handle_read_register(const std::string& cmd) {
    int reg_num = std::stoi(cmd.substr(1), nullptr, 16);
    /* DEBUG_GetRegister's `default: return 0` makes an out-of-range index
     * answer 00000000, which a client cannot tell from a register that
     * genuinely holds zero. P has always validated 0..15; p and G did not. */
    if (reg_num < 0 || reg_num > 15) {
        send_packet("E01");
        return;
    }
    uint32_t value = DEBUG_GetRegister(reg_num);

    std::stringstream ss;
    ss << std::hex << std::setfill('0') << std::setw(8) << swap32(value);
    send_packet(ss.str());
}

void GDBServer::handle_read_registers() {
    std::stringstream ss;
    ss << std::hex << std::setfill('0');

    // x86 32-bit register order: EAX, ECX, EDX, EBX, ESP, EBP, ESI, EDI, EIP, EFLAGS, CS, SS, DS, ES, FS, GS
    const int reg_count = 16;
    for (int i = 0; i < reg_count; ++i) {
        uint32_t value = DEBUG_GetRegister(i);
        ss << std::setw(8) << swap32(value);
    }

    send_packet(ss.str());
}

void GDBServer::handle_write_registers(const std::string& args) {
    /* Sixteen registers of eight hex digits each, and nothing else. The
     * loop used to run over whatever the client sent: a ragged tail was
     * dropped, and indices past 15 fell through DEBUG_SetRegister's switch
     * and were discarded -- with OK either way. */
    const size_t reg_count = 16;
    if (args.length() % 8 != 0 || args.length() / 8 > reg_count) {
        send_packet("E01");
        return;
    }

    // Parse hex string, 8 chars per register
    for (size_t i = 0; i < args.length() / 8; ++i) {
        std::string hex_val = args.substr(i * 8, 8);
        uint32_t value = std::stoul(hex_val, nullptr, 16);
        DEBUG_SetRegister(static_cast<int>(i), swap32(value));
    }
    send_packet("OK");
}

void GDBServer::handle_write_register(const std::string& args) {
    size_t eq = args.find('=');
    if (eq == std::string::npos) {
        send_packet("E01");
        return;
    }
    int reg = (int)std::stoul(args.substr(0, eq), nullptr, 16);
    /* DEBUG_SetRegister's switch has no default case, so an out-of-range
     * index is silently discarded and would otherwise still get "OK". */
    if (reg < 0 || reg > 15) {
        send_packet("E01");
        return;
    }
    uint32_t value = (uint32_t)std::stoul(args.substr(eq + 1), nullptr, 16);
    DEBUG_SetRegister(reg, swap32(value));
    send_packet("OK");
}

void GDBServer::handle_read_memory(const std::string& args) {
    size_t comma = args.find(',');
    if (comma == std::string::npos) {
        send_packet("E01");
        return;
    }

    uint32_t address = std::stoul(args.substr(0, comma), nullptr, 16);
    uint32_t length = std::stoul(args.substr(comma + 1), nullptr, 16);

    /* The reply is two hex digits per byte, so the largest read that still
     * fits inside the PacketSize this stub advertises in qSupported is
     * GDB_MAX_PACKET_SIZE / 2. Refusing anything larger keeps the reply
     * within what the client was promised, and -- with the VLA in
     * send_packet gone -- keeps a client-chosen length from sizing anything
     * unbounded. gdb itself splits long reads into PacketSize-sized chunks,
     * so a conforming client never trips this. */
    if (length > GDB_MAX_READ_BYTES) {
        LOG(LOG_REMOTE, LOG_WARN)("GDBServer: m rejected: %u bytes exceeds the "
                                  "%u byte limit implied by PacketSize",
                                  (unsigned)length, (unsigned)GDB_MAX_READ_BYTES);
        send_packet("E01");
        return;
    }

    std::stringstream ss;
    ss << std::hex << std::setfill('0');

    /* Read until the first byte that cannot be read, then stop. RSP allows
     * a reply shorter than the requested length, and gdb treats the short
     * reply as "readable memory ends here" -- which is the whole point.
     * Reporting E01 only when nothing at all could be read keeps every
     * client that reads mapped memory working exactly as before, while no
     * longer passing a fabricated 0 off as guest state. */
    uint32_t read = 0;
    for (; read < length; ++read) {
        uint8_t value;
        if (!DEBUG_ReadMemory(address + read, &value)) break;
        ss << std::setw(2) << static_cast<int>(value);
    }

    if (read == 0 && length != 0) {
        send_packet("E01");
        return;
    }

    send_packet(ss.str());
}

void GDBServer::handle_write_memory(const std::string& args) {
    size_t comma = args.find(',');
    size_t colon = args.find(':');
    if (comma == std::string::npos || colon == std::string::npos) {
        send_packet("E01");
        return;
    }

    if (colon < comma) {
        send_packet("E01");
        return;
    }

    uint32_t address = std::stoul(args.substr(0, comma), nullptr, 16);
    uint32_t length = std::stoul(args.substr(comma + 1, colon - comma - 1),
                                 nullptr, 16);
    const std::string payload = args.substr(colon + 1);

    /* M carries the byte count twice: as `length`, and as the width of the
     * payload. They must agree. `length` used to be parsed off the wire and
     * never read, so a short payload wrote fewer bytes than the client asked
     * for and a long one wrote past the region it asked for -- both with OK.
     * Two hex digits per byte, so an odd-width payload is malformed too;
     * hex_decode drops its trailing nibble. */
    if (payload.length() != static_cast<size_t>(length) * 2) {
        send_packet("E01");
        return;
    }

    const std::string data = hex_decode(payload);

    for (size_t i = 0; i < data.length(); ++i) {
        if (!DEBUG_WriteMemory(address + static_cast<uint32_t>(i),
                               static_cast<uint8_t>(data[i]))) {
            /* Bytes before this one already landed. RSP has no way to say
             * how far a partial write got, so report the failure and let the
             * client re-read the region. */
            send_packet("E01");
            return;
        }
    }

    send_packet("OK");
}

void GDBServer::handle_breakpoint(const std::string& args) {
    char type = args[0];
    size_t comma1 = args.find(',');
    size_t comma2 = args.find(',', comma1 + 1);
    if (comma1 == std::string::npos || comma2 == std::string::npos) {
        send_packet("E01");
        return;
    }

    int bp_type = std::stoi(args.substr(1, comma1 - 1));
    uint32_t address = std::stoul(args.substr(comma1 + 1, comma2 - comma1 - 1), nullptr, 16);

    if (bp_type != 0) {  // Only software breakpoints supported
        send_packet("");
        return;
    }

    bool success;
    if (type == 'Z') {
        success = DEBUG_SetBreakpoint(address);
    } else {
        success = DEBUG_RemoveBreakpoint(address);
    }

    send_packet(success ? "OK" : "E01");
}

void GDBServer::handle_query(const std::string& cmd) {
    if (cmd.substr(0, 10) == "Supported:") {
        /* Two vendor features naming the two semantics this build fixed.
         * dosbox-x-linear-bp+ : Z0/z0 take a LINEAR address, as the
         *   protocol specifies, not the packed far pointer older builds
         *   expected. Needed because Z0 answers OK under either reading.
         * dosbox-x-eip-offset+ : register 8 is EIP, an offset within CS,
         *   not SegPhys(cs)+reg_eip. Needed because either interpretation
         *   yields a plausible-looking number.
         * Real gdb ignores features it does not recognise, so both stay
         * RSP-legal. */
        /* swbreak+ and hwbreak+ are deliberately NOT advertised. In
         * qSupported they promise that stop replies carry a swbreak: or
         * hwbreak: annotation saying why the stub stopped; this stub only
         * ever sends a bare S05. hwbreak+ was doubly false because
         * handle_breakpoint refuses Z1-Z4 outright. Advertising less is
         * always safe; add either one back only together with the
         * annotation it promises. */
        /* PacketSize is derived from the same constant handle_read_memory
         * enforces, so the advertisement and the limit cannot drift apart. */
        std::stringstream supported;
        supported << "PacketSize=" << std::hex << GDB_MAX_PACKET_SIZE
                  << ";vContSupported+;QStartNoAckMode+;dosbox-x-linear-bp+;"
                     "dosbox-x-eip-offset+";
        send_packet(supported.str());
    } else if (cmd.substr(0, 11) == "fThreadInfo") {
        send_packet("m1");
    } else if (cmd.substr(0, 11) == "sThreadInfo") {
        send_packet("l");
    } else if (cmd.substr(0, 8) == "Attached") {
        send_packet("1");
    } else if (cmd == "C") {
        send_packet("");
    } else {
        send_packet("");
    }
}

std::string GDBServer::hex_encode(const std::string& input) {
    std::stringstream ss;
    ss << std::hex << std::setfill('0');
    for (unsigned char c : input) {
        ss << std::setw(2) << static_cast<int>(c);
    }
    return ss.str();
}

std::string GDBServer::hex_decode(const std::string& input) {
    std::string output;
    for (size_t i = 0; i + 1 < input.length(); i += 2) {
        uint8_t byte = (hex_to_int(input[i]) << 4) | hex_to_int(input[i + 1]);
        output.push_back(static_cast<char>(byte));
    }
    return output;
}

uint8_t GDBServer::hex_to_int(char c) {
    if (c >= '0' && c <= '9') return c - '0';
    if (c >= 'a' && c <= 'f') return c - 'a' + 10;
    if (c >= 'A' && c <= 'F') return c - 'A' + 10;
    return 0;
}

#endif /* C_REMOTEDEBUG */
