"""Reflect CLI; no launcher, account service, or PC VR runtime."""
import argparse
import os
from pathlib import Path
import platform
import signal
import subprocess
import sys

from . import sdk as android_sdk

ROOT = Path(__file__).resolve().parents[1]
STATE = Path(os.environ.get("REFLECT_HOME", Path.home() / "Library/Application Support/Reflect")).expanduser().resolve()


def logging_options(parser, default=argparse.SUPPRESS):
    group = parser.add_mutually_exclusive_group()
    group.add_argument("--logs", dest="log_mode", choices=["all", "none", "logcat", "emulator", "unity", "http", "unity-http"],
                       default=default, help="live terminal logs (default: all); unity shows only the Unity logcat tag")
    for flags, mode, help in [
        (["--no-logs", "--no-log"], "none", "hide live logs; keep saving log files"),
        (["--logcat", "--only-logcat"], "logcat", "show only live Android logcat"),
        (["--emulator-logs", "--only-emulator"], "emulator", "show only live emulator logs"),
        (["--unity"], "unity", "show only live Unity logcat messages"),
        (["--unity-network", "--unity-http"], "unity-http", "show Unity logcat messages and HTTP/proxy logs (use --http to capture traffic)"),
    ]:
        group.add_argument(*flags, dest="log_mode", action="store_const", const=mode,
                           default=argparse.SUPPRESS, help=help)


def run(*arguments, **kwargs):
    print("+ " + " ".join(str(a) for a in arguments), flush=True)
    return subprocess.run([str(a) for a in arguments], check=True, **kwargs)


def main():
    parser = argparse.ArgumentParser(description="Reflect: Android OpenXR APKs in a Mac window")
    parser.add_argument("--sdk", type=Path, default=Path(os.environ.get("ANDROID_HOME", STATE / "sdk")))
    logging_options(parser, "all")
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("setup", help="download the ARM64 Android SDK and create Reflect's emulator")
    commands.add_parser("build", help="build the native Mac viewer and Android runtime APKs")
    commands.add_parser("network-setup", help="install the optional HTTP/S capture proxy")
    commands.add_parser("doctor", help="report local prerequisites")
    install = commands.add_parser("install", help="install an ARM64 APK and optional OBB files")
    install.add_argument("apk", type=Path)
    install.add_argument("--obb", type=Path, action="append", default=[])
    install.add_argument("--owned", action="store_true", help="legacy ownership-list flag; launched games are automatically reported as owned")
    commands.add_parser("list", help="list APK packages installed in Reflect's Android device")
    commands.add_parser("stop", help="stop Reflect and shut down its emulator")
    commands.add_parser("logs", help="print the locations of emulator, viewer, and game logs")
    for command in ("run", "demo"):
        play = commands.add_parser(command, help="play an installed APK" if command == "run" else "install and play the included OpenXR sample")
        if command == "run": play.add_argument("package")
        play.add_argument("--width", type=int, default=1024)
        play.add_argument("--height", type=int, default=1024)
        play.add_argument("--fps", type=int, default=90)
        play.add_argument("--capture", type=Path, help="save the first eye image to a PNG")
        play.add_argument("--frames", type=int, help="close after receiving this many frames")
        play.add_argument("--seconds", type=float, help="stop after this many seconds")
        play.add_argument("--http", "--http-log", action="store_true", help="capture HTTP/S headers and bodies for this game")
        play.add_argument("--http-dir", type=Path, help="parent directory for HTTP capture sessions (also enables capture)")
        play.add_argument("--http-file", type=Path, help="save a mitmproxy archive to this new file (also enables capture)")
        play.add_argument("--http-ports", default="80,443", help="TCP ports to capture (default: 80,443; requires --http)")
    for command_parser in commands.choices.values():
        logging_options(command_parser)
    args = parser.parse_args()
    args.sdk = args.sdk.expanduser().resolve()
    try:
        if platform.system() != "Darwin" or platform.machine() != "arm64":
            raise RuntimeError("Reflect requires an Apple silicon Mac; run from an ARM64 terminal.")
        if args.command == "setup":
            android_sdk.install(args.sdk)
            android_sdk.create_avd(args.sdk, STATE / "avd")
            print("Android is ready. Next: ./reflect build")
        elif args.command == "build":
            from .build import build
            build(args.sdk, STATE)
        elif args.command == "network-setup":
            from .network import setup
            setup(STATE)
        elif args.command == "doctor":
            import shutil
            for tool in ["python3", "cmake", "clang", "java"]:
                print(f"{tool}: {shutil.which(tool) or 'missing'}")
            print(f"Android SDK: {args.sdk}")
            for directory, *_ in android_sdk.PACKAGES:
                print(f"  {directory}: {'installed' if (args.sdk / directory).exists() else 'missing'}")
            for artifact in ["viewer/Reflect.app/Contents/MacOS/Reflect", "reflect-runtime.apk", "reflect-systemdriver.apk", "reflect-platform.apk", "reflect-demo.apk", "libVkLayer_REFRACT_runtime.so", "reflect-http-relay"]:
                print(f"  {artifact}: {'built' if (ROOT / 'build-reflect' / artifact).exists() else 'missing'}")
            print(f"Free space: {shutil.disk_usage(STATE if STATE.exists() else ROOT).free / 1e9:.1f} GB")
        elif args.command == "logs":
            for name in ["emulator", "viewer", "game", "http", "proxy"]:
                print(STATE / f"{name}.log")
            print(STATE / "http")
        else:
            from .runtime import Android, BUILD
            from .logs import LiveLogs
            android = Android(args.sdk, STATE)
            if args.command == "stop":
                with LiveLogs(STATE, args.log_mode):
                    android.stop()
                return
            with android.lock(), LiveLogs(STATE, args.log_mode):
                if args.command == "install":
                    android.install(args.apk, args.obb, args.owned)
                elif args.command == "list":
                    android.list()
                else:
                    if not (64 <= args.width <= 4096 and 64 <= args.height <= 4096 and 40 <= args.fps <= 250):
                        raise RuntimeError("Eye dimensions must be 64–4096, and FPS must be 40–250.")
                    if args.frames is not None and args.frames < 1 or args.seconds is not None and args.seconds <= 0:
                        raise RuntimeError("Frame and time limits must be positive.")
                    def interrupt(signum, frame):
                        raise KeyboardInterrupt
                    signal.signal(signal.SIGTERM, interrupt)
                    package = android.install(BUILD / "reflect-demo.apk") if args.command == "demo" else args.package
                    android.play(package, args.width, args.height, args.fps, args.capture, args.frames, args.seconds,
                                 http=args.http or args.http_dir is not None or args.http_file is not None,
                                 http_dir=args.http_dir, http_ports=args.http_ports, http_file=args.http_file)
    except (RuntimeError, OSError, subprocess.CalledProcessError) as error:
        print(f"reflect: {error}", file=sys.stderr)
        sys.exit(1)
    except KeyboardInterrupt:
        sys.exit(130)
