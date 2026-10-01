"""Run the complete installed APK -> Android OpenXR -> Metal window smoke check.

Usage: python3 -m macos.verify
The native keyboard/mouse check is interactive: use the controls while the demo runs.
"""
import json
from pathlib import Path
import socket
import struct
import subprocess
import threading
import time
import zlib

from .cli import ROOT, STATE


def png_pixels(file):
    data = file.read_bytes()
    if data[:8] != b"\x89PNG\r\n\x1a\n":
        raise RuntimeError("The viewer did not write a PNG")
    cursor, compressed = 8, b""
    while cursor < len(data):
        size = struct.unpack_from(">I", data, cursor)[0]
        kind = data[cursor+4:cursor+8]
        chunk = data[cursor+8:cursor+8+size]
        if kind == b"IHDR":
            width, height, depth, color, *_ = struct.unpack(">IIBBBBB", chunk)
            if depth != 8 or color not in (2,6):
                raise RuntimeError("Expected an 8-bit RGB/RGBA PNG")
        if kind == b"IDAT": compressed += chunk
        cursor += size+12
    raw = zlib.decompress(compressed)
    stride, bpp = width * (4 if color == 6 else 3), (4 if color == 6 else 3)
    previous = bytearray(stride)
    pixels = bytearray()
    offset = 0
    for _ in range(height):
        filter = raw[offset]; row = bytearray(raw[offset+1:offset+1+stride]); offset += stride+1
        for i in range(stride):
            left = row[i-bpp] if i >= bpp else 0
            above = previous[i]
            corner = previous[i-bpp] if i >= bpp else 0
            if filter == 1: prediction = left
            elif filter == 2: prediction = above
            elif filter == 3: prediction = (left+above)//2
            elif filter == 4:
                p = left+above-corner
                distances = [abs(p-left),abs(p-above),abs(p-corner)]
                prediction = [left,above,corner][distances.index(min(distances))]
            elif filter == 0: prediction = 0
            else: raise RuntimeError("Invalid PNG filter")
            row[i] = (row[i]+prediction)&255
        pixels.extend(row); previous = row
    return width, height, bpp, pixels


def main():
    import argparse
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--interactive", action="store_true", help="run for 30 seconds and require real keyboard/mouse input")
    args = parser.parse_args()
    directory = ROOT / "build-reflect/verification"
    directory.mkdir(parents=True, exist_ok=True)
    capture = directory / "smoke.png"
    capture.unlink(missing_ok=True)
    command = [str(ROOT / "reflect"), "demo", "--width", "512", "--height", "512", "--capture", str(capture), "--seconds", "35" if args.interactive else "20"]
    if not args.interactive: command += ["--frames", "120"]
    report = {"pose_records":0,"walk":False,"look":False,"trigger":False,"primary":False}
    done = threading.Event()
    def watch():
        initial = None
        deadline = time.monotonic()+60
        while not done.is_set() and time.monotonic() < deadline:
            try:
                with socket.create_connection(("127.0.0.1",38490),timeout=1) as client:
                    while not done.is_set():
                        data = b""
                        while len(data)<2600:
                            chunk=client.recv(2600-len(data))
                            if not chunk: raise EOFError()
                            data+=chunk
                        if struct.unpack_from("<IHH",data) != (0x54434652,6,1):
                            raise RuntimeError("Wrong pose protocol")
                        head=struct.unpack_from("<7f",data,24)
                        hand=struct.unpack_from("<II4f",data,136)
                        initial=initial or head
                        report["pose_records"]+=1
                        report["walk"] |= abs(head[0]-initial[0])+abs(head[2]-initial[2])>.01
                        report["look"] |= abs(head[4]-initial[4])+abs(head[3]-initial[3])>.01
                        report["trigger"] |= hand[2]>.5
                        report["primary"] |= bool(hand[1]&1)
            except (OSError,EOFError):
                time.sleep(.1)
    thread=threading.Thread(target=watch,daemon=True); thread.start()
    if args.interactive:
        print("In the demo: press W, press E, press Space, click to capture the mouse, move it, then Escape.",flush=True)
    try:
        subprocess.run(command,check=True,timeout=300)
    finally:
        done.set(); thread.join(timeout=3)
    width,height,bpp,pixels=png_pixels(capture)
    colors={tuple(pixels[i:i+3]) for i in range(0,len(pixels),bpp)}
    if (width,height)!=(512,512) or len(colors)<10:
        raise RuntimeError("The APK did not produce a rendered scene")
    report.update({"eye_width":width,"eye_height":height,"unique_colors":len(colors),"apk":"com.reflect.demo","renderer":"native Metal viewer","capture":str(capture)})
    if report["pose_records"]<10:
        raise RuntimeError("No usable pose stream")
    if args.interactive and not all(report[key] for key in ["walk","look","trigger","primary"]):
        raise RuntimeError(f"Input check incomplete: {report}")
    (directory / "smoke.json").write_text(json.dumps(report,indent=2)+"\n")
    if (STATE / "session.json").exists():
        raise RuntimeError("Session record was not cleaned up")
    print("Reflect integration check passed.",flush=True)


if __name__ == "__main__":
    main()
