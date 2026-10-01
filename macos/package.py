"""Create the standalone Reflect fork with source and built Mac/Android programs.

Usage: python3 -m macos.package
The result contains only Reflect's CLI/viewer and shared Android runtime components.
"""
import shutil
import zipfile

from .build import BUILD, ROOT, loader


def main():
    artifacts = [BUILD / "viewer/Reflect.app", *[BUILD / f"reflect-{name}.apk" for name in ["runtime","systemdriver","platform","demo"]], BUILD / "libVkLayer_REFRACT_runtime.so", BUILD / "reflect-http-relay"]
    for item in artifacts:
        if not item.exists():
            raise RuntimeError(f"Missing {item}; run ./reflect build first.")
    destination = ROOT / "dist/Reflect"
    destination.parent.mkdir(exist_ok=True)
    # Recreate only this generated package; never touch the SDK or emulator data.
    if destination.exists():
        shutil.rmtree(destination)
    destination.mkdir()
    excluded = shutil.ignore_patterns("__pycache__", "*.pyc")
    for name in ["macos","android-runtime","android-runtime-apk","protocol","platform-sdk","tools"]:
        shutil.copytree(ROOT / name,destination / name,ignore=excluded)
    for name in ["reflect","README.md","LICENSE","CREDITS.md"]:
        shutil.copy2(ROOT / name,destination / name)
    shutil.copy2(ROOT / "CMakeLists.txt", destination / "CMakeLists.txt")
    app = destination / "build-reflect/viewer/Reflect.app"
    app.parent.mkdir(parents=True)
    shutil.copytree(artifacts[0],app)
    for artifact in artifacts[1:]:
        shutil.copy2(artifact,destination / "build-reflect" / artifact.name)
    licenses = destination / "licenses"
    licenses.mkdir()
    shutil.copy2(loader() / "META-INF/LICENSE",licenses / "OpenXR-loader-LICENSE.txt")
    (destination / "README.md").write_text("# Ready to run\n\nThis folder includes the built Apple silicon viewer and runtime APKs. With Python 3 available, run `./reflect setup` once, then `./reflect demo`. For your own apps, use `./reflect install game.apk` and the printed run command. CMake, Java, and Xcode tools are needed only to rebuild the source.\n\n" + (destination / "README.md").read_text())
    archive = ROOT / "dist/Reflect-macos-arm64.zip"
    with zipfile.ZipFile(archive,"w",compression=zipfile.ZIP_DEFLATED) as output:
        for file in sorted(destination.rglob("*")):
            if file.is_file(): output.write(file,file.relative_to(destination.parent))
    print(f"Standalone fork: {destination}\nArchive: {archive}")


if __name__ == "__main__":
    main()
