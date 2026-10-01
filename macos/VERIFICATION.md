# Reflect verification — 2026-10-01

Verified on this Apple M1 Mac running macOS 27.0. The viewer is an ARM64 Mach-O executable with a macOS 12 deployment target. Older macOS versions have not been tested here.

| Requirement | Observed result |
|---|---|
| New CLI app named Reflect | `reflect` commands run from both the worktree and the standalone `dist/Reflect` folder. The native window identifies itself as Reflect. |
| Apple silicon execution | The emulator is `qemu-system-aarch64-headless`; Android reports `arm64-v8a`; the Android GPU renderer and Vulkan physical device identify Apple M1. |
| Install APKs | Installed the built `com.reflect.demo` APK and the independent Khronos `org.khronos.openxr.hello_xr.vulkan` APK through the CLI. Both remain installed across an emulator stop/restart. |
| Run in a window | Inspected the native Metal window and transported captures. GLES and Vulkan scenes render with the correct vertical orientation. |
| Keyboard and mouse controls | The final interactive verifier received 2,826 pose records and observed walking, head rotation, right trigger, and primary button input. Android's OpenXR action logs also recorded trigger presses and changed view positions. |
| Strip launcher/headset/account features | The standalone archive has no upstream launcher, host bridge, Windows viewer, translator, or scripts directories. It contains the CLI, native viewer, Android runtime/compatibility APKs, source, and licenses. |
| Build the standalone fork | `dist/Reflect/reflect build` successfully rebuilt the viewer, runtime, system driver, platform service, and demo using only source in that folder and the external Android SDK. Its rebuilt demo ran successfully. |
| Shutdown | The frame-limited demo exits and removes its session record. `reflect stop` also stopped a running standalone package session and its dedicated emulator. A subsequent cold boot and installation succeeded. |

Repeatable commands:

```sh
./reflect setup
./reflect build
python3 -m macos.verify
python3 -m macos.verify --interactive
python3 -m macos.package
```

The final interactive check reported a 512×512 captured eye with 385 distinct colors, plus `walk`, `look`, `trigger`, and `primary` all true. Machine-local images, logs, and JSON results are under `build-reflect/verification/`.

The independent Vulkan APK came from KhronosGroup/OpenXR-SDK-Source release 1.1.59.1. It is a verification input, not bundled with Reflect. These checks establish a working installation/rendering/input path; they do not establish compatibility with every Quest game. GLES mixed scene/panel composition and independent hand gestures retain the limitations described in the README.

## Yeeps startup fixes

Yeeps 2.29.6a (`com.TrassGames.Yeeps`) initially stopped with Unity's “non-Oculus device” error. After setting the private guest's Quest identity, the Mac's Vulkan driver stalled in a descriptor template update. Reflect now loads the inherited Refract Vulkan compatibility layer, which expands those updates into ordinary descriptor writes. Game APKs remain unchanged.

Yeeps then submitted its background and two UI quad layers. The previous pixel fallback dropped the UI. The new Vulkan pixel atlas carries the scene and all packed panels together and honors their vertical orientation flags. The native viewer now draws from its main-thread timer to avoid missing MTKView display callbacks.

On this M1, a cold Android restart followed by `./reflect run com.TrassGames.Yeeps` reached the upright Yeeps logo and readable Terms of Service screen. The native title showed 1024×1024 and approximately 10–14 displayed FPS during inspection. Terms were not accepted; gameplay, authentication, and multiplayer beyond this screen have not been verified.

Automatic compatibility setup was checked by restoring the guest's original Google identity and calling `Android.ready()`: it configured and rebooted the guest, then verified `Oculus` and `Quest 3`. The SDK's original build properties remain unchanged. The standalone package's noninteractive `python3 -m macos.verify` also passed after these changes.
