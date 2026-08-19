"""``python -m marketd`` - run the server.

    python -m marketd serve --port 8080
    python -m marketd routes
    python -m marketd config
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from dataclasses import replace

from .api.app import create_app
from .bootstrap import build_container, serve
from .config import Settings
from .util import jsonx


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="marketd", description="a miniature trading venue")
    sub = parser.add_subparsers(dest="command")

    run = sub.add_parser("serve", help="run the HTTP server")
    run.add_argument("--host", default=None)
    run.add_argument("--port", type=int, default=None)
    run.add_argument("--log-level", default=None)

    sub.add_parser("routes", help="print the route table")
    sub.add_parser("config", help="print effective settings")

    args = parser.parse_args(argv)
    command = args.command or "serve"

    settings = Settings.from_env()
    if command == "serve":
        overrides = {
            key: value
            for key, value in (
                ("host", args.host), ("port", args.port), ("log_level", args.log_level)
            )
            if value is not None
        }
        if overrides:
            settings = replace(settings, **overrides)
        try:
            asyncio.run(serve(settings))
        except KeyboardInterrupt:
            pass
        return 0

    container = build_container(settings)
    if command == "routes":
        create_app(container)  # registering the routes is what fills the table
        print(jsonx.dumps_str(container.router.describe()))
    elif command == "config":
        print(jsonx.dumps_str(settings.redacted()))
    return 0


if __name__ == "__main__":
    sys.exit(main())
