import json
import argparse
import sys


TOOLS = [
    {
        "name": "search_code",
        "description": "Search code using a deliberately verbose description that would be expensive when repeated.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": "The exact semantic search query to run against the code index.",
                },
                "limit": {
                    "type": "integer",
                    "description": "Maximum number of ranked code chunks to return to the caller.",
                    "default": 10,
                },
            },
            "required": ["query"],
        },
    },
    {
        "name": "read_symbol",
        "description": "Read a symbol implementation by fully qualified name.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "symbol": {
                    "type": "string",
                    "description": "Fully qualified symbol name.",
                }
            },
            "required": ["symbol"],
        },
    },
]


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--label", default="backend")
    cli_args = parser.parse_args()
    for line in sys.stdin:
        request = json.loads(line)
        request_id = request.get("id")
        method = request.get("method")
        if method == "initialize":
            respond(request_id, {"protocolVersion": "2025-06-18", "capabilities": {"tools": {}}})
        elif method == "tools/list":
            respond(request_id, {"tools": TOOLS})
        elif method == "tools/call":
            name = request["params"]["name"]
            tool_args = request["params"].get("arguments", {})
            respond(
                request_id,
                {
                    "content": [
                        {
                            "type": "text",
                            "text": f"{cli_args.label} called {name} with {json.dumps(tool_args, sort_keys=True)}",
                        }
                    ]
                },
            )
        else:
            error(request_id, -32601, f"Method not found: {method}")


def respond(request_id, result):
    sys.stdout.write(json.dumps({"jsonrpc": "2.0", "id": request_id, "result": result}) + "\n")
    sys.stdout.flush()


def error(request_id, code, message):
    sys.stdout.write(
        json.dumps({"jsonrpc": "2.0", "id": request_id, "error": {"code": code, "message": message}})
        + "\n"
    )
    sys.stdout.flush()


if __name__ == "__main__":
    main()
