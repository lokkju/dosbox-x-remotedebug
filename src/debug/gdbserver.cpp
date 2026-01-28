#include "dosbox.h"

#if C_REMOTEDEBUG

#include <errno.h>
#include "gdbserver.h"
#include "debug.h"
#include "logging.h"

// Helper functions
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
        if (errno == EAGAIN || errno == EWOULDBLOCK) {
            return false;  // No pending connection
        }
        LOG(LOG_REMOTE, LOG_ERROR)("GDBServer: accept failed: %s", strerror(errno));
        return false;
    }

    // Check for mutual exclusion with interactive debugger
    if (DEBUG_IsInteractiveDebuggerActive()) {
        LOG(LOG_REMOTE, LOG_WARN)("GDBServer: Rejecting connection - interactive debugger is active");
        const char* error_msg = "$E99#b2";
        send(new_fd, error_msg, strlen(error_msg), 0);
        close(new_fd);
        return false;
    }

    // Set client socket non-blocking
    int flags = fcntl(new_fd, F_GETFL, 0);
    fcntl(new_fd, F_SETFL, flags | O_NONBLOCK);

    client_fd = new_fd;
    recv_buffer.clear();
    noack_mode = false;

    LOG(LOG_REMOTE, LOG_NORMAL)("GDBServer: Client connected");
    return true;
}

GDBAction GDBServer::poll() {
    static int poll_count = 0;
    poll_count++;

    // Log every 10000 polls to show we're being called (LOG_NORMAL for visibility)
    if (poll_count % 10000 == 1) {
        LOG(LOG_REMOTE, LOG_NORMAL)("GDBServer: poll() called (count=%d, running=%d, server_fd=%d, client_fd=%d)",
                                    poll_count, running ? 1 : 0, server_fd, client_fd);
    }

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
            if (errno == EAGAIN || errno == EWOULDBLOCK) {
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
            write(client_fd, "-", 1);
        }
        return "";
    }

    // Send ACK
    if (!noack_mode) {
        write(client_fd, "+", 1);
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

    char response[packet.length() + 5];
    snprintf(response, sizeof(response), "$%s#%02x", packet.c_str(), checksum);

    write(client_fd, response, strlen(response));

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

    return GDBAction::NONE;
}

GDBAction GDBServer::handle_v_packets(const std::string& cmd) {
    if (cmd == "vCont?") {
        send_packet("vCont;c;s;t");
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
    // Parse hex string, 8 chars per register
    for (size_t i = 0; i < args.length() / 8; ++i) {
        std::string hex_val = args.substr(i * 8, 8);
        uint32_t value = std::stoul(hex_val, nullptr, 16);
        DEBUG_SetRegister(static_cast<int>(i), swap32(value));
    }
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

    std::stringstream ss;
    ss << std::hex << std::setfill('0');

    for (uint32_t i = 0; i < length; ++i) {
        uint8_t value = DEBUG_ReadMemory(address + i);
        ss << std::setw(2) << static_cast<int>(value);
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

    uint32_t address = std::stoul(args.substr(0, comma), nullptr, 16);
    std::string data = hex_decode(args.substr(colon + 1));

    for (size_t i = 0; i < data.length(); ++i) {
        DEBUG_WriteMemory(address + static_cast<uint32_t>(i), data[i]);
    }

    send_packet("OK");
}

void GDBServer::handle_breakpoint(const std::string& args) {
    LOG(LOG_REMOTE, LOG_NORMAL)("GDBServer: handle_breakpoint called with args='%s'", args.c_str());

    char type = args[0];
    size_t comma1 = args.find(',');
    size_t comma2 = args.find(',', comma1 + 1);
    if (comma1 == std::string::npos || comma2 == std::string::npos) {
        LOG(LOG_REMOTE, LOG_ERROR)("GDBServer: Breakpoint parse error - missing commas");
        send_packet("E01");
        return;
    }

    int bp_type = std::stoi(args.substr(1, comma1 - 1));
    uint32_t address = std::stoul(args.substr(comma1 + 1, comma2 - comma1 - 1), nullptr, 16);

    LOG(LOG_REMOTE, LOG_NORMAL)("GDBServer: Breakpoint type=%c, bp_type=%d, address=0x%x", type, bp_type, address);

    if (bp_type != 0) {  // Only software breakpoints supported
        LOG(LOG_REMOTE, LOG_WARN)("GDBServer: Non-software breakpoint type %d not supported", bp_type);
        send_packet("");
        return;
    }

    bool success;
    if (type == 'Z') {
        LOG(LOG_REMOTE, LOG_NORMAL)("GDBServer: Setting breakpoint at 0x%x", address);
        success = DEBUG_SetBreakpoint(address);
        LOG(LOG_REMOTE, LOG_NORMAL)("GDBServer: DEBUG_SetBreakpoint returned %s", success ? "true" : "false");
    } else {
        LOG(LOG_REMOTE, LOG_NORMAL)("GDBServer: Removing breakpoint at 0x%x", address);
        success = DEBUG_RemoveBreakpoint(address);
    }

    send_packet(success ? "OK" : "E01");
}

void GDBServer::handle_query(const std::string& cmd) {
    if (cmd.substr(0, 10) == "Supported:") {
        send_packet("PacketSize=3fff;swbreak+;hwbreak+;vContSupported+;QStartNoAckMode+");
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
