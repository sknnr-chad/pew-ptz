"""
pew_ptz.cli — command line for the server and the supervisor.

    python -m pew_ptz [--port N] [--log-dir DIR]
    python -m pew_ptz.supervise [--port N] [--log-dir DIR]

The options override PEW_PTZ_SERVER_PORT and PEW_PTZ_LOG_DIR, which in turn
override .env. They're applied to the environment before pew_ptz.server is
imported, because the server reads its settings at import time.
"""

from __future__ import annotations

import argparse
import os

from pew_ptz.exitcodes import EXIT_CONFIG


class _Parser(argparse.ArgumentParser):
    def error(self, message):
        # argparse exits 2 by default, which Python also uses for its own
        # usage errors; the supervisor needs to know this is bad config.
        self.print_usage()  # goes nowhere under pythonw, which has no stderr
        self.exit(EXIT_CONFIG, f"{self.prog}: error: {message}\n")


def parse_args(argv: list[str] | None = None, prog: str = "pew-ptz") -> argparse.Namespace:
    parser = _Parser(prog=prog, description="Phone-based PTZ camera and Zoom controller.")
    parser.add_argument("--port", type=int, help="HTTP port (default 8080)")
    parser.add_argument("--log-dir", help="write server.log here (default: stdout only)")
    return parser.parse_args(argv)


def apply_to_env(args: argparse.Namespace) -> None:
    if args.port is not None:
        os.environ["PEW_PTZ_SERVER_PORT"] = str(args.port)
    if args.log_dir:
        os.environ["PEW_PTZ_LOG_DIR"] = args.log_dir


def main(argv: list[str] | None = None) -> int:
    apply_to_env(parse_args(argv))
    from pew_ptz import version

    version.STARTUP_BUILD = version.disk_build()
    from pew_ptz import server

    return server.main()
