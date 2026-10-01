"""Pinned Apple silicon packages from Google's public Android SDK repository."""
import hashlib
import json
import os
from pathlib import Path
import shutil
import tempfile
import urllib.request
import zipfile
import stat

REPOSITORY = "https://dl.google.com/android/repository/"
IMAGE = "system-images/android-34/google_apis/arm64-v8a"
PACKAGES = [
    ("platform-tools", "platform-tools_r37.0.1-darwin.zip", "6ae73f4de6452dc57e62ec02b68eed92a4c21661", 16110554),
    ("emulator", "emulator-darwin_aarch64-15917651.zip", "f22f44948a2b7f0a0103645b9a639290eef92426", 394555844),
    ("build-tools/36.0.0", "build-tools_r36_macosx.zip", "199ae0047ee61e842f8ee0c6d3918e44fb9a1f83", 79121749),
    ("platforms/android-29", "platform-29_r05.zip", "9d8a7e0ffa5168dbca6c60355b9129c6c7572aff", 78293913),
    ("ndk/27.3.13750724", "android-ndk-r27d-darwin.zip", "2970926d705988f79baa9b04c51b4f7914dd8c56", 839006233),
    (IMAGE, "sys-img/google_apis/arm64-v8a-34_r14.zip", "2fe8b46d419a3400e30f31b0152b241b50c8b99f", 1610393229),
]


def install(sdk):
    sdk.mkdir(parents=True, exist_ok=True)
    cache = sdk / ".reflect-downloads"
    cache.mkdir(exist_ok=True)
    for destination, archive, checksum, size in PACKAGES:
        target = sdk / destination
        stamp = target / ".reflect-package.json"
        if stamp.exists() and json.loads(stamp.read_text()).get("sha1") == checksum:
            continue
        # Keep user-managed SDK installations in place. The build validates required tools.
        if (target / "source.properties").exists() and not stamp.exists():
            print(f"Using existing {destination}", flush=True)
            continue
        part = cache / (Path(archive).name + ".part")
        print(f"Downloading {destination} ({size / 1e9:.2f} GB)", flush=True)
        have = part.stat().st_size if part.exists() else 0
        if have != size:
            request = urllib.request.Request(REPOSITORY + archive, headers={"Range": f"bytes={have}-"} if have else {})
            with urllib.request.urlopen(request, timeout=60) as response:
                if have and response.status != 206:
                    have = 0
                with part.open("ab" if have else "wb") as output:
                    previous = -1
                    while chunk := response.read(1024 * 1024):
                        output.write(chunk)
                        have += len(chunk)
                        percent = int(have * 100 / size)
                        if percent // 10 != previous:
                            previous = percent // 10
                            print(f"  {percent}%", flush=True)
        digest = hashlib.sha1()
        with part.open("rb") as source:
            while chunk := source.read(1024 * 1024):
                digest.update(chunk)
        if part.stat().st_size != size or digest.hexdigest() != checksum:
            part.unlink()
            raise RuntimeError(f"Checksum mismatch for {archive}; run setup again.")
        with zipfile.ZipFile(part) as contents:
            unpacked = sum(item.file_size for item in contents.infolist())
            for item in contents.infolist():
                path = Path(item.filename)
                if path.is_absolute() or ".." in path.parts:
                    raise RuntimeError("Unsafe SDK archive path")
        if shutil.disk_usage(sdk).free < unpacked + 512 * 1024**2:
            raise RuntimeError(f"Need {unpacked / 1e9 + .5:.1f} GB free to unpack {destination}.")
        print(f"Unpacking {destination}", flush=True)
        with tempfile.TemporaryDirectory(dir=cache) as temporary:
            staging = Path(temporary)
            # Google's system images use ZIP64, which macOS ditto cannot always read.
            with zipfile.ZipFile(part) as contents:
                for item in contents.infolist():
                    output = staging / item.filename
                    mode = item.external_attr >> 16
                    if item.is_dir():
                        output.mkdir(parents=True, exist_ok=True)
                        continue
                    output.parent.mkdir(parents=True, exist_ok=True)
                    if stat.S_ISLNK(mode):
                        link = contents.read(item).decode()
                        if not (output.parent / link).resolve().is_relative_to(staging.resolve()):
                            raise RuntimeError("Unsafe SDK archive symlink")
                        output.symlink_to(link)
                    else:
                        with contents.open(item) as source, output.open("wb") as destination_file:
                            shutil.copyfileobj(source, destination_file, 1024 * 1024)
                        if mode & 0o777:
                            output.chmod(mode & 0o777)
            entries = [p for p in staging.iterdir() if p.name != "__MACOSX"]
            if len(entries) != 1:
                raise RuntimeError(f"Unexpected layout in {archive}")
            target.parent.mkdir(parents=True, exist_ok=True)
            if target.exists():
                raise RuntimeError(f"Incomplete SDK package at {target}; move it aside and retry.")
            shutil.move(str(entries[0]), target)
        stamp.write_text(json.dumps({"archive": archive, "sha1": checksum}) + "\n")
        part.unlink()


def create_avd(sdk, avd_home, name="reflect-arm64"):
    avd_home.mkdir(parents=True, exist_ok=True)
    directory = avd_home / f"{name}.avd"
    config = directory / "config.ini"
    if config.exists():
        return
    directory.mkdir(exist_ok=True)
    values = {
        "avd.ini.encoding": "UTF-8", "AvdId": name, "avd.ini.displayname": "Reflect",
        "abi.type": "arm64-v8a", "hw.cpu.arch": "arm64", "hw.cpu.ncore": "4",
        "hw.ramSize": "4096", "vm.heapSize": "512M", "disk.dataPartition.size": "2G",
        "image.sysdir.1": str(sdk / IMAGE) + "/", "tag.id": "google_apis", "tag.display": "Google APIs",
        "target": "android-34", "hw.gpu.enabled": "yes", "hw.gpu.mode": "host",
        "hw.lcd.width": "1280", "hw.lcd.height": "720", "hw.lcd.density": "240",
        "hw.keyboard": "yes", "hw.mainKeys": "no", "hw.audioInput": "yes", "hw.audioOutput": "yes",
        "hw.camera.back": "none", "hw.camera.front": "none", "hw.sdCard": "no",
        "showDeviceFrame": "no", "fastboot.forceColdBoot": "yes", "PlayStore.enabled": "no",
    }
    config.write_text("".join(f"{key}={value}\n" for key, value in values.items()))
    (avd_home / f"{name}.ini").write_text(f"avd.ini.encoding=UTF-8\npath={directory}\ntarget=android-34\n")
