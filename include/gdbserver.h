#ifndef DOSBOX_GDBSERVER_H
#define DOSBOX_GDBSERVER_H

#include "dosbox.h"

#if C_REMOTEDEBUG

#include <string>
#include <cstring>
#include <sstream>
#include <iomanip>
#include <sys/socket.h>
#include <netinet/in.h>
#include <arpa/inet.h>
#include <unistd.h>
#include <fcntl.h>

/* The largest reply body this stub will emit, and the value it advertises
 * as PacketSize in qSupported. The two must agree: a client is entitled to
 * size its buffers from the advertisement and reject anything longer. */
static const uint32_t GDB_MAX_PACKET_SIZE = 0x3fff;

/* `m` answers two hex digits per byte, so this is the most bytes a single
 * read can return without overrunning the advertised packet size. It is
 * also the ceiling on the length a client may request: before it existed,
 * an unbounded length from the wire sized a stack VLA in send_packet and
 * `m0,ffffff` crashed the emulator. */
static const uint32_t GDB_MAX_READ_BYTES = GDB_MAX_PACKET_SIZE / 2;

// Action requested by GDB client, returned from poll()
enum class GDBAction {
    NONE,           // No action needed, continue polling
    STEP,           // Execute single step
    CONTINUE,       // Continue execution until breakpoint
    STOP,           // GDB wants CPU stopped (halt reason query, Ctrl-C)
    DISCONNECT      // Client disconnected
};

class GDBServer {
public:
    GDBServer(int port) : port(port), server_fd(-1), client_fd(-1),
                          running(false), noack_mode(false) {}
    ~GDBServer() { stop(); }

    // Lifecycle
    void start();           // Setup listening socket (non-blocking)
    void stop();            // Close all sockets

    // Called from debug loop
    GDBAction poll();       // Check for clients/data, process commands, return action

    // State queries
    bool is_running() const { return running; }
    bool has_client() const { return client_fd >= 0; }

    // Called by debugger when execution stops (breakpoint, step complete, etc.)
    void send_stop_reply(int signal = 5);  // Default SIGTRAP

private:
    int port;
    int server_fd;
    int client_fd;
    bool running;
    bool noack_mode;
    std::string recv_buffer;  // Buffer for partial packet data

    // Socket setup
    void setup_socket();
    bool try_accept();      // Non-blocking accept, returns true if new client

    // Packet I/O (non-blocking)
    bool receive_data();    // Read available data into buffer, returns false on disconnect
    bool has_complete_packet() const;
    std::string extract_packet();  // Extract and remove one packet from buffer
    void send_packet(const std::string& packet);

    // Handshake
    bool perform_handshake();

    // Command processing - returns action for step/continue, NONE otherwise
    GDBAction process_command(const std::string& cmd);

    // GDB command handlers
    void handle_read_register(const std::string& cmd);
    void handle_read_registers();
    void handle_write_registers(const std::string& args);
    void handle_write_register(const std::string& args);
    void handle_read_memory(const std::string& args);
    void handle_write_memory(const std::string& args);
    void handle_breakpoint(const std::string& args);
    void handle_query(const std::string& args);
    GDBAction handle_v_packets(const std::string& cmd);

    // Helper functions
    std::string hex_encode(const std::string& input);
    std::string hex_decode(const std::string& input);
    static uint8_t hex_to_int(char c);
};

#endif /* C_REMOTEDEBUG */

#endif /* DOSBOX_GDBSERVER_H */
