# Credits

Reflect is an Apple silicon command line fork of [Refract](https://github.com/MIXIDtheSilly/Refract)
(MIXIDtheSilly), based on [AXRB](https://github.com/TheReal-Flo/AXRB)
(Florian Reintgen). Its Android OpenXR runtime, loader services, platform compatibility
service, and wire protocol are inherited from those projects. Their MIT copyright
notices are retained in [LICENSE](LICENSE).

Reflect adds the Cocoa/Metal viewer, keyboard and mouse controls, Mac setup/build/install
tools, OpenXR demo, and optional HTTP/S capture.

The downloaded [Khronos Android OpenXR loader](https://github.com/KhronosGroup/OpenXR-SDK-Source)
is licensed under Apache 2.0. Its license is retained in the downloaded AAR at
`META-INF/LICENSE` and copied into standalone packages by `python3 -m macos.package`.
OpenXR declarations retain their source notices. Optional HTTP capture uses
[mitmproxy](https://github.com/mitmproxy/mitmproxy), an MIT-licensed dependency installed
separately by `./reflect network-setup`.
