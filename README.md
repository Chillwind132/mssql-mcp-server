# mssql-mcp-server

[![License: MIT](https://img.shields.io/github/license/Chillwind132/mssql-mcp-server)](LICENSE)
[![Python 3.11](https://img.shields.io/badge/python-3.11-blue?logo=python&logoColor=white)](https://www.python.org/)
[![Docker](https://img.shields.io/badge/docker-ready-2496ED?logo=docker&logoColor=white)](Dockerfile)

**A Microsoft SQL Server MCP server.** Query, inspect, and troubleshoot any SQL Server instance from Cursor, Claude Code, Codex, or any MCP (Model Context Protocol) client. Connects via ODBC using per-caller credentials elicited at runtime. Passwords live only in server memory with an idle TTL and are never logged.

- **Query tools**: `connect` / `disconnect` / `list_sessions` session lifecycle, `execute_sql` with table/JSON/CSV output, and `explain_query` for estimated or actual Showplan XML execution plans
- **Per-caller identity**: the `X-DB-User` and `X-DB-Password` headers are both optional; anything missing is elicited once and cached in-memory
- **Permission-scoped access**: read vs. write is governed by the SQL login's own permissions; `provision_mcp_read.sql` provisions a ready-made read-only `mcp_read` login across all user databases
- **Zero secrets on disk**: no credentials in config files, env vars, or logs

## Example prompts

- "Show the execution plan for this query and suggest a better index."
- "Who is blocking whom on `db1` right now?"
- "List the 10 largest tables in the `sales` database."
- "Find all stored procedures that reference the `Orders` table."
- "Export the results of this query as CSV."

## Tools

| Tool | Description |
|------|-------------|
| `connect` | Open a session to a SQL Server database and return a `session_id` |
| `disconnect` | Close an active SQL Server session |
| `list_sessions` | List active sessions with server, database, and usage details |
| `execute_sql` | Run a SQL statement and return rows as table, JSON, or CSV |
| `explain_query` | Estimated or actual execution plan as Showplan XML |

## Quick Start

```bash
docker compose -f docker-compose.yml -p mssql-mcp up -d --build --force-recreate
```

## Client setup

Both `X-DB-User` and `X-DB-Password` headers are optional: anything missing is prompted for once via MCP elicitation and cached in memory.

### Cursor (`mcp.json`)

```json
{
  "mcpServers": {
    "mssql-mcp": {
      "type": "http",
      "url": "http://localhost:8007/mcp",
      "headers": {
        "X-DB-User": "<your-db-username>",
        "X-DB-Password": "<your-db-password>"
      }
    }
  }
}
```

### Claude Code

```bash
claude mcp add --transport http mssql-mcp http://localhost:8007/mcp \
  --header "X-DB-User: <your-db-username>" \
  --header "X-DB-Password: <your-db-password>"
```

### Codex (`~/.codex/config.toml`)

```toml
[mcp_servers.mssql-mcp]
url = "http://localhost:8007/mcp"
http_headers = { "X-DB-User" = "<your-db-username>", "X-DB-Password" = "<your-db-password>" }
```

Any other MCP client works the same way: point it at the streamable HTTP endpoint and pass the headers.
