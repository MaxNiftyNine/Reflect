"""mitmproxy subprocess addon: complete HTTP bodies and duplicate headers on disk."""
import json
import os
from pathlib import Path
import time

DIRECTORY = Path(os.environ["REFLECT_HTTP_DIRECTORY"])
SUMMARY = Path(os.environ["REFLECT_HTTP_SUMMARY"])


def record(event, flow, message=None):
    data = {"event": event, "id": flow.id, "time": time.time(),
            "method": flow.request.method, "url": flow.request.pretty_url}
    if message is not None:
        data.update(http_version=message.http_version,
                    headers=list(message.headers.items(multi=True)),
                    trailers=list(message.trailers.items(multi=True)) if message.trailers else [],
                    timestamp_start=message.timestamp_start, timestamp_end=message.timestamp_end)
        if message.raw_content is not None:
            filename = f"bodies/{flow.id}-{event}.bin"
            (DIRECTORY / filename).write_bytes(message.raw_content)
            data.update(body=filename, body_bytes=len(message.raw_content), body_encoding="raw; see Content-Encoding header")
            decoded = message.get_content(strict=False)
            if decoded is not None and decoded != message.raw_content:
                decoded_name = f"bodies/{flow.id}-{event}-decoded.bin"
                (DIRECTORY / decoded_name).write_bytes(decoded)
                data["decoded_body"] = decoded_name
        if event == "response":
            data.update(status=message.status_code, reason=message.reason)
    if event == "error":
        data["error"] = str(flow.error)
    with (DIRECTORY / "http.jsonl").open("a", encoding="utf-8") as stream:
        stream.write(json.dumps(data, ensure_ascii=True) + "\n")
    detail = str(data.get("status", data.get("error", "→")))
    # Avoid line/control-character injection from a remote URL or error string.
    line = f"{event} {data['method']} {data['url']} {detail}"
    with SUMMARY.open("a", encoding="utf-8") as stream:
        stream.write("".join(c if c.isprintable() else " " for c in line) + "\n")


def request(flow):
    record("request", flow, flow.request)


def response(flow):
    record("response", flow, flow.response)


def error(flow):
    record("error", flow)
