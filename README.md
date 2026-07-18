# mssql-mcp-server

Remote Microsoft SQL Server operations via ODBC, exposed as an MCP (Model Context Protocol) server. Connects to any SQL Server instance using per-caller credentials elicited at runtime — passwords live only in server memory with an idle TTL and are never logged.

- **Query tools** — `connect` / `disconnect` / `list_sessions` session lifecycle, `execute_sql` with table/JSON/CSV output, and `explain_query` for estimated or actual Showplan XML execution plans
- **Per-caller identity** — the `X-DB-User` and `X-DB-Password` headers are both optional; anything missing is elicited once and cached in-memory
- **Permission-scoped access** — read vs. write is governed by the SQL login's own permissions; `provision_mcp_read.sql` provisions a ready-made read-only `mcp_read` login across all user databases
- **Zero secrets on disk** — no credentials in config files, env vars, or logs

## Quick Start

```bash
docker compose -f docker-compose.yml -p mssql-mcp up -d --build --force-recreate
```

## Cursor `mcp.json`

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
