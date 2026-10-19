"""Command line: ``agentwebview serve`` runs the dashboard and the agent endpoints; ``agentwebview mcp`` is the
stdio MCP server for clients that can only start a local process."""

from __future__ import annotations

import argparse
import logging
import os
import sys
import webbrowser
from pathlib import Path

from agentwebview import __version__
from agentwebview.config import Config, is_loopback


def _config(args: argparse.Namespace) -> Config:
    overrides: dict[str, object] = {}
    if getattr(args, "data", None):
        overrides["data_dir"] = Path(args.data).expanduser()
    for name in ("host", "port", "public_url"):
        value = getattr(args, name, None)
        if value is not None:
            overrides[name] = value
    if getattr(args, "allow_remote", False):
        overrides["allow_remote"] = True
    if getattr(args, "extra_host", None):
        overrides["extra_hosts"] = tuple(args.extra_host)
    return Config.from_env(**overrides)


def serve(args: argparse.Namespace) -> int:
    import uvicorn

    from agentwebview.api import create_app
    from agentwebview.service import Service

    config = _config(args)
    if not is_loopback(config.host) and not config.allow_remote:
        print(
            f"agentwebview: refusing to listen on {config.host}: the dashboard has no login, so anyone who can reach "
            "this address could change your keys and settings. Use --allow-remote only behind your own "
            "authentication (a VPN or a reverse proxy that requires a login).",
            file=sys.stderr,
        )
        return 2
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    service = Service(config)
    app = create_app(service)
    url = config.base_url
    print(f"\n  AgentWebView {__version__}\n  Dashboard:  {url}\n  MCP:        {url}/mcp\n  Data:       {config.data_dir}\n")
    if not is_loopback(config.host):
        print("  WARNING: listening beyond this computer with no login. Put it behind your own authentication.\n")
    if args.open:
        webbrowser.open(url)
    uvicorn.run(app, host=config.host, port=config.port, log_level="warning", proxy_headers=False)
    return 0


def mcp_stdio(args: argparse.Namespace) -> int:
    logging.basicConfig(level=logging.WARNING, stream=sys.stderr)  # stdout carries the protocol
    from agentwebview import mcp_server
    from agentwebview.errors import GatewayError
    from agentwebview.service import Service

    token = args.token or os.environ.get("AGENTWEBVIEW_TOKEN")
    config = _config(args)
    service = Service(config)
    try:
        service.authenticate(token)
    except GatewayError as exc:
        print(f"agentwebview mcp: {exc.message}", file=sys.stderr)
        return 2
    service.startup()
    mcp_server.configure(service, stdio_token=token)
    try:
        mcp_server.mcp.run("stdio")
    finally:
        service.shutdown()
    return 0


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(prog="agentwebview", description="AgentWebView: budgets, rules and receipts for AI agents.")
    parser.add_argument("--version", action="version", version=f"agentwebview {__version__}")
    sub = parser.add_subparsers(dest="command")

    p_serve = sub.add_parser("serve", help="run the dashboard, the REST API and the MCP endpoint")
    p_serve.add_argument("--host", help="address to listen on (default 127.0.0.1)")
    p_serve.add_argument("--port", type=int, help="port (default 8718)")
    p_serve.add_argument("--data", help="data folder (default ~/.agentwebview or $AGENTWEBVIEW_DATA)")
    p_serve.add_argument("--allow-remote", action="store_true", help="allow listening beyond loopback (no login!)")
    p_serve.add_argument("--extra-host", action="append", help="another Host name to answer to (repeatable)")
    p_serve.add_argument("--public-url", help="the URL agents use to reach this server (shown in setup instructions)")
    p_serve.add_argument("--open", action="store_true", help="open the dashboard in your browser")
    p_serve.set_defaults(func=serve)

    p_mcp = sub.add_parser("mcp", help="stdio MCP server for clients that start a local process")
    p_mcp.add_argument("--token", help="the agent's token (or $AGENTWEBVIEW_TOKEN)")
    p_mcp.add_argument("--data", help="data folder (default ~/.agentwebview or $AGENTWEBVIEW_DATA)")
    p_mcp.set_defaults(func=mcp_stdio)

    args = parser.parse_args(argv)
    if not getattr(args, "func", None):
        args = parser.parse_args(["serve", *(argv or sys.argv[1:])])
    raise SystemExit(args.func(args))


if __name__ == "__main__":  # pragma: no cover
    main()
