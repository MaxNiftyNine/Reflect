"""Build Reflect using CMake, the Android NDK, and Android's standalone APK tools."""
import hashlib
import os
from pathlib import Path
import shutil
import subprocess
import urllib.request
import zipfile

ROOT = Path(__file__).resolve().parents[1]
BUILD = ROOT / "build-reflect"
LOADER_URL = "https://repo.maven.apache.org/maven2/org/khronos/openxr/openxr_loader_for_android/1.1.59/openxr_loader_for_android-1.1.59.aar"
LOADER_SHA256 = "01ec8ba10137ddec67769d0e9b36f321478f3b660d8e3e65e66db0b857c008a0"


def call(*args, **kwargs):
    return subprocess.run([str(a) for a in args], check=True, **kwargs)


def loader():
    destination = ROOT / "third_party/reflect-openxr-loader"
    if not (destination / ".sha256").exists():
        destination.mkdir(parents=True, exist_ok=True)
        with urllib.request.urlopen(LOADER_URL, timeout=60) as response:
            data = response.read()
        if hashlib.sha256(data).hexdigest() != LOADER_SHA256:
            raise RuntimeError("OpenXR loader checksum mismatch")
        import io
        with zipfile.ZipFile(io.BytesIO(data)) as archive:
            archive.extractall(destination)
        (destination / ".sha256").write_text(LOADER_SHA256)
    return destination


def build(sdk, state):
    sdk = sdk.expanduser().resolve()
    ndk = sdk / "ndk/27.3.13750724"
    tools = sdk / "build-tools/36.0.0"
    android_jar = sdk / "platforms/android-29/android.jar"
    toolchain = ndk / "build/cmake/android.toolchain.cmake"
    for required in [toolchain, tools / "aapt2", android_jar]:
        if not required.exists():
            raise RuntimeError(f"Missing {required}. Run ./reflect setup first.")
    if not shutil.which("cmake"):
        raise RuntimeError("CMake is required. Install it with: brew install cmake")
    java_home = os.environ.get("JAVA_HOME")
    if not java_home:
        detected = subprocess.run(["/usr/libexec/java_home", "-v", "21"], capture_output=True, text=True)
        if detected.returncode:
            raise RuntimeError("Java 21 is required. Install a JDK and set JAVA_HOME to its Contents/Home directory.")
        java_home = detected.stdout.strip()
    java = Path(java_home) / "bin"
    environment = dict(os.environ, JAVA_HOME=java_home)
    state.mkdir(parents=True, exist_ok=True)
    keystore = state / "debug.keystore"
    if not keystore.exists():
        call(java / "keytool", "-genkeypair", "-keystore", keystore, "-storepass", "android", "-keypass", "android", "-alias", "androiddebugkey", "-keyalg", "RSA", "-keysize", "2048", "-validity", "10000", "-dname", "CN=Reflect Debug,O=Reflect,C=US")
        keystore.chmod(0o600)
    cmake_arguments = [f"-DCMAKE_TOOLCHAIN_FILE={toolchain}", "-DANDROID_ABI=arm64-v8a", "-DANDROID_PLATFORM=android-29", "-DCMAKE_BUILD_TYPE=Release"]
    jobs = str(min(os.cpu_count() or 4, 8))
    call("cmake", "-S", ROOT / "macos", "-B", BUILD / "viewer", "-DCMAKE_BUILD_TYPE=Release")
    call("cmake", "--build", BUILD / "viewer", "-j", jobs)
    call("cmake", "-S", ROOT, "-B", BUILD / "runtime", *cmake_arguments)
    call("cmake", "--build", BUILD / "runtime", "-j", jobs)
    call("cmake", "-S", ROOT / "platform-sdk/native", "-B", BUILD / "platform", *cmake_arguments)
    call("cmake", "--build", BUILD / "platform", "-j", jobs)
    compiler = ndk / "toolchains/llvm/prebuilt/darwin-x86_64/bin/aarch64-linux-android29-clang"
    call(str(compiler) + "++", "-std=c++17", "-O2", "-static-libstdc++", "-fPIE", "-pie",
         ROOT / "macos/network/relay.cpp", "-o", BUILD / "reflect-http-relay")
    driver = BUILD / "librefract_driver.so"
    call(compiler, "-shared", "-fPIC", "-O2", "-o", driver, ROOT / "android-runtime-apk/systemdriver/refract_driver.c", "-llog", "-ldl")
    call(str(compiler) + "++", "-std=c++17", "-shared", "-fPIC", "-O2", "-static-libstdc++", "-Wl,-Bsymbolic",
         ROOT / "macos/vulkan-compat/android_vulkan_layer.cpp", "-llog", "-o", BUILD / "libVkLayer_REFRACT_runtime.so")
    runtime = BUILD / "runtime/android-runtime/libopenxr_runtime.so"

    def apk(name, manifest, sources=None, libraries=(), resources=None):
        work = BUILD / name
        work.mkdir(parents=True, exist_ok=True)
        for folder in ["classes", "dex", "resources", "gen"]:
            shutil.rmtree(work / folder, ignore_errors=True)
            (work / folder).mkdir()
        text = manifest.read_text().replace("Refract", "Reflect")
        (work / "AndroidManifest.xml").write_text(text)
        flags = []
        if resources:
            call(tools / "aapt2", "compile", "--dir", resources, "-o", work / "resources")
            flags = list((work / "resources").glob("*.flat"))
        call(tools / "aapt2", "link", "-I", android_jar, "--manifest", work / "AndroidManifest.xml", "--java", work / "gen", "-o", work / "unsigned.apk", *flags)
        if sources:
            files = list(sources.rglob("*.java")) + list((work / "gen").rglob("*.java"))
            call(java / "javac", "-source", "8", "-target", "8", "-Xlint:-options", "-bootclasspath", android_jar, "-d", work / "classes", *files)
            call(tools / "d8", "--min-api", "29", "--output", work / "dex", *list((work / "classes").rglob("*.class")), env=environment)
        with zipfile.ZipFile(work / "unsigned.apk", "a", compression=zipfile.ZIP_DEFLATED) as archive:
            if sources:
                archive.write(work / "dex/classes.dex", "classes.dex")
            for library in libraries:
                archive.write(library, f"lib/arm64-v8a/{library.name}")
        call(tools / "zipalign", "-f", "-p", "4", work / "unsigned.apk", work / "aligned.apk")
        output = BUILD / f"{name}.apk"
        call(tools / "apksigner", "sign", "--ks", keystore, "--ks-pass", "pass:android", "--key-pass", "pass:android", "--out", output, work / "aligned.apk", env=environment)
        call(tools / "apksigner", "verify", output, env=environment)
        print(f"Built {output}", flush=True)

    app = ROOT / "android-runtime-apk"
    apk("reflect-runtime", app / "AndroidManifest.xml", app / "src", [runtime], app / "res")
    apk("reflect-systemdriver", app / "systemdriver/AndroidManifest.xml", app / "systemdriver/src", [runtime, driver])
    apk("reflect-platform", ROOT / "platform-sdk/apk/AndroidManifest.xml", ROOT / "platform-sdk/apk/src", [BUILD / "platform/librefract_ovrplatform.so"])
    openxr = loader()
    call("cmake", "-S", ROOT / "macos/demo", "-B", BUILD / "demo-native", *cmake_arguments, f"-DOPENXR_LOADER_DIR={openxr}")
    call("cmake", "--build", BUILD / "demo-native", "-j", jobs)
    apk("reflect-demo", ROOT / "macos/demo/AndroidManifest.xml", libraries=[BUILD / "demo-native/libreflect_demo.so", openxr / "jni/arm64-v8a/libopenxr_loader.so"])
    print("Reflect built. Run ./reflect demo to install and play the sample.")
