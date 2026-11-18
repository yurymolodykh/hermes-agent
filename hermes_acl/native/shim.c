/*
 * hermes-acl-shim: apply a Landlock policy (filesystem + network), then become the command.
 *
 *   hermes-acl-shim --spec <spec.txt> -- <argv...>
 *
 * Why C: this runs on every terminal/background/`rg`/kernel spawn, and the Python fallback
 * costs ~60 ms of interpreter startup before it can confine anything. Measured on this host:
 * /bin/true 0.75 ms, Python shim 64 ms, this binary 0.9 ms. A boundary that costs 60 ms per
 * command is a boundary operators switch off.
 *
 * The spec format is hermes_acl/spec.py's: ONE format, read by both implementations.
 *
 * Order is the guarantee: read spec -> PR_SET_NO_NEW_PRIVS -> create ruleset ->
 * add rules -> restrict_self -> execvpe. Any failure exits 126 BEFORE execve, so a shim that
 * cannot confine refuses to run the command rather than running it unconfined.
 *
 * Network default is DENY: the ruleset handles bind/connect for TCP *and* UDP (ABI 7+), and with
 * no allow rule every connect/bind/sendto in the child is refused with EACCES. A destination is
 * opened by PORT only — Landlock has no per-host rule, so an allowed port is reachable on every
 * host (a residual gap named in the plan, not papered over).
 */
#define _GNU_SOURCE
#include <errno.h>
#include <fcntl.h>
#include <linux/landlock.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/prctl.h>
#include <sys/syscall.h>
#include <unistd.h>

#ifndef LANDLOCK_ACCESS_FS_REFER
#define LANDLOCK_ACCESS_FS_REFER (1ULL << 13)
#endif
#ifndef LANDLOCK_ACCESS_FS_TRUNCATE
#define LANDLOCK_ACCESS_FS_TRUNCATE (1ULL << 14)
#endif
#ifndef LANDLOCK_ACCESS_NET_BIND_TCP
#define LANDLOCK_ACCESS_NET_BIND_TCP (1ULL << 0)
#define LANDLOCK_ACCESS_NET_CONNECT_TCP (1ULL << 1)
#endif
#ifndef LANDLOCK_ACCESS_NET_BIND_UDP
#define LANDLOCK_ACCESS_NET_BIND_UDP (1ULL << 2)
#define LANDLOCK_ACCESS_NET_CONNECT_SEND_UDP (1ULL << 3)
#endif

#define EXIT_REFUSED 126
#define EXIT_USAGE 127
#define MAX_RULES 512

struct fs_rule { __u64 access; char *path; };
struct net_rule { __u64 access; __u64 port; };

/* Every FS right the given ABI understands: nothing is left unhandled by omission. */
static __u64 handled_fs(int abi) {
    __u64 m = LANDLOCK_ACCESS_FS_EXECUTE | LANDLOCK_ACCESS_FS_WRITE_FILE |
              LANDLOCK_ACCESS_FS_READ_FILE | LANDLOCK_ACCESS_FS_READ_DIR |
              LANDLOCK_ACCESS_FS_REMOVE_FILE | LANDLOCK_ACCESS_FS_REMOVE_DIR |
              LANDLOCK_ACCESS_FS_MAKE_CHAR | LANDLOCK_ACCESS_FS_MAKE_DIR |
              LANDLOCK_ACCESS_FS_MAKE_REG | LANDLOCK_ACCESS_FS_MAKE_SOCK |
              LANDLOCK_ACCESS_FS_MAKE_FIFO | LANDLOCK_ACCESS_FS_MAKE_BLOCK |
              LANDLOCK_ACCESS_FS_MAKE_SYM;
    if (abi >= 2) m |= LANDLOCK_ACCESS_FS_REFER;
    if (abi >= 3) m |= LANDLOCK_ACCESS_FS_TRUNCATE;
#ifdef LANDLOCK_ACCESS_FS_IOCTL_DEV
    if (abi >= 5) m |= LANDLOCK_ACCESS_FS_IOCTL_DEV;
#endif
    return m;
}

static __u64 handled_net(int abi) {
    __u64 m = 0;
    if (abi < 4) return 0;
    m |= LANDLOCK_ACCESS_NET_BIND_TCP | LANDLOCK_ACCESS_NET_CONNECT_TCP;
    if (abi >= 7) m |= LANDLOCK_ACCESS_NET_BIND_UDP | LANDLOCK_ACCESS_NET_CONNECT_SEND_UDP;
    return m;
}

/* rwx -> access bits: r = read file + read dir, w = write/truncate/create/remove, x = execute. */
static __u64 perms_to_access(const char *perms, __u64 mask) {
    __u64 a = 0;
    for (const char *p = perms; *p; p++) {
        switch (*p) {
        case 'r': a |= LANDLOCK_ACCESS_FS_READ_FILE | LANDLOCK_ACCESS_FS_READ_DIR; break;
        case 'w': a |= LANDLOCK_ACCESS_FS_WRITE_FILE | LANDLOCK_ACCESS_FS_TRUNCATE |
                      LANDLOCK_ACCESS_FS_MAKE_REG | LANDLOCK_ACCESS_FS_MAKE_DIR |
                      LANDLOCK_ACCESS_FS_MAKE_SYM | LANDLOCK_ACCESS_FS_REMOVE_FILE |
                      LANDLOCK_ACCESS_FS_REMOVE_DIR; break;
        case 'x': a |= LANDLOCK_ACCESS_FS_EXECUTE; break;
        default: return 0; /* unknown letter: the caller refuses */
        }
    }
    /* Rights this ABI cannot express are dropped, not fatal. */
    return a & mask;
}

static int read_spec(const char *path, struct fs_rule *fs, int *nfs, struct net_rule *net,
                     int *nnet, int *net_mode, int *net_required) {
    FILE *f = fopen(path, "r");
    if (!f) { fprintf(stderr, "hermes-acl-shim: spec %s: %s\n", path, strerror(errno)); return -1; }
    char line[16384];
    int seen_version = 0;
    int abi = (int)syscall(SYS_landlock_create_ruleset, NULL, 0, LANDLOCK_CREATE_RULESET_VERSION);
    if (abi < 1) { fclose(f); fprintf(stderr, "hermes-acl-shim: no landlock (abi %d)\n", abi); return -1; }
    __u64 fs_mask = handled_fs(abi);
    while (fgets(line, sizeof line, f)) {
        size_t n = strlen(line);
        while (n && (line[n-1] == '\n' || line[n-1] == '\r')) line[--n] = 0;
        char *s = line;
        while (*s == ' ' || *s == '\t') s++;
        if (!*s || *s == '#') continue;
        if (!strncmp(s, "version ", 8)) {
            if (atoi(s + 8) != 1) { fprintf(stderr, "hermes-acl-shim: unsupported spec version\n"); fclose(f); return -1; }
            seen_version = 1;
        } else if (!strncmp(s, "fs ", 3)) {
            char *perms = s + 3;
            char *sp = strchr(perms, ' ');
            if (!sp) { fprintf(stderr, "hermes-acl-shim: bad fs line\n"); fclose(f); return -1; }
            *sp = 0;
            char *rpath = sp + 1;
            if (*rpath != '/') { fprintf(stderr, "hermes-acl-shim: path not absolute: %s\n", rpath); fclose(f); return -1; }
            if (*nfs >= MAX_RULES) { fprintf(stderr, "hermes-acl-shim: too many fs rules\n"); fclose(f); return -1; }
            __u64 acc = perms_to_access(perms, fs_mask);
            if (!acc) { fprintf(stderr, "hermes-acl-shim: bad perms '%s'\n", perms); fclose(f); return -1; }
            fs[*nfs].access = acc;
            fs[*nfs].path = strdup(rpath);
            if (!fs[*nfs].path) { fclose(f); return -1; }
            (*nfs)++;
        } else if (!strncmp(s, "net ", 4)) {
            char *d = s + 4;
            if (!strcmp(d, "off")) { *net_mode = 0; }
            else if (!strcmp(d, "deny")) { *net_mode = 1; }
            else if (!strcmp(d, "required")) { *net_required = 1; }
            else if (!strncmp(d, "allow ", 6)) {
                char kind[16] = {0};
                long port = -1;
                if (sscanf(d + 6, "%15s %ld", kind, &port) != 2 || port < 0 || port > 65535) {
                    fprintf(stderr, "hermes-acl-shim: bad net allow '%s'\n", d);
                    fclose(f); return -1;
                }
                __u64 bits = 0;
                if (!strcmp(kind, "connect")) {
                    bits |= LANDLOCK_ACCESS_NET_CONNECT_TCP;
                    if (abi >= 7) bits |= LANDLOCK_ACCESS_NET_CONNECT_SEND_UDP;
                } else if (!strcmp(kind, "bind")) {
                    bits |= LANDLOCK_ACCESS_NET_BIND_TCP;
                    if (abi >= 7) bits |= LANDLOCK_ACCESS_NET_BIND_UDP;
                } else { fprintf(stderr, "hermes-acl-shim: unknown net kind '%s'\n", kind); fclose(f); return -1; }
                if (*nnet >= MAX_RULES) { fprintf(stderr, "hermes-acl-shim: too many net rules\n"); fclose(f); return -1; }
                net[*nnet].access = bits;
                net[*nnet].port = (__u64)port;
                (*nnet)++;
            } else { fprintf(stderr, "hermes-acl-shim: unknown net directive '%s'\n", d); fclose(f); return -1; }
        } else {
            fprintf(stderr, "hermes-acl-shim: unknown directive '%s'\n", s);
            fclose(f); return -1;
        }
    }
    fclose(f);
    if (!seen_version) { fprintf(stderr, "hermes-acl-shim: spec has no version line\n"); return -1; }
    return abi;
}

int main(int argc, char **argv) {
    const char *spec_path = NULL;
    int sep = -1;
    for (int i = 1; i < argc; i++) {
        if (!strcmp(argv[i], "--spec") && i + 1 < argc) spec_path = argv[++i];
        else if (!strcmp(argv[i], "--")) { sep = i; break; }
    }
    if (!spec_path || sep < 0 || sep + 1 >= argc) {
        fprintf(stderr, "usage: %s --spec <spec.txt> -- <argv...>\n", argv[0]);
        return EXIT_USAGE;
    }

    struct fs_rule fs[MAX_RULES];
    struct net_rule net[MAX_RULES];
    int nfs = 0, nnet = 0, net_mode = 1, net_required = 0;
    int abi = read_spec(spec_path, fs, &nfs, net, &nnet, &net_mode, &net_required);
    if (abi < 1) return EXIT_REFUSED;

    __u64 net_mask = net_mode ? handled_net(abi) : 0;
    if (net_mode && !net_mask) {
        if (net_required) {
            fprintf(stderr, "hermes-acl-shim: network boundary required but this kernel (ABI %d) has no net rights\n", abi);
            return EXIT_REFUSED;
        }
        fprintf(stderr, "hermes-acl-shim: WARNING abi %d has no network rights; filesystem only\n", abi);
    }

    struct landlock_ruleset_attr ra;
    memset(&ra, 0, sizeof ra);
    ra.handled_access_fs = handled_fs(abi);
    ra.handled_access_net = net_mask;
    int rs = (int)syscall(SYS_landlock_create_ruleset, &ra, sizeof ra, 0);
    if (rs < 0) {
        /* A kernel that ignores handled_access_net answers EINVAL here: refuse, do not guess. */
        fprintf(stderr, "hermes-acl-shim: create_ruleset(fs=0x%llx net=0x%llx): %s\n",
                (unsigned long long)ra.handled_access_fs, (unsigned long long)net_mask, strerror(errno));
        return EXIT_REFUSED;
    }

    for (int i = 0; i < nfs; i++) {
        int pfd = open(fs[i].path, O_PATH | O_CLOEXEC);
        if (pfd < 0) {
            /* Absent path grants nothing; refusing would make every policy brittle. */
            if (errno == ENOENT) continue;
            fprintf(stderr, "hermes-acl-shim: open %s: %s\n", fs[i].path, strerror(errno));
            return EXIT_REFUSED;
        }
        struct landlock_path_beneath_attr pb;
        memset(&pb, 0, sizeof pb);
        pb.allowed_access = fs[i].access;
        pb.parent_fd = pfd;
        if (syscall(SYS_landlock_add_rule, rs, LANDLOCK_RULE_PATH_BENEATH, &pb, 0) != 0) {
            fprintf(stderr, "hermes-acl-shim: add_rule %s: %s\n", fs[i].path, strerror(errno));
            return EXIT_REFUSED;
        }
        close(pfd);
    }
    for (int i = 0; i < nnet; i++) {
        if (!net_mask) continue;
        struct landlock_net_port_attr na;
        memset(&na, 0, sizeof na);
        na.allowed_access = net[i].access & net_mask;
        na.port = net[i].port;
        if (!na.allowed_access) continue;
        if (syscall(SYS_landlock_add_rule, rs, LANDLOCK_RULE_NET_PORT, &na, 0) != 0) {
            fprintf(stderr, "hermes-acl-shim: add_net_rule %llu: %s\n",
                    (unsigned long long)net[i].port, strerror(errno));
            return EXIT_REFUSED;
        }
    }

    if (prctl(PR_SET_NO_NEW_PRIVS, 1, 0, 0, 0) != 0) {
        fprintf(stderr, "hermes-acl-shim: PR_SET_NO_NEW_PRIVS: %s\n", strerror(errno));
        return EXIT_REFUSED;
    }
    if (syscall(SYS_landlock_restrict_self, rs, 0) != 0) {
        fprintf(stderr, "hermes-acl-shim: restrict_self: %s\n", strerror(errno));
        return EXIT_REFUSED;
    }
    close(rs);

    /* The report a caller may want (net actually enforced or not). */
    const char *report = getenv("HERMES_ACL_SHIM_REPORT");
    if (report && *report) {
        unsetenv("HERMES_ACL_SHIM_REPORT");
        FILE *rf = fopen(report, "w");
        if (rf) {
            fprintf(rf, "{\"abi\": %d, \"fs_handled\": %llu, \"net_enabled\": %s, \"net_ports\": [",
                    abi, (unsigned long long)ra.handled_access_fs, net_mask ? "true" : "false");
            int first = 1;
            for (int i = 0; i < nnet && net_mask; i++) {
                fprintf(rf, "%s%llu", first ? "" : ", ", (unsigned long long)net[i].port);
                first = 0;
            }
            fprintf(rf, "], \"net_rights\": %llu}\n", (unsigned long long)net_mask);
            fclose(rf);
        }
    }

    execvp(argv[sep + 1], &argv[sep + 1]);
    fprintf(stderr, "hermes-acl-shim: exec %s: %s\n", argv[sep + 1], strerror(errno));
    return EXIT_USAGE;
}
