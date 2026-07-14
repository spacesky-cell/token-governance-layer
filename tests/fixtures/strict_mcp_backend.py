from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path


TOOLS = [
    {
        "name": "echo",
        "description": "Echo one value.",
        "inputSchema": {
            "type": "object",
            "properties": {"value": {"type": "string"}},
        },
    }
]


def emit(message: dict) -> None:
    sys.stdout.write(json.dumps(message) + "\n")
    sys.stdout.flush()


def record(path: Path | None, message: dict) -> None:
    if path is None:
        return
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(message) + "\n")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--mode", default="normal")
    parser.add_argument("--events", type=Path)
    parser.add_argument("--release", type=Path)
    args = parser.parse_args()
    catalog_generation = 0
    for line in sys.stdin:
        message = json.loads(line)
        record(args.events, message)
        method = message.get("method")
        request_id = message.get("id")
        if method == "notifications/initialized" and args.mode == "never-read":
            time.sleep(30)
            continue
        if method == "initialize":
            version = "2024-11-05" if args.mode == "incompatible" else "2025-06-18"
            emit(
                {
                    "jsonrpc": "2.0",
                    "id": request_id,
                    "result": {
                        "protocolVersion": version,
                        "capabilities": {"tools": {"listChanged": True}},
                    },
                }
            )
        elif method == "tools/list":
            if args.mode == "slow-list":
                deadline = time.monotonic() + 5
                while args.release is not None and not args.release.exists() and time.monotonic() < deadline:
                    time.sleep(0.01)
            if args.mode == "interleaved":
                emit({"jsonrpc": "2.0", "method": "notifications/progress", "params": {}})
            tool = dict(TOOLS[0])
            tool["description"] = f"Echo one value. generation={catalog_generation}"
            emit({"jsonrpc": "2.0", "id": request_id, "result": {"tools": [tool]}})
            if args.mode == "list-change" and catalog_generation == 0:
                catalog_generation = 1
                emit({"jsonrpc": "2.0", "method": "notifications/tools/list_changed"})
        elif method == "tools/call":
            if args.mode == "timeout":
                continue
            if args.mode == "stderr-flood":
                secret = "ghp_" + "S" * 40
                private_key = "-----BEGIN PRIVATE KEY-----\nprivate-material\n-----END PRIVATE KEY-----"
                sys.stderr.write(("noise\n" * 20000) + f"token={secret}\n{private_key}\n")
                sys.stderr.flush()
            if args.mode == "pem-chunked":
                for chunk in (
                    "-----BEGIN PRIVATE KEY-----\n",
                    "chunked-private-material\n",
                    "-----END PRIVATE KEY-----\n",
                ):
                    sys.stderr.write(chunk)
                    sys.stderr.flush()
                    time.sleep(0.05)
            if args.mode == "error-secret":
                secret = "ghp_" + "E" * 40
                private_key = "-----BEGIN PRIVATE KEY-----\nerror-private-material\n-----END PRIVATE KEY-----"
                emit(
                    {
                        "jsonrpc": "2.0",
                        "id": request_id,
                        "error": {"code": -32001, "message": f"token={secret} {private_key}"},
                    }
                )
                continue
            emit(
                {
                    "jsonrpc": "2.0",
                    "id": request_id,
                    "result": {
                        "content": [{"type": "text", "text": "backend result"}],
                        "isError": args.mode == "tool-error",
                    },
                }
            )


if __name__ == "__main__":
    main()
