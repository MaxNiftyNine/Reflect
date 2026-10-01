// Android TCP redirect to a loopback HTTP CONNECT proxy. Runs as root, never as the app UID.
#include <arpa/inet.h>
#include <sys/socket.h>
#include <poll.h>
#include <unistd.h>
#include <signal.h>
#include <cerrno>
#include <cstdio>
#include <string>
#include <thread>

static bool send_all(int fd, const char* data, size_t size) {
    while (size) {
        auto n = send(fd, data, size, MSG_NOSIGNAL);
        if (n < 0 && errno == EINTR) continue;
        if (n <= 0) return false;
        data += n; size -= n;
    }
    return true;
}

static void relay(int client, int family) {
    int upstream = -1;
    auto work = [&]() {
        sockaddr_storage dst{}; socklen_t length = sizeof(dst);
        if (getsockopt(client, family == AF_INET ? SOL_IP : SOL_IPV6, 80, &dst, &length)) return;
        char ip[INET6_ADDRSTRLEN]; unsigned port;
        std::string host;
        if (family == AF_INET) {
            auto* a = reinterpret_cast<sockaddr_in*>(&dst);
            if (!inet_ntop(AF_INET, &a->sin_addr, ip, sizeof(ip))) return;
            host = ip; port = ntohs(a->sin_port);
        } else {
            auto* a = reinterpret_cast<sockaddr_in6*>(&dst);
            if (!inet_ntop(AF_INET6, &a->sin6_addr, ip, sizeof(ip))) return;
            host = std::string("[") + ip + "]"; port = ntohs(a->sin6_port);
        }
        upstream = socket(AF_INET, SOCK_STREAM | SOCK_CLOEXEC, 0);
        if (upstream < 0) return;
        timeval timeout{30, 0};
        setsockopt(upstream, SOL_SOCKET, SO_SNDTIMEO, &timeout, sizeof(timeout));
        setsockopt(upstream, SOL_SOCKET, SO_RCVTIMEO, &timeout, sizeof(timeout));
        setsockopt(client, SOL_SOCKET, SO_SNDTIMEO, &timeout, sizeof(timeout));
        sockaddr_in proxy{}; proxy.sin_family = AF_INET; proxy.sin_port = htons(38492);
        proxy.sin_addr.s_addr = htonl(INADDR_LOOPBACK);
        if (connect(upstream, reinterpret_cast<sockaddr*>(&proxy), sizeof(proxy))) return;
        auto authority = host + ":" + std::to_string(port);
        auto connect_request = "CONNECT " + authority + " HTTP/1.1\r\nHost: " + authority + "\r\n\r\n";
        if (!send_all(upstream, connect_request.data(), connect_request.size())) return;
        std::string response;
        while (response.size() < 8192 && (response.size() < 4 || response.substr(response.size()-4) != "\r\n\r\n")) {
            char c; if (recv(upstream, &c, 1, 0) != 1) return; response += c;
        }
        auto space = response.find(' ');
        if (space == std::string::npos || response.substr(space + 1, 3) != "200" || response.substr(response.size()-4) != "\r\n\r\n") return;
        pollfd sockets[2]{{client, POLLIN, 0}, {upstream, POLLIN, 0}};
        int open = 2;
        while (open) {
            auto result = poll(sockets, 2, 300000);
            if (result < 0 && errno == EINTR) continue;
            if (result <= 0) return;
            for (int i = 0; i < 2; ++i) {
                if (!sockets[i].revents || sockets[i].fd < 0) continue;
                if (sockets[i].revents & (POLLERR | POLLNVAL)) return;
                char buffer[65536]; auto n = recv(sockets[i].fd, buffer, sizeof(buffer), 0);
                int other = i ? client : upstream;
                if (n <= 0) {
                    shutdown(other, SHUT_WR); sockets[i].fd = -1; --open;
                } else if (!send_all(other, buffer, n)) return;
            }
        }
    };
    work(); close(client); if (upstream >= 0) close(upstream);
}

int main() {
    signal(SIGPIPE, SIG_IGN);
    pollfd listeners[2]{};
    for (int i = 0; i < 2; ++i) {
        int family = i ? AF_INET6 : AF_INET;
        int fd = socket(family, SOCK_STREAM | SOCK_CLOEXEC, 0);
        if (fd < 0) { perror("socket"); return 1; }
        int yes = 1; setsockopt(fd, SOL_SOCKET, SO_REUSEADDR, &yes, sizeof(yes));
        if (i) setsockopt(fd, IPPROTO_IPV6, IPV6_V6ONLY, &yes, sizeof(yes));
        sockaddr_storage address{}; socklen_t length;
        if (i) {
            auto* a = reinterpret_cast<sockaddr_in6*>(&address);
            a->sin6_family = family; a->sin6_port = htons(38500); a->sin6_addr = in6addr_loopback; length = sizeof(*a);
        } else {
            auto* a = reinterpret_cast<sockaddr_in*>(&address);
            a->sin_family = family; a->sin_port = htons(38500); a->sin_addr.s_addr = htonl(INADDR_LOOPBACK); length = sizeof(*a);
        }
        if (bind(fd, reinterpret_cast<sockaddr*>(&address), length) || listen(fd, 128)) { perror("listen"); return 1; }
        listeners[i] = {fd, POLLIN, 0};
    }
    puts("Reflect HTTP relay ready"); fflush(stdout);
    for (;;) {
        if (poll(listeners, 2, -1) < 0) { if (errno == EINTR) continue; return 1; }
        for (int i = 0; i < 2; ++i) if (listeners[i].revents & POLLIN) {
            int client = accept4(listeners[i].fd, nullptr, nullptr, SOCK_CLOEXEC);
            if (client >= 0) std::thread(relay, client, i ? AF_INET6 : AF_INET).detach();
        }
    }
}
