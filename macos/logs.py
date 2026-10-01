"""Follow Reflect's saved logs without putting terminal backpressure on the game."""
import re
import threading


class LiveLogs:
    def __init__(self, state, mode="all"):
        sources = {
            "all": [("emulator", "emulator"), ("game", "logcat"), ("viewer", "viewer"), ("http", "http"), ("proxy", "proxy")],
            "http": [("http", "http"), ("proxy", "proxy")],
            "unity-http": [("game", "logcat"), ("http", "http"), ("proxy", "proxy")],
            "none": [], "logcat": [("game", "logcat")],
            "emulator": [("emulator", "emulator")], "unity": [("game", "logcat")],
        }
        self.files = []
        for name, label in sources[mode]:
            path = state / f"{name}.log"
            try:
                stat = path.stat()
                identity, offset = (stat.st_dev, stat.st_ino), stat.st_size
            except FileNotFoundError:
                identity, offset = None, 0
            self.files.append({"path": path, "label": label, "identity": identity,
                               "offset": offset, "partial": b""})
        self.unity = mode in ("unity", "unity-http")
        self.stop = threading.Event()
        self.thread = None

    def __enter__(self):
        if self.files:
            self.thread = threading.Thread(target=self.follow, name="reflect-logs", daemon=True)
            self.thread.start()
        return self

    def __exit__(self, *exc):
        self.stop.set()
        if self.thread:
            self.thread.join(timeout=2)

    def emit(self, file, raw):
        line = raw.decode("utf-8", errors="replace").rstrip("\r")
        if not self.unity or file["label"] != "logcat" or re.search(r"\s[VDIWEFAS]\s+Unity\s*:", line):
            print(f"[{file['label']}] {line}", flush=True)

    def poll(self):
        for file in self.files:
            try:
                stat = file["path"].stat()
                identity = (stat.st_dev, stat.st_ino)
                if identity != file["identity"] or stat.st_size < file["offset"]:
                    file.update(identity=identity, offset=0, partial=b"")
                with file["path"].open("rb") as stream:
                    stream.seek(file["offset"])
                    data = stream.read(256 * 1024)
                    file["offset"] = stream.tell()
                if data:
                    lines = (file["partial"] + data).split(b"\n")
                    file["partial"] = lines.pop()
                    for line in lines:
                        self.emit(file, line)
                    # Bound memory for unusually long lines or carriage-return progress output.
                    if len(file["partial"]) >= 256 * 1024:
                        self.emit(file, file["partial"])
                        file["partial"] = b""
            except FileNotFoundError:
                continue

    def follow(self):
        try:
            while not self.stop.is_set():
                self.poll()
                self.stop.wait(.1)
            self.poll()
            for file in self.files:
                if file["partial"]:
                    self.emit(file, file["partial"])
        except (OSError, ValueError):
            # Closed/piped terminal output must not stop Android or its file logging.
            return
