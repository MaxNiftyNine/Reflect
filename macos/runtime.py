"""APK install and play lifecycle for Reflect's dedicated ARM64 Android emulator."""
import contextlib
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import signal
import shlex
import subprocess
import time
import zipfile

ROOT = Path(__file__).resolve().parents[1]
BUILD = ROOT / "build-reflect"
SERIAL = "emulator-5580"
AVD = "reflect-arm64"
PACKAGE = re.compile(r"[A-Za-z][A-Za-z0-9_]*(?:\.[A-Za-z][A-Za-z0-9_]*)+")
RUNTIME_APKS = ["reflect-runtime", "reflect-systemdriver", "reflect-platform"]


def package_name(value):
    if not PACKAGE.fullmatch(value):
        raise RuntimeError(f"Invalid Android package name: {value}")
    return value


def process_identity(pid):
    # Include the start time so a reused PID cannot identify an unrelated process.
    return subprocess.run(["ps", "-p", str(pid), "-o", "lstart=,command="], capture_output=True, text=True).stdout.strip()


class Android:
    def __init__(self, sdk, state):
        self.sdk, self.state = sdk.expanduser().resolve(), state
        self.state.mkdir(parents=True, exist_ok=True)
        self.environment = dict(os.environ, ANDROID_SDK_ROOT=str(self.sdk), ANDROID_HOME=str(self.sdk), ANDROID_AVD_HOME=str(state / "avd"))
        self.emulator_process = None

    def adb(self, *args, check=True, timeout=120):
        # adb shell reparses arguments remotely. Quote each word so package/file names remain literal.
        if args and args[0] == "shell":
            args = ("shell", shlex.join(str(a) for a in args[1:]))
        command = [str(self.sdk / "platform-tools/adb"), "-s", SERIAL, *[str(a) for a in args]]
        try:
            result = subprocess.run(command, capture_output=True, text=True, timeout=timeout)
        except subprocess.TimeoutExpired:
            if check:
                raise RuntimeError(f"Android did not respond within {timeout} seconds: {args[0]}") from None
            return subprocess.CompletedProcess(command, 124, "", "Android command timed out")
        if check and result.returncode:
            raise RuntimeError((result.stderr + result.stdout).strip() or f"adb failed: {args}")
        return result

    @contextlib.contextmanager
    def lock(self):
        with (self.state / "session.lock").open("w") as file:
            try:
                fcntl.flock(file, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                raise RuntimeError("A Reflect command is already using Android. Close the game or run ./reflect stop.")
            yield

    def ready(self):
        if not (self.sdk / "emulator/emulator").exists():
            raise RuntimeError("Android SDK is missing. Run ./reflect setup.")
        reuse = False
        for _ in range(30):
            status = self.adb("get-state", check=False, timeout=10)
            if status.returncode:
                break
            response = self.adb("emu", "avd", "name", check=False, timeout=10)
            names = [line.strip() for line in response.stdout.splitlines() if line.strip() and line.strip() != "OK"]
            if names:
                if names[0] != AVD:
                    raise RuntimeError(f"Port 5580 belongs to another emulator ({names[0]}). Stop that emulator first.")
                reuse = True
                break
            # ADB can briefly report a device after the emulator console has shut down.
            time.sleep(.1)
        else:
            raise RuntimeError("The emulator on port 5580 is not responding; wait for it to stop and retry.")
        if not reuse:
            if not (self.state / "avd" / f"{AVD}.ini").exists():
                raise RuntimeError("Reflect's virtual device is missing. Run ./reflect setup.")
            print("Starting ARM64 Android…", flush=True)
            with (self.state / "emulator.log").open("a") as log:
                self.emulator_process = subprocess.Popen([str(self.sdk / "emulator/emulator"), "-avd", AVD, "-port", "5580", "-no-window", "-no-snapshot", "-writable-system", "-gpu", "host", "-memory", "4096"], env=self.environment, stdout=log, stderr=subprocess.STDOUT, start_new_session=True)
        deadline = time.monotonic() + 240
        while time.monotonic() < deadline:
            if self.emulator_process and self.emulator_process.poll() is not None:
                raise RuntimeError(f"Android stopped during boot. See {self.state / 'emulator.log'}")
            if self.adb("shell", "getprop", "sys.boot_completed", check=False, timeout=10).stdout.strip() == "1":
                abi = self.adb("shell", "getprop", "ro.product.cpu.abi").stdout.strip()
                if abi != "arm64-v8a":
                    raise RuntimeError(f"Expected an ARM64 Android image, got {abi}")
                if not self.quest_identity():
                    # Migrate an emulator started by an older Reflect without writable system images.
                    if self.emulator_process is not None:
                        raise RuntimeError("Reflect's writable Android system could not be remounted.")
                    self.stop()
                    self.emulator_process = None
                    return self.ready()
                print("Android ready (arm64-v8a).", flush=True)
                self.adb("shell", "settings", "put", "global", "hidden_api_policy", "1", check=False)
                self.adb("shell", "settings", "put", "secure", "show_ime_with_hard_keyboard", "0", check=False)
                self.adb("shell", "input", "keyevent", "82", check=False)
                return
            time.sleep(1)
        raise RuntimeError(f"Android boot timed out. See {self.state / 'emulator.log'}")

    def wait_boot(self):
        self.adb("wait-for-device", timeout=180)
        deadline = time.monotonic() + 180
        while time.monotonic() < deadline:
            if self.adb("shell", "getprop", "sys.boot_completed", check=False, timeout=10).stdout.strip() == "1":
                return
            time.sleep(1)
        raise RuntimeError("Android did not finish rebooting after Quest compatibility setup.")

    def quest_identity(self):
        # Unity's Oculus loader checks Android Build.MANUFACTURER before opening OpenXR.
        # Change only Reflect's private guest; leave the SDK's original system images intact.
        if self.adb("shell", "getprop", "ro.product.manufacturer").stdout.strip().lower() == "oculus":
            return True
        print("Configuring Quest device compatibility…", flush=True)
        self.adb("root")
        self.adb("wait-for-device")
        self.adb("disable-verity")
        result = self.adb("remount", check=False)
        message = result.stdout + result.stderr
        if "bootloader unlocked" in message:
            return False
        if "reboot" in message.lower():
            self.adb("reboot")
            self.wait_boot()
            self.adb("root")
            self.adb("wait-for-device")
            result = self.adb("remount", check=False)
            message = result.stdout + result.stderr
        if result.returncode or "Remount succeeded" not in message:
            raise RuntimeError(f"Could not configure Quest device compatibility: {message.strip()}")
        script = r'''set -e
for f in /system/build.prop /vendor/build.prop /product/etc/build.prop /system_ext/etc/build.prop /odm/etc/build.prop; do
    [ -f "$f" ] || continue
    [ -f "$f.before-reflect" ] || cp -p "$f" "$f.before-reflect"
    sed -i -E 's/^(ro\.product\.[a-z_]+\.brand)=.*/\1=oculus/; s/^(ro\.product\.[a-z_]+\.manufacturer)=.*/\1=Oculus/; s/^(ro\.product\.[a-z_]+\.model)=.*/\1=Quest 3/' "$f"
done
sync'''
        self.adb("shell", "sh", "-c", script)
        self.adb("reboot")
        self.wait_boot()
        if self.adb("shell", "getprop", "ro.product.manufacturer").stdout.strip().lower() != "oculus":
            raise RuntimeError("Quest device compatibility did not persist after reboot.")
        return True

    def install_runtime(self):
        cache_file = self.state / "runtime-installed.json"
        try:
            cache = json.loads(cache_file.read_text())
        except (FileNotFoundError, ValueError):
            cache = {}
        for name, package in zip(RUNTIME_APKS, ["com.refract.openxrruntime", "com.oculus.systemdriver", "com.oculus.horizon"]):
            path = BUILD / f"{name}.apk"
            if not path.exists():
                raise RuntimeError("Runtime APKs are missing. Run ./reflect build.")
            digest = hashlib.sha256(path.read_bytes()).hexdigest()
            installed = self.adb("shell", "pm", "path", package, check=False).stdout.startswith("package:")
            if cache.get(name) == digest and installed:
                continue
            print(f"Installing {name}…", flush=True)
            self.adb("install", "--no-incremental", "--force-queryable", "-r", path)
            cache[name] = digest
        cache_file.write_text(json.dumps(cache) + "\n")

    def configure_vulkan(self, package):
        layer = BUILD / "libVkLayer_REFRACT_runtime.so"
        if not layer.exists():
            raise RuntimeError("Vulkan compatibility layer is missing. Run ./reflect build.")
        self.adb("root")
        self.adb("wait-for-device")
        directory = "/data/local/debug/vulkan"
        remote = directory + "/" + layer.name
        digest = hashlib.sha256(layer.read_bytes()).hexdigest()
        current = self.adb("shell", "sha256sum", remote, check=False).stdout.split()
        if not current or current[0] != digest:
            self.adb("shell", "mkdir", "-p", directory)
            self.adb("push", layer, remote + ".new")
            if self.adb("shell", "sha256sum", remote + ".new").stdout.split()[0] != digest:
                raise RuntimeError("Vulkan compatibility layer transfer failed.")
            self.adb("shell", "mv", remote + ".new", remote)
        paths = ["/data/local/debug", directory, remote]
        self.adb("shell", "chmod", "755", *paths)
        self.adb("shell", "chcon", "u:object_r:apk_data_file:s0", *paths)
        for key, value in [("enable_gpu_debug_layers", "1"), ("gpu_debug_app", package), ("gpu_debug_layers", "VK_LAYER_REFRACT_runtime")]:
            self.adb("shell", "settings", "put", "global", key, value)
        self.adb("shell", "settings", "delete", "global", "gpu_debug_layer_app")
        # Keep Apple GPU texture handling native; only descriptor compatibility is needed here.
        self.adb("shell", "setprop", "debug.refract.transcode_textures", "0")
        self.adb("shell", "setprop", "debug.refract.cached_buffer_memory", "0")

    def apk_info(self, path):
        path = path.expanduser().resolve()
        if not path.is_file():
            raise RuntimeError(f"APK not found: {path}")
        with zipfile.ZipFile(path) as archive:
            abis = {item.split('/')[1] for item in archive.namelist() if item.startswith("lib/") and item.endswith(".so")}
        if abis and "arm64-v8a" not in abis:
            raise RuntimeError(f"APK has no ARM64 libraries (contains {', '.join(sorted(abis))}).")
        info = subprocess.run([str(self.sdk / "build-tools/36.0.0/aapt2"), "dump", "badging", str(path)], capture_output=True, text=True, check=True).stdout
        match = re.search(r"^package: name='([^']+)'", info, re.M)
        if not match:
            raise RuntimeError("Could not read APK package name")
        return package_name(match.group(1)), path

    def install(self, path, obbs=(), owned=False):
        package, path = self.apk_info(path)
        self.ready()
        self.install_runtime()
        print(f"Installing {package}…", flush=True)
        self.adb("install", "--no-incremental", "-r", "-g", path)
        for obb in obbs:
            obb = obb.expanduser().resolve()
            if not obb.is_file() or obb.suffix.lower() != ".obb":
                raise RuntimeError(f"Expansion file must be an existing .obb file: {obb}")
            destination = f"/sdcard/Android/obb/{package}"
            self.adb("shell", "mkdir", "-p", destination)
            self.adb("push", obb, destination + "/" + obb.name)
        if owned:
            file = self.state / "owned-games.txt"
            games = set(file.read_text().splitlines()) if file.exists() else set()
            games.add(package)
            file.write_text("\n".join(sorted(games)) + "\n")
        print(f"Installed. Play with: ./reflect run {package}", flush=True)
        return package

    def list(self):
        self.ready()
        print(self.adb("shell", "pm", "list", "packages", "-3").stdout.replace("package:", "").strip())

    def play(self, package, width=1024, height=1024, fps=90, capture=None, frames=None, seconds=None,
             http=False, http_dir=None, http_ports="80,443", http_file=None):
        package_name(package)
        viewer = BUILD / "viewer/Reflect.app/Contents/MacOS/Reflect"
        if not viewer.exists():
            raise RuntimeError("Mac viewer is missing. Run ./reflect build.")
        self.ready()
        self.install_runtime()
        self.configure_vulkan(package)
        activities = self.adb("shell", "cmd", "package", "resolve-activity", "--brief", "-a", "android.intent.action.MAIN", "-c", "android.intent.category.LAUNCHER", package).stdout.strip().splitlines()
        component = activities[-1] if activities else ""
        if "/" not in component or not component.startswith(package + "/"):
            raise RuntimeError(f"No launchable activity found for {package}; install its APK first.")
        properties = {
            "debug.refract.runtime_name": "Oculus", "debug.refract.gpu_share": "0",
            "debug.refract.composite": "1", "debug.refract.pixel_composite": "1", "debug.refract.frame_sync": "0",
            "debug.refract.direct_host": "1", "debug.refract.video": "off",
            "debug.refract.gles_readback": "1", "debug.refract.gles_probe": "0",
            "debug.refract.pixel_top_down": "1",
            "debug.refract.readback_layers": "scene",
            "debug.refract.stream_eyes": "2", "debug.refract.stream_scale": "100",
            "debug.refract.hfov": "90", f"debug.refract.platform.owned.{package}": "1",
        }
        for key, value in properties.items():
            self.adb("shell", "setprop", key, value)
        self.adb("shell", "am", "force-stop", package)
        # Older installs may not have received -g; hidden permission prompts stop Unity's render loop.
        for permission in ["android.permission.RECORD_AUDIO", "android.permission.POST_NOTIFICATIONS"]:
            self.adb("shell", "pm", "grant", package, permission, check=False)
        ready = self.state / "viewer-ready"
        ready.unlink(missing_ok=True)
        arguments = [str(viewer), "--width", str(width), "--height", str(height), "--fps", str(fps), "--ready-file", str(ready)]
        if capture:
            capture = capture.expanduser().resolve(); capture.parent.mkdir(parents=True, exist_ok=True)
            arguments += ["--capture", str(capture)]
        if frames:
            arguments += ["--quit-after-frames", str(frames)]
        session_file = self.state / "session.json"
        process = None
        log_process = None
        started = False
        network = None
        try:
            if http:
                from .network import NetworkCapture
                network = NetworkCapture(self, package, http_dir, http_ports, http_file)
                network.start()
            # New inodes let live followers distinguish this session from the previous one.
            (self.state / "viewer.log").unlink(missing_ok=True)
            with (self.state / "viewer.log").open("w") as log:
                process = subprocess.Popen(arguments, stdout=log, stderr=subprocess.STDOUT)
            temporary = session_file.with_suffix(".tmp")
            temporary.write_text(json.dumps({"pid": os.getpid(), "identity": process_identity(os.getpid()), "viewer_pid": process.pid, "package": package}) + "\n")
            temporary.replace(session_file)
            for _ in range(100):
                if ready.exists(): break
                if process.poll() is not None:
                    raise RuntimeError(f"Viewer failed to start. See {self.state / 'viewer.log'}")
                time.sleep(.1)
            else:
                raise RuntimeError("Viewer startup timed out")
            for port in (38490, 38491):
                for attempt in range(60):
                    result = self.adb("reverse", f"tcp:{port}", f"tcp:{port}", check=False)
                    if result.returncode == 0:
                        break
                    message = (result.stdout + result.stderr).strip()
                    if "Address already in use" not in message or attempt == 59:
                        raise RuntimeError(f"Could not connect Android to the viewer on port {port}: {message}")
                    # Immediately after shutdown, adbd can still be releasing its old listener.
                    time.sleep(.25)
            (self.state / "game.log").unlink(missing_ok=True)
            with (self.state / "game.log").open("w") as log:
                log_process = subprocess.Popen([str(self.sdk / "platform-tools/adb"), "-s", SERIAL, "logcat", "-v", "threadtime", "-T", "1"], stdout=log, stderr=subprocess.STDOUT)
            result = self.adb("shell", "am", "start", "-W", "-n", component)
            if "Error:" in result.stdout or "Exception" in result.stdout:
                raise RuntimeError(result.stdout.strip())
            started = True
            print(f"Playing {package}. Close the window or press Ctrl-C to stop.\nWASD walk; click captures mouse; Esc releases; E/Q triggers; R/F grips; Space/Z A/X; Backspace/X B/Y; M menu; C reach; T calibration; Home reset; Tab stereo.", flush=True)
            deadline = time.monotonic() + seconds if seconds else float('inf')
            next_health_check = time.monotonic() + 5
            while process.poll() is None and time.monotonic() < deadline:
                time.sleep(.5)
                if network:
                    network.check()
                if time.monotonic() >= next_health_check:
                    next_health_check = time.monotonic() + 5
                    if not self.adb("shell", "pidof", package, check=False).stdout.strip():
                        raise RuntimeError(f"{package} exited. See {self.state / 'game.log'} for Android errors.")
            if process.poll() not in (None, 0):
                raise RuntimeError(f"Viewer exited with status {process.returncode}. See {self.state / 'viewer.log'}")
        finally:
            if network:
                network.close()
            if started:
                self.adb("shell", "sync", check=False)
                self.adb("shell", "am", "force-stop", package, check=False)
            for child in (process, log_process):
                if child and child.poll() is None:
                    child.terminate()
                    try: child.wait(timeout=5)
                    except subprocess.TimeoutExpired: child.kill(); child.wait()
            for port in (38490, 38491):
                self.adb("reverse", "--remove", f"tcp:{port}", check=False)
            ready.unlink(missing_ok=True)
            session_file.unlink(missing_ok=True)

    def stop(self):
        session = self.state / "session.json"
        if session.exists():
            data = json.loads(session.read_text())
            pid = int(data["pid"])
            identity = process_identity(pid)
            if identity and identity == data.get("identity"):
                os.kill(pid, signal.SIGINT)
                for _ in range(100):
                    if not session.exists(): break
                    time.sleep(.1)
                else:
                    raise RuntimeError("Reflect is still stopping; leave Android running and retry shortly.")
            else:
                raise RuntimeError("Session record is stale; it does not identify a running Reflect process.")
        if self.adb("get-state", check=False, timeout=5).returncode == 0:
            names = self.adb("emu", "avd", "name").stdout.splitlines()
            name = names[0] if names else "unknown"
            if name != AVD:
                raise RuntimeError("Port 5580 belongs to another emulator; leaving it running.")
            self.adb("shell", "sync", check=False)
            self.adb("emu", "kill")
            for _ in range(300):
                if self.adb("get-state", check=False, timeout=5).returncode != 0:
                    break
                time.sleep(.1)
            else:
                raise RuntimeError("Android has not finished shutting down; check emulator.log.")
        print("Reflect stopped. Android app data remains in the virtual device.")
