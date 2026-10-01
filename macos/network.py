"""Optional HTTP/S inspection for Reflect's own Android guest."""
import datetime
import json
import os
from pathlib import Path
import re
import shlex
import shutil
import socket
import subprocess
import sys
import time
import uuid

SOURCE = Path(__file__).resolve().parent / "network"
PORT = 38492
REMOTE = "/data/local/tmp/reflect-http"
CERTS = "/apex/com.android.conscrypt/cacerts"
SYSTEM_CERTS = "/system/etc/security/cacerts"


def setup(state):
    """Install the optional proxy in a private venv, never the user's Python environment."""
    interpreter = next((shutil.which(name) for name in ("python3.13", "python3.12", "python3.14")
                        if shutil.which(name)), None)
    if interpreter is None and sys.version_info >= (3, 12):
        interpreter = sys.executable
    if not interpreter:
        raise RuntimeError("HTTP capture needs Python 3.12 or newer. Install it, then run ./reflect network-setup.")
    state.mkdir(parents=True, exist_ok=True)
    directory = state / "network-venv"
    subprocess.run([interpreter, "-m", "venv", str(directory)], check=True)
    subprocess.run([str(directory / "bin/python"), "-m", "pip", "install", "mitmproxy==12.2.3"], check=True)
    print("HTTP capture is ready. Use ./reflect run PACKAGE --http", flush=True)


class NetworkCapture:
    def __init__(self, android, package, directory=None, ports="80,443", archive=None):
        self.android, self.package = android, package
        try:
            values = sorted(set(int(p) for p in ports.split(",")))
        except ValueError:
            raise RuntimeError("HTTP ports must be comma-separated port numbers.") from None
        if not values or len(values) > 15 or any(p < 1 or p > 65535 or p in (PORT, 38500) for p in values):
            raise RuntimeError("Choose 1–15 HTTP ports from 1–65535, excluding 38492 and 38500.")
        self.ports = ",".join(map(str, values))
        stamp = datetime.datetime.now().strftime("%Y%m%d-%H%M%S") + "-" + uuid.uuid4().hex[:8]
        base = directory.expanduser().resolve() if directory else android.state / "http"
        self.directory = base / stamp
        self.archive = archive.expanduser().resolve() if archive else self.directory / "flows.mitm"
        self.proxy = self.relay = None
        self.rules = []
        self.mounts = []
        self.reverse = False
        self.remote_created = False
        self.uid = None

    def start(self):
        a = self.android
        executable = a.state / "network-venv/bin/mitmdump"
        relay_binary = SOURCE.parents[1] / "build-reflect/reflect-http-relay"
        if not executable.exists():
            raise RuntimeError("Install the optional HTTP proxy first: ./reflect network-setup")
        if not relay_binary.exists():
            raise RuntimeError("HTTP relay is missing. Run ./reflect build, or use the updated standalone Reflect folder.")
        if self.archive.exists():
            raise RuntimeError(f"Capture file already exists: {self.archive}. Choose a new --http-file path.")
        # Guest files can survive reboot even when an interrupted write left the marker empty.
        if a.adb("shell", "test", "-d", REMOTE, check=False).returncode == 0:
            old_boot = a.adb("shell", "cat", f"{REMOTE}/boot-id", check=False).stdout.strip()
            current_boot = a.adb("shell", "cat", "/proc/sys/kernel/random/boot_id").stdout.strip()
            if (old_boot and old_boot != current_boot) or not self.has_active_guest_capture():
                a.adb("shell", "rm", "-rf", REMOTE)
            else:
                raise RuntimeError("A previous HTTP capture did not clean up. Run ./reflect stop to restart the guest before capturing again.")
        with socket.socket() as probe:
            try:
                probe.bind(("127.0.0.1", PORT))
            except OSError:
                raise RuntimeError(f"HTTP capture port {PORT} is already in use.") from None
        packages = a.adb("shell", "cmd", "package", "list", "packages", "-U", "--user", "0",
                         self.package, check=False).stdout
        # Package filters are substring matches; require the exact package name.
        match = re.search(r"^package:" + re.escape(self.package) + r"\s+uid:(\d+)\s*$", packages, re.MULTILINE)
        if not match:
            # Android versions label the user-0 app UID differently in dumpsys.
            dump = a.adb("shell", "dumpsys", "package", self.package).stdout
            match = re.search(r"^\s*(?:userId|appId)=(\d+)\s*$", dump, re.MULTILINE)
        if not match:
            raise RuntimeError("Cannot determine this game's Android UID for HTTP capture.")
        self.uid = match.group(1)
        self.directory.mkdir(parents=True, mode=0o700)
        (self.directory / "bodies").mkdir(mode=0o700)
        self.archive.parent.mkdir(parents=True, exist_ok=True)
        # Reserve the path and restrict payload access before the proxy starts writing.
        with self.archive.open("xb"):
            pass
        self.archive.chmod(0o600)
        (self.directory / "session.json").write_text(json.dumps({"package": self.package, "uid": self.uid,
            "tcp_ports": self.ports, "started": time.time(), "mitmproxy_archive": str(self.archive),
            "format": "HTTP events in http.jsonl; bodies referenced by relative path"}, indent=2) + "\n")
        ca = a.state / "network-ca"
        ca.mkdir(mode=0o700, exist_ok=True)
        ca.chmod(0o700)
        for name in ("http", "proxy"):
            (a.state / f"{name}.log").unlink(missing_ok=True)
        environment = dict(os.environ, PYTHONUNBUFFERED="1", REFLECT_HTTP_DIRECTORY=str(self.directory),
                           REFLECT_HTTP_SUMMARY=str(a.state / "http.log"))
        with (a.state / "proxy.log").open("a") as log:
            self.proxy = subprocess.Popen([str(executable), "--listen-host", "127.0.0.1", "--listen-port", str(PORT),
                "--set", f"confdir={ca}", "--set", "connection_strategy=lazy", "--set", "ssl_insecure=false",
                "--set", "block_global=false", "-s", str(SOURCE / "addon.py"),
                "-w", str(self.archive)], env=environment, stdout=log, stderr=subprocess.STDOUT)
        certificate = ca / "mitmproxy-ca-cert.pem"
        for _ in range(200):
            if self.proxy.poll() is not None:
                raise RuntimeError(f"HTTP proxy exited. See {a.state / 'proxy.log'}")
            if certificate.exists():
                try:
                    with socket.create_connection(("127.0.0.1", PORT), timeout=.1):
                        break
                except OSError:
                    pass
            time.sleep(.1)
        else:
            raise RuntimeError("HTTP proxy startup timed out.")
        cert_hash = subprocess.run(["openssl", "x509", "-in", str(certificate), "-subject_hash_old", "-noout"],
                                   check=True, capture_output=True, text=True).stdout.strip()
        if not re.fullmatch(r"[0-9a-f]{8}", cert_hash):
            raise RuntimeError("Unexpected proxy certificate hash.")
        a.adb("shell", "mkdir", REMOTE)
        self.remote_created = True
        a.adb("shell", "sh", "-c", f"cat /proc/sys/kernel/random/boot_id > {REMOTE}/boot-id")
        a.adb("shell", "sync")
        a.adb("shell", "mkdir", f"{REMOTE}/cacerts")
        a.adb("shell", "sh", "-c", f"cp {CERTS}/* {REMOTE}/cacerts/")
        a.adb("shell", "sh", "-c", f"cp {SYSTEM_CERTS}/* {REMOTE}/cacerts/")
        a.adb("push", str(certificate), f"{REMOTE}/cacerts/{cert_hash}.0")
        a.adb("push", str(relay_binary), f"{REMOTE}/relay")
        a.adb("shell", "chmod", "755", REMOTE, f"{REMOTE}/cacerts", f"{REMOTE}/relay")
        a.adb("shell", "sh", "-c", f"chmod 644 {REMOTE}/cacerts/* && chcon -R u:object_r:system_security_cacerts_file:s0 {REMOTE}/cacerts")
        # Android 14's Conscrypt APEX is immutable; bind a temporary store in each zygote namespace.
        pids = a.adb("shell", "pidof", "zygote", "zygote64", check=False).stdout.split()
        if not pids or not all(pid.isdigit() for pid in pids):
            raise RuntimeError("Cannot locate Android zygote for temporary HTTPS trust.")
        for pid in pids:
            # Java uses Conscrypt's APEX store; native TLS libraries may use the system path.
            for store in (CERTS, SYSTEM_CERTS):
                a.adb("shell", "nsenter", f"--mount=/proc/{pid}/ns/mnt", "--", "mount", "--bind", f"{REMOTE}/cacerts", store)
                self.mounts.append((pid, store))
        a.adb("reverse", f"tcp:{PORT}", f"tcp:{PORT}")
        self.reverse = True
        from .runtime import SERIAL
        with (self.directory / "relay.log").open("w") as log:
            command = f"echo $$ > {REMOTE}/pid; exec {REMOTE}/relay"
            self.relay = subprocess.Popen([str(a.sdk / "platform-tools/adb"), "-s", SERIAL, "shell",
                shlex.join(["sh", "-c", command])], stdout=log, stderr=subprocess.STDOUT)
        for _ in range(50):
            if self.relay.poll() is not None:
                raise RuntimeError(f"HTTP relay exited. See {self.directory / 'relay.log'}")
            if "ready" in (self.directory / "relay.log").read_text():
                break
            time.sleep(.1)
        else:
            raise RuntimeError("HTTP relay startup timed out.")
        for binary in ("iptables", "ip6tables"):
            rule = ["-t", "nat", "-p", "tcp", "-m", "owner", "--uid-owner", self.uid,
                    "-m", "multiport", "--dports", self.ports, "-m", "comment", "--comment", "reflect-http",
                    "-j", "REDIRECT", "--to-ports", "38500"]
            result = a.adb("shell", binary, "-I", "OUTPUT", "1", *rule, check=False)
            if result.returncode:
                message = (result.stderr + result.stdout).strip()
                if binary == "ip6tables" and "Table does not exist" in message:
                    notice = "IPv6 NAT is unavailable in this emulator; capturing IPv4 only. IPv6 traffic is not captured."
                    print(f"[http] {notice}", flush=True)
                    with (a.state / "proxy.log").open("a") as log:
                        log.write(notice + "\n")
                    continue
                raise RuntimeError(message or f"Could not configure {binary} for HTTP capture.")
            self.rules.append((binary, rule))
        session_path = self.directory / "session.json"
        session = json.loads(session_path.read_text())
        session["ip_versions"] = [4] + ([6] if any(binary == "ip6tables" for binary, _ in self.rules) else [])
        session_path.write_text(json.dumps(session, indent=2) + "\n")
        print(f"HTTP/S capture: {self.directory}\nFull headers and bodies: http.jsonl + bodies/\nMitmproxy archive: {self.archive}\nCapturing this game's TCP ports {self.ports}. Pinned TLS and QUIC are not decrypted.", flush=True)

    def has_active_guest_capture(self):
        """Fail closed on inspection errors; directory presence alone is not an active capture."""
        a = self.android
        for binary in ("iptables", "ip6tables"):
            result = a.adb("shell", binary, "-t", "nat", "-S", "OUTPUT", check=False)
            if result.returncode:
                if binary == "ip6tables" and "Table does not exist" in result.stdout + result.stderr:
                    continue
                raise RuntimeError("Could not inspect previous HTTP redirect rules: " + (result.stderr + result.stdout).strip())
            if "reflect-http" in result.stdout:
                return True
        pids = a.adb("shell", "pidof", "zygote", "zygote64", check=False).stdout.split()
        if not pids or not all(pid.isdigit() for pid in pids):
            raise RuntimeError("Could not inspect Android certificate mount namespaces.")
        for pid in ["self", *pids]:
            mounts = a.adb("shell", "cat", f"/proc/{pid}/mountinfo").stdout
            if "reflect-http/cacerts" in mounts:
                return True
        processes = a.adb("shell", "ps", "-A", "-o", "ARGS").stdout
        return any(line.strip().split(" ")[0] == f"{REMOTE}/relay" for line in processes.splitlines())

    def check(self):
        for name, child in (("proxy", self.proxy), ("relay", self.relay)):
            if child and child.poll() is not None:
                raise RuntimeError(f"HTTP {name} exited; stopping the capture. See capture and proxy logs.")

    def close(self):
        a = self.android
        failures = []
        def shell(*args):
            result = a.adb("shell", *args, check=False, timeout=10)
            if result.returncode:
                failures.append(result.stderr.strip() or result.stdout.strip() or "Android cleanup failed")
            return result
        if self.remote_created:
            shell("am", "force-stop", self.package)
        for binary, rule in reversed(self.rules):
            shell(binary, "-D", "OUTPUT", *rule)
        if self.remote_created:
            pid = a.adb("shell", "cat", f"{REMOTE}/pid", check=False, timeout=5).stdout.strip()
            if pid.isdigit():
                shell("kill", pid)
        for child in (self.relay, self.proxy):
            if child and child.poll() is None:
                child.terminate()
                try:
                    child.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    child.kill(); child.wait()
        if self.reverse:
            a.adb("reverse", "--remove", f"tcp:{PORT}", check=False, timeout=10)
        for pid, store in reversed(self.mounts):
            shell("nsenter", f"--mount=/proc/{pid}/ns/mnt", "--", "umount", store)
        if self.remote_created and not failures:
            shell("rm", "-rf", REMOTE)
        if failures:
            print("HTTP cleanup was incomplete; run ./reflect stop before another capture. " + "; ".join(failures), file=sys.stderr)
        if self.directory.exists():
            for name in ("http", "proxy"):
                path = a.state / f"{name}.log"
                if path.exists():
                    shutil.copy2(path, self.directory / f"{name}.log")
            # Restrict saved payloads even when the user's shell has a permissive umask.
            for path in self.directory.rglob("*"):
                path.chmod(0o700 if path.is_dir() else 0o600)
