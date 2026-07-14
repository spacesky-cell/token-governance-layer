from __future__ import annotations

import argparse
import json
import sys
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
    args = parser.parse_args()
    catalog_generation = 0
    for line in sys.stdin:
        message = json.loads(line)
        record(args.events, message)
        method = message.get("method")
        request_id = message.get("id")
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
                sys.stderr.write(("noise\n" * 20000) + f"token={secret}\n")
                sys.stderr.flush()
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
