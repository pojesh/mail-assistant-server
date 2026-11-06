"""Run the API locally: `uv run python -m scripts.dev_server [--port 8000]`.

On Windows, psycopg's async mode (used by LangGraph's Postgres checkpointer) cannot run on
the default ProactorEventLoop, so this starts Uvicorn on a SelectorEventLoop. On Linux and
macOS (and on Cloud Run) it behaves exactly like `uvicorn app.main:app`.
"""

import argparse
import asyncio
import selectors
import sys

import uvicorn


def selector_loop() -> asyncio.AbstractEventLoop:
    return asyncio.SelectorEventLoop(selectors.SelectSelector())


def main() -> None:
    parser = argparse.ArgumentParser(description="Run the Mail Assistant API locally.")
    parser.add_argument("--port", type=int, default=8000)
    parser.add_argument("--host", default="127.0.0.1")
    args = parser.parse_args()

    config = uvicorn.Config("app.main:app", host=args.host, port=args.port, access_log=False)
    server = uvicorn.Server(config)
    if sys.platform == "win32":
        asyncio.run(server.serve(), loop_factory=selector_loop)
    else:
        asyncio.run(server.serve())


if __name__ == "__main__":
    main()
