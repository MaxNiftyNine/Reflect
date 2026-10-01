# Reflect

Reflect is a small command line fork of [Refract](https://github.com/MIXIDtheSilly/Refract) for **Apple silicon Macs**. Install an Android OpenXR APK, run it in a native Metal window, and control its simulated VR head and controllers with your keyboard and mouse.

Reflect runs an ARM64 Android emulator on the Apple CPU and GPU. It has no launcher interface, SteamVR integration, Meta login, store downloads, or ARM-to-x86 translator. The Android OpenXR runtime and local compatibility services come from Refract. This repository contains Reflect and its Android runtime dependencies.

## Start

Requirements: an Apple silicon Mac running macOS 12 or newer, Python 3.10 or newer, CMake, Java 21, and Xcode Command Line Tools. Android Studio is not needed. If CMake or Java is missing, Homebrew can install them:

```sh
brew install cmake openjdk@21
export JAVA_HOME="$(brew --prefix openjdk@21)/libexec/openjdk.jdk/Contents/Home"
```

From this directory:

```sh
./reflect setup
./reflect build
./reflect demo
```

`setup` downloads checksum-verified SDK packages from Google and creates a dedicated Android 14 ARM64 virtual device. Allow roughly 12 GB of disk space for the SDK, build outputs, and a small Android data partition; installed games need more. Downloads resume after interruption. `build` builds the native viewer, runtime/compatibility APKs, and an included OpenXR demo. The demo shows a room with colored cubes; the right trigger turns the center cube orange.

The SDK, emulator data, signing key, and logs live in `~/Library/Application Support/Reflect/`. Set `REFLECT_HOME` to choose another location. Use `./reflect --sdk /path/to/sdk ...` or `ANDROID_HOME` to use an existing SDK with the required versions. Reflect uses its own AVD directory even with a shared SDK.

## Install and play

```sh
./reflect install /path/to/game.apk
./reflect install /path/to/game.apk --obb /path/to/main.123.com.example.game.obb
./reflect list
./reflect run com.example.game
./reflect stop
```

The install command prints the package name and the exact play command. APKs need ARM64 libraries, or no native libraries. Install uses `adb install -r -g` to preserve app data and grant requested Android runtime permissions so hidden permission dialogs do not stop rendering; signature conflicts are reported without uninstalling the existing app. Original APK and OBB files stay in place. The Android data partition starts at 2 GB and can be enlarged in Reflect's `avd/reflect-arm64.avd/config.ini` while the emulator is stopped.

Reflect automatically configures its private Android device with the Quest brand/manufacturer identity needed by Unity's Oculus XR plugin. The first launch can reboot Android during this setup. A Vulkan compatibility layer expands descriptor template updates before they reach the Mac graphics driver. These changes do not rewrite game APKs.

Closing the window or pressing Ctrl-C stops the game. Android stays booted for the next game. `stop` shuts down Reflect's emulator after syncing its storage; saves remain in its virtual device. Reflect reserves emulator port 5580 and local pose/image ports 38490/38491, and reports a conflict if another program owns them.

### Live terminal logs

Android commands show new emulator, logcat, and viewer messages together by default, labeled `[emulator]`, `[logcat]`, and `[viewer]`. Existing log history is not replayed. Choose a mode before or after the command:

```sh
./reflect run com.TrassGames.Yeeps                  # all live logs
./reflect run com.TrassGames.Yeeps --no-logs        # status and errors only
./reflect run com.TrassGames.Yeeps --logcat         # Android logcat only
./reflect run com.TrassGames.Yeeps --emulator-logs  # emulator only
./reflect run com.TrassGames.Yeeps --unity          # logcat, Unity tag only
```

The equivalent selector is `--logs all|none|logcat|emulator|unity|http|unity-http`. `--no-log`, `--only-logcat`, and `--only-emulator` are aliases. Log files are saved in all modes, and Unity filtering affects only terminal output. `./reflect logs` prints their locations. An already-running emulator produces new output only when it has something to report.

Games using Meta platform calls can use the local compatibility service. Reflect reports every launched game as owned to this local service; an ownership list or `--owned` flag is not required. This does not sign into Meta or change ownership on Meta's servers. App compatibility remains experimental, as in Refract; a successful demo does not establish compatibility with every Quest title.

### HTTP/S request and response capture

Install the optional proxy once, then enable capture when launching a game:

```sh
./reflect network-setup
./reflect run com.TrassGames.Yeeps --http
```

`--http` (alias `--http-log`) saves complete request/response headers, duplicate headers, trailers, timestamps, errors and bodies. Live request summaries appear alongside the normal terminal logs. Use `--logs http` for only network logs, or `--no-logs` to save captures quietly.

To show Unity messages and network logs together:

```sh
./reflect run com.TrassGames.Yeeps --http --unity-network
```

`--unity-network` (alias `--unity-http`, or `--logs unity-http`) filters Android logcat to the Unity tag while also showing HTTP summaries and proxy diagnostics. `--http` enables the actual network capture.

To save the requests and responses directly to a mitmproxy-compatible file of your choice:

```sh
./reflect run com.TrassGames.Yeeps --http-file ./yeeps.mitm --unity-network
```

`--http-file` enables capture automatically and streams complete captured HTTP flows, including request/response headers and bodies, into that file. Choose a new filename each time; existing files are preserved. Close the game or press Ctrl-C to finish the capture cleanly. Open it with `mitmproxy -r ./yeeps.mitm` or `mitmweb -r ./yeeps.mitm`. Reflect's installed commands are under `~/Library/Application Support/Reflect/network-venv/bin/` if they aren't on your PATH. The separate JSON/body exports are also retained in the usual session folder.

Each session gets a folder under `~/Library/Application Support/Reflect/http/`. The exact path is printed at startup:

- `http.jsonl`: one JSON event per request, response or HTTP error, linked by flow ID.
- `bodies/`: complete binary request and response payloads. Compressed payloads also get a decoded copy when available; JSON events reference the files.
- `flows.mitm`: native mitmproxy capture, viewable with the installed `network-venv/bin/mitmproxy -r PATH/flows.mitm`.
- `http.log`, `proxy.log`, `relay.log`: summaries and diagnostics.

Choose another parent folder with `--http-dir ./captures` (also enables capture), or additional HTTP TCP ports with `--http --http-ports 80,443,8080`. Captures contain the actual headers and payloads, including any credentials sent by the game; the capture folders are private to your Mac user.

If the emulator lacks IPv6 NAT support, capture continues for IPv4 and prints a notice that IPv6 traffic is not captured. Each capture's `session.json` records the IP versions enabled.

Capture redirects only the launched game's Android UID, including native sockets, through a local proxy. HTTPS uses a temporary certificate store mount inside Reflect's Android guest; the Mac's certificate store and proxy settings are untouched. The redirect and temporary trust are removed on normal exit or Ctrl-C. If the process is forcibly killed, run `./reflect stop` before the next capture to reboot the guest and clear remaining rules/mounts.

This captures HTTP/S over the selected TCP ports (80 and 443 by default). Certificate pinning, custom TLS trust stores, QUIC/HTTP3, other ports and traffic from separate Android UIDs can prevent capture. Pinning is not bypassed; TLS failures appear in proxy diagnostics. Streaming HTTP bodies are buffered to preserve complete payloads, which can increase memory use and delay delivery for large responses. See the [mitmproxy certificate documentation](https://docs.mitmproxy.org/stable/concepts/certificates/) for HTTPS interception details.

## Controls

| Input | Action |
|---|---|
| Click the window | Capture mouse for looking around |
| Mouse movement | Turn and tilt the simulated head |
| Escape | Release mouse and clear held inputs |
| WASD / Shift | Walk / walk faster |
| Left click / E | Right trigger |
| Right click / R | Right grip |
| Q / F | Left trigger / left grip |
| Space or Enter / Backspace | Right A / B |
| Z / X | Left X / Y |
| Arrow keys | Right thumbstick |
| M | Left menu button |
| Mouse wheel | Change controller reach |
| Hold C | Put the right controller at the center of the view |
| Hold T | Extend both arms for calibration |
| Home | Reset position and viewing direction |
| Tab | Toggle one eye / stereo display |
| Command-Q | Quit the viewer |

Inputs are accepted while Reflect is focused. Switching to another app releases the mouse and clears held buttons. Hands follow the head; games requiring independent hand gestures may need additional controls.

## Diagnostics

```sh
./reflect doctor
./reflect logs
./reflect run com.example.game --width 768 --height 768 --fps 60
./reflect demo --capture /tmp/reflect-demo.png --frames 120 --seconds 30
```

`--capture` saves the first transported eye image. `--frames` and `--seconds` stop a diagnostic session automatically. `viewer.log` contains viewer/network errors; `game.log` contains Android logcat for the current session; `emulator.log` contains emulator startup errors. A crashed Android game is reported and its viewer is closed.

Graphics use RGBA pixel transport to avoid Windows/Nvidia shared textures. GLES uses direct GL readback with rows flipped into top-down order; Vulkan uses the runtime's staging readback. Vulkan scene and UI panel layers are transported together in a pixel atlas, including their orientation flags. GLES mixed layers still use the inherited fallback and can drop extra panels. H.264 and Windows shared GPU frames are not enabled. The title shows the resolution and measured display FPS. Throughput depends on eye resolution, rendering cost, and emulator transport; lower resolution can help.

To repeat the integration check after a build:

```sh
python3 -m macos.verify
python3 -m macos.verify --interactive
```

The first command installs and runs the real demo APK, checks its transported image and pose stream, and verifies session cleanup. The second also requires keyboard walking, mouse looking, and controller button input. Results go under `build-reflect/verification/`.

`python3 -m macos.package` writes a standalone fork to `dist/Reflect/` and `dist/Reflect-macos-arm64.zip`. It includes the built viewer, runtime APKs, CLI, and source; Windows launcher, headset bridge, and translator sources are omitted. That folder can run with Python 3 and the SDK installed by `setup`, without installing build tools.

## Source

```text
reflect                       CLI entry point
macos/cli.py                  command parsing
macos/sdk.py                  SDK download and ARM64 AVD setup
macos/build.py                native and Android APK builds
macos/runtime.py              install, launch, logging, and shutdown
macos/viewer.mm               Cocoa/Metal window and keyboard/mouse pose server
macos/demo/                   redistributable OpenXR demo APK
android-runtime*/             inherited Android OpenXR runtime and loader services
platform-sdk/                 inherited local platform compatibility services
protocol/                     inherited frame and pose records
```

See [CREDITS.md](CREDITS.md) and [LICENSE](LICENSE). Reflect's CLI/viewer/demo and inherited runtime are MIT licensed. The downloaded Khronos Android OpenXR loader is Apache 2.0; its license is included in its AAR under `META-INF/LICENSE`.
