"""`python server.py login | status | logout`: the login, from a terminal.

With no arguments server.py is the MCP server and stdout is the protocol.
These commands are the same file run by a person, so they print.
"""

from __future__ import annotations

import argparse
import sys

import uc_auth


def _login(args) -> int:
    try:
        if args.paste:
            print("In a logged-in forum tab: F12 > Network > refresh > right-click the first\n"
                  "request > Copy > Copy as cURL. Paste it here, then press Enter on an "
                  "empty line:")
            lines = []
            for line in sys.stdin:
                if not line.strip() and lines:
                    break
                lines.append(line)
            cookies = uc_auth.parse_paste("".join(lines))
            state, detail, cookies = uc_auth.settle(cookies)
            if state != "ok":
                print(f"That login does not work: {detail}. Nothing was saved.")
                return 1
            uc_auth.save(cookies)
            print("Logged in" + (f" as {detail}" if detail else "") + ". Login saved.")
        else:
            uc_auth.sign_in(timeout=args.timeout, say=print, browser=args.browser)
    except uc_auth.LoginError as e:
        print(f"Login failed: {e}")
        return 1
    print("Done. A running server picks this up on its next request; nothing to restart.")
    return 0


def _logout(_args) -> int:
    gone = uc_auth.forget()
    print("\n".join(f"removed {p}" for p in gone) if gone else "No saved login to remove.")
    return 0


def run(argv: list[str], status) -> int:
    """`status` is the server's own session report, so both views agree."""
    parser = argparse.ArgumentParser(
        prog="server.py", description="uc-mcp. Run with no command to start the MCP server.")
    sub = parser.add_subparsers(dest="command", required=True)
    login = sub.add_parser("login", help="open a browser window and sign in to the forum")
    login.add_argument("--paste", action="store_true",
                       help="no browser on this machine: paste a copied request instead")
    login.add_argument("--browser", help="path of the Chrome, Edge or Brave to use")
    login.add_argument("--timeout", type=float, default=300,
                       help="seconds to wait for the login (default 300)")
    sub.add_parser("status", help="check the login with one live request")
    sub.add_parser("logout", help="delete the saved login and the sign-in profile")
    args = parser.parse_args(argv)
    if args.command == "login":
        return _login(args)
    if args.command == "logout":
        return _logout(args)
    report = status()
    print(report)
    return 1 if report.startswith("uc-mcp error") else 0
