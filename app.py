#!/usr/bin/env python3
"""MSSQL-MCP Server — session-based SQL Server access over HTTP.
"""

import logging
import logging.handlers
import os
import sys

import uvicorn
from fastmcp import FastMCP
from fastmcp.server.http import create_streamable_http_app
from starlette.middleware import Middleware
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request

from agent.session_manager import SessionRegistry, UserIdentity, current_user
from agent.tools import register_tools

LOG_DIR = os.environ.get("LOG_DIR", "/app/logs")
LOG_MAX_BYTES = int(os.environ.get("LOG_MAX_BYTES", 10 * 1024 * 1024))
LOG_BACKUP_COUNT = int(os.environ.get("LOG_BACKUP_COUNT", 5))
DB_PASSWORD_IDLE_TTL_SECONDS = int(
    os.environ.get("DB_PASSWORD_IDLE_TTL_SECONDS", "3600")
)


class AuthMiddleware(BaseHTTPMiddleware):
    """Resolve flexible per-caller DB credentials from request headers.

    Both X-DB-User and X-DB-Password are optional. Whatever is missing is
    collected later through MCP elicitation by the connect tool. When no
    username is supplied, the MCP session id is used as a stable per-client key
    so elicited credentials persist across calls and stay isolated between
    concurrent clients.
    """

    async def dispatch(self, request: Request, call_next):  # type: ignore[override]
        username = request.headers.get("x-db-user", "").strip()
        password = request.headers.get("x-db-password", "")

        peer_ip = request.client.host if request.client else ""
        xff = request.headers.get("x-forwarded-for", "")
        forwarded_for = (
            xff.split(",")[0].strip() if xff else request.headers.get("x-real-ip", "")
        )

        token = current_user.set(
            UserIdentity(
                username=username,
                password=password,
                peer_ip=peer_ip,
                forwarded_for=forwarded_for,
            )
        )
        try:
            return await call_next(request)
        finally:
            current_user.reset(token)


def _setup_logging() -> None:
    log_level = os.environ.get("LOG_LEVEL", "INFO").upper()
    os.makedirs(LOG_DIR, exist_ok=True)

    fmt = logging.Formatter("%(asctime)s [%(name)s] %(levelname)s %(message)s")

    root = logging.getLogger("mssql-mcp")
    root.setLevel(log_level)
    root.propagate = False

    stderr_handler = logging.StreamHandler(sys.stderr)
    stderr_handler.setFormatter(fmt)
    root.addHandler(stderr_handler)

    file_handler = logging.handlers.RotatingFileHandler(
        os.path.join(LOG_DIR, "mssql-mcp.log"),
        maxBytes=LOG_MAX_BYTES,
        backupCount=LOG_BACKUP_COUNT,
    )
    file_handler.setFormatter(fmt)
    root.addHandler(file_handler)

    # Dedicated audit trail — one block per connect/query/disconnect event.
    audit = logging.getLogger("mssql-mcp.audit")
    audit.setLevel(logging.INFO)
    audit.propagate = False
    audit_handler = logging.handlers.RotatingFileHandler(
        os.path.join(LOG_DIR, "mssql-mcp-audit.log"),
        maxBytes=LOG_MAX_BYTES,
        backupCount=LOG_BACKUP_COUNT,
    )
    audit_handler.setFormatter(logging.Formatter("%(message)s"))
    audit.addHandler(audit_handler)


def main() -> None:
    _setup_logging()
    logger = logging.getLogger("mssql-mcp")

    port = int(os.environ.get("MCPO_PORT", "8007"))
    logger.info("PORT=%d", port)

    registry = SessionRegistry(
        password_idle_ttl_seconds=DB_PASSWORD_IDLE_TTL_SECONDS
    )
    mcp = FastMCP(name="mssql-mcp")
    register_tools(mcp, registry)

    app = create_streamable_http_app(
        server=mcp,
        streamable_http_path="/mcp",
        middleware=[Middleware(AuthMiddleware)],
    )

    logger.info("mssql-mcp starting (streamable-http on port %d)", port)
    uvicorn.run(app, host=os.environ.get("MCP_BIND_HOST", "0.0.0.0"), port=port)


if __name__ == "__main__":
    main()
