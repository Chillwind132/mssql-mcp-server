"""MCP tool definitions — session-based SQL Server access.

A connect tool elicits the DB password once per cached session and returns a
session_id; every query tool takes that session_id. Read vs. write access is
enforced by the SQL Server login's own permissions, not by an in-process policy
layer.

"""

import csv
import io
import json
import logging
from typing import Annotated, Any, List, Tuple

from fastmcp import Context, FastMCP
from pydantic import Field

from .session_manager import SessionRegistry

logger = logging.getLogger("mssql-mcp.tools")


# ----------------------------------------------------------------------------
# Result formatting helpers
# ----------------------------------------------------------------------------
def format_table(headers: List[str], rows: List[Tuple[Any, ...]]) -> str:
    if not headers:
        return "(no columns)"
    if not rows:
        return "(no rows)"

    str_rows = []
    widths = [len(h) for h in headers]
    for row in rows:
        str_row = []
        for i, cell in enumerate(row):
            if cell is None:
                s = "NULL"
            elif isinstance(cell, bool):
                s = "true" if cell else "false"
            elif isinstance(cell, (bytes, bytearray)):
                s = "<binary>"
            else:
                s = str(cell)
            str_row.append(s)
            widths[i] = max(widths[i], len(s))
        str_rows.append(str_row)

    sep = " | "
    header_row = sep.join(h.ljust(widths[i]) for i, h in enumerate(headers))
    divider = "-+-".join("-" * w for w in widths)
    body = [sep.join(cell.ljust(widths[i]) for i, cell in enumerate(r)) for r in str_rows]
    return "\n".join([header_row, divider] + body)


def format_json(headers: List[str], rows: List[Tuple[Any, ...]]) -> str:
    result = []
    for row in rows:
        obj = {}
        for i, header in enumerate(headers):
            value = row[i] if i < len(row) else None
            if isinstance(value, (bytes, bytearray)):
                obj[header] = "<binary>"
            elif hasattr(value, "isoformat"):
                obj[header] = value.isoformat()
            else:
                obj[header] = value
        result.append(obj)
    return json.dumps(result, indent=2, default=str)


def format_csv(headers: List[str], rows: List[Tuple[Any, ...]]) -> str:
    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow(headers)
    for row in rows:
        str_row = []
        for cell in row:
            if cell is None:
                str_row.append("")
            elif isinstance(cell, (bytes, bytearray)):
                str_row.append("<binary>")
            elif hasattr(cell, "isoformat"):
                str_row.append(cell.isoformat())
            else:
                str_row.append(str(cell))
        writer.writerow(str_row)
    return output.getvalue()


def result_summary(headers: List[str], rows: List[Tuple[Any, ...]]) -> str:
    return f"{len(rows)} row(s), {len(headers)} column(s)"


def _render(columns: List[str], rows: List[Tuple[Any, ...]], fmt: str) -> str:
    fmt = fmt.lower()
    if fmt == "json":
        return format_json(columns, rows)
    if fmt == "csv":
        return format_csv(columns, rows)
    return format_table(columns, rows) if columns else "(no result)"


def _is_auth_failure(result: Any) -> bool:
    """Heuristically detect a SQL Server authentication failure in a result."""
    if not isinstance(result, dict):
        return False
    err = str(result.get("error", "")).lower()
    return any(
        marker in err
        for marker in ("login failed", "18456", "28000", "password did not match")
    )


def register_tools(mcp: FastMCP, sm: SessionRegistry) -> None:
    async def _elicit(ctx: Context, message: str, what: str) -> Any:
        """Prompt the caller for a single value. Returns the string, or an error
        dict if the prompt was cancelled or elicitation is unavailable."""
        try:
            result = await ctx.elicit(message=message, response_type=str)
            if result.action != "accept":
                return {"status": "cancelled", "message": f"DB {what} entry cancelled"}
        except Exception:
            return {"error": f"Elicitation unavailable - cannot prompt for DB {what}"}

        value = result.data if hasattr(result, "data") and result.data else ""
        if not value:
            return {"error": f"No DB {what} provided"}
        return str(value)

    async def _elicit_username(ctx: Context) -> Any:
        return await _elicit(
            ctx,
            "Enter the SQL Server username (login) to connect with.",
            "username",
        )

    async def _elicit_password(ctx: Context, username: str) -> Any:
        return await _elicit(
            ctx,
            (
                f"Enter the SQL Server password for {username}.\n"
                "It is cached only in MCP server memory with an idle TTL and is never logged."
            ),
            "password",
        )

    # ==================================================================
    # Session lifecycle
    # ==================================================================

    @mcp.tool()
    async def connect(
        server: Annotated[str, Field(description="SQL Server host or IP, e.g. 'db.example.com'")],
        database: Annotated[str, Field(description="Database/catalog name to connect to, e.g. 'mydb'")],
        ctx: Context,
        port: Annotated[int, Field(description="TCP port of the SQL Server instance (default 1433)")] = 1433,
    ) -> dict[str, Any]:
        """Open a session to a SQL Server database and return a session_id; this is the required first step before any other tool. Missing credentials are prompted for once and cached in memory; on a login failure nothing is cached, so just retry connect.
        """
        header_user, header_pw = sm.header_credentials()

        # Both supplied via headers -> authenticate directly; headers are authoritative.
        if header_user and header_pw:
            return await sm.connect_with_credentials(
                header_user, header_pw, server, database, port
            )

        # Fast path: reuse an already-validated cached credential set.
        if sm.has_credentials():
            result = await sm.connect(server, database, port)
            if not _is_auth_failure(result):
                return result
            sm.invalidate()  # stale/rotated — re-prompt for whatever is missing below

        username = header_user
        if not username:
            username = await _elicit_username(ctx)
            if isinstance(username, dict):
                return username  # cancelled or elicitation unavailable

        pw = await _elicit_password(ctx, username)
        if isinstance(pw, dict):
            return pw  # cancelled or elicitation unavailable

        return await sm.connect_with_credentials(username, pw, server, database, port)

    @mcp.tool()
    def disconnect(
        session_id: Annotated[str, Field(description="Session ID returned by connect")],
    ) -> dict[str, Any]:
        """Close an active SQL Server session and release its connection. If you don't have the session_id, call list_sessions first.
        """
        return sm.disconnect(session_id)

    @mcp.tool()
    def list_sessions() -> dict[str, Any]:
        """List active SQL Server sessions with their server, database, and usage details. Use it to find a session_id or check whether you are already connected to a target.
        """
        return sm.list_sessions()

    # ==================================================================
    # Query — access governed by the SQL login's permissions
    # ==================================================================

    @mcp.tool()
    async def execute_sql(
        session_id: Annotated[str, Field(description="Session ID returned by connect")],
        sql: Annotated[str, Field(description="SQL statement to execute, typically a SELECT")],
        format: Annotated[str, Field(description="Output format: 'table' (default), 'json', or 'csv'")] = "table",
    ) -> str:
        """Execute a single SQL statement on the session and return the rows as table, JSON, or CSV. Read vs. write is enforced by the login's own permissions, so prefer efficient read-only queries.
        """
        try:
            columns, rows = await sm.run_query(session_id, sql)
        except KeyError:
            return f"ERROR: Session not found: {session_id}. Call connect first."
        except Exception as e:
            logger.exception("execute_sql failed")
            return f"ERROR: {type(e).__name__}: {str(e)}"

        result = _render(columns, rows, format)
        return f"{result}\n\n[{result_summary(columns, rows)}]"

    @mcp.tool()
    async def explain_query(
        session_id: Annotated[str, Field(description="Session ID returned by connect")],
        sql: Annotated[str, Field(description="SQL statement to get the execution plan for")],
        actual: Annotated[bool, Field(description="False (default) = estimated plan, statement is NOT run; True = actual plan, runs the statement and includes runtime counters")] = False,
    ) -> str:
        """Return the SQL Server execution plan for a statement as Showplan XML (requires SHOWPLAN permission). Default actual=false gives the estimated plan without running the statement; actual=true runs it and adds real runtime row/time counters.
        """
        try:
            plan = await sm.explain(session_id, sql, actual=actual)
        except KeyError:
            return f"ERROR: Session not found: {session_id}. Call connect first."
        except Exception as e:
            logger.exception("explain_query failed")
            return f"ERROR: {type(e).__name__}: {str(e)}"

        if not plan:
            return "(no plan returned)"
        kind = "actual" if actual else "estimated"
        return f"{plan}\n\n[{kind} execution plan, {len(plan):,} chars]"
