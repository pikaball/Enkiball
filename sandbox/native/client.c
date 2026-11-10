#define _POSIX_C_SOURCE 200809L
#include <errno.h>
#include <signal.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/socket.h>
#include <sys/un.h>
#include <unistd.h>

/* Protocol v1: one JSON request and one JSON response per connection. */
static int write_all(int fd, const char *p, size_t n) {
    while (n) {
        ssize_t k = write(fd, p, n);
        if (k < 0) { if (errno == EINTR) continue; return -1; }
        p += k; n -= (size_t)k;
    }
    return 0;
}
int main(int argc, char **argv) {
    const char *path = getenv("ENKISANDBOX_SOCKET");
    if (!path) path = "/run/enkisandbox/control.sock";
    if (argc == 2 && strcmp(argv[1], "--version") == 0) {
        puts("enkisandbox-client 1"); return 0;
    }
    if (argc == 3 && strcmp(argv[1], "--socket") == 0) path = argv[2];
    else if (argc != 1) {
        fprintf(stderr, "usage: enkisandbox-client [--socket PATH] < request.json\n"); return 64;
    }
    struct sockaddr_un addr = {0};
    addr.sun_family = AF_UNIX;
    if (strlen(path) >= sizeof addr.sun_path) { fputs("socket path too long\n", stderr); return 64; }
    strcpy(addr.sun_path, path);
    signal(SIGPIPE, SIG_IGN);
    int fd = socket(AF_UNIX, SOCK_STREAM, 0);
    if (fd < 0 || connect(fd, (struct sockaddr *)&addr, sizeof addr) < 0) {
        perror("enkisandbox connection"); if (fd >= 0) close(fd); return 69;
    }
    char buf[65536]; size_t total = 0; ssize_t n;
    while ((n = read(STDIN_FILENO, buf, sizeof buf)) != 0) {
        if (n < 0) { if (errno == EINTR) continue; perror("stdin"); close(fd); return 74; }
        total += (size_t)n;
        if (total > 48 * 1024 * 1024 || write_all(fd, buf, (size_t)n)) { fputs("request failed\n", stderr); close(fd); return 74; }
    }
    shutdown(fd, SHUT_WR);
    while ((n = read(fd, buf, sizeof buf)) != 0) {
        if (n < 0) { if (errno == EINTR) continue; perror("response"); close(fd); return 74; }
        if (write_all(STDOUT_FILENO, buf, (size_t)n)) { close(fd); return 74; }
    }
    close(fd); return 0;
}
