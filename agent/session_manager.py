"""SQL Server session manager — thread-safe, per-user connection registry.

The calling user's identity comes from the ``current_user`` context-var (set by
the auth middleware via the X-DB-User header). The database password is
collected through MCP elicitation by the connect tool, cached in memory with an
idle TTL, and never accepted through HTTP headers or written to logs.

Read vs. write access is NOT enforced here — it is delegated entirely to the
SQL Server login's own permissions. The pyodbc layer and the human-readable
audit trail are inlined below to keep the server to app + tools + this module.
"""

import asyncio
import contextvars
import logging
import os
import threading
import time
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Any, List, Optional, Tuple

import pyodbc
from fastmcp.server.dependencies import get_context

logger = logging.getLogger("mssql-mcp.sessions")
audit = logging.getLogger("mssql-mcp.audit")

# ----------------------------------------------------------------------------
# Configuration (env-driven)
# ----------------------------------------------------------------------------
ODBC_DRIVER = os.environ.get("MSSQL_ODBC_DRIVER", "ODBC Driver 17 for SQL Server")
DEFAULT_PORT = int(os.environ.get("MSSQL_DEFAULT_PORT", "1433"))
ENCRYPT = os.environ.get("MSSQL_ENCRYPT", "no")
TRUST_SERVER_CERTIFICATE = os.environ.get(
    "MSSQL_TRUST_SERVER_CERTIFICATE", "true"
).strip().lower() in ("1", "true", "yes")
CONNECTION_TIMEOUT = int(os.environ.get("MSSQL_CONNECTION_TIMEOUT", "30"))
QUERY_TIMEOUT = int(os.environ.get("MSSQL_QUERY_TIMEOUT", "30"))
MAX_ROWS_PER_QUERY = int(os.environ.get("MAX_ROWS_PER_QUERY", "50000"))
SCHEMA_MAX_ROWS = 10000

pyodbc.pooling = True


# ----------------------------------------------------------------------------
# Audit trail — one human-readable block per connect/query/disconnect event
# ----------------------------------------------------------------------------
SEPARATOR = "=" * 80
MAX_SQL_CHARS = 4000


def _ts() -> str:
    return time.strftime("%Y-%m-%d %H:%M:%S")


def _truncate_sql(text: str, limit: int = MAX_SQL_CHARS) -> str:
    if not text:
        return ""
    if len(text) <= limit:
        return text
    return text[:limit] + f"\n--- truncated ({len(text):,} chars total) ---"


def audit_block(header: str, fields: dict, body: str = "") -> None:
    lines = [f"\n{SEPARATOR}", f"{_ts()}  {header}", "-" * 80]
    for k, v in fields.items():
        lines.append(f"  {k:<14}: {v}")
    if body:
        lines.append("")
        lines.append("  SQL> " + _truncate_sql(body))
    lines.append(SEPARATOR)
    audit.info("\n".join(lines))


# ----------------------------------------------------------------------------
# pyodbc layer (formerly db.py)
# ----------------------------------------------------------------------------
class DatabaseError(Exception):
    """Database execution error."""


class QueryTimeoutError(DatabaseError):
    """Query execution exceeded its timeout."""


def build_connection_string(
    server: str, database: str, username: str, password: str, port: int
) -> str:
    trust = "yes" if TRUST_SERVER_CERTIFICATE else "no"
    return (
        f"Driver={{{ODBC_DRIVER}}};"
        f"Server={server},{port};"
        f"Database={database};"
        f"UID={username};"
        f"PWD={password};"
        f"Encrypt={ENCRYPT};"
        f"TrustServerCertificate={trust};"
    )


@contextmanager
def _get_connection(connection_string: str):
    conn = None
    try:
        conn = pyodbc.connect(
            connection_string, autocommit=False, timeout=CONNECTION_TIMEOUT
        )
        conn.setencoding(encoding="utf-8")
        yield conn
    except pyodbc.Error as e:
        logger.exception("Database connection error: %s", e)
        raise DatabaseError(f"Failed to connect to database: {e}") from e
    finally:
        if conn:
            try:
                conn.close()
            except Exception as e:
                logger.warning("Error closing connection: %s", e)


async def execute_query(
    connection_string: str,
    sql: str,
    timeout: Optional[int] = None,
    max_rows: Optional[int] = None,
) -> Tuple[List[str], List[Tuple[Any, ...]]]:
    """Run a query in a worker thread with a hard timeout. Returns (columns, rows)."""
    timeout = timeout if timeout is not None else QUERY_TIMEOUT
    max_rows = max_rows if max_rows is not None else MAX_ROWS_PER_QUERY

    def _sync_execute() -> Tuple[List[str], List[Tuple[Any, ...]]]:
        with _get_connection(connection_string) as conn:
            cursor = conn.cursor()
            try:
                cursor.execute(sql)
                columns = (
                    [desc[0] for desc in cursor.description]
                    if cursor.description
                    else []
                )
                rows: List[Tuple[Any, ...]] = []
                if columns:
                    while True:
                        batch = cursor.fetchmany(1000)
                        if not batch:
                            break
                        rows.extend(batch)
                        if len(rows) >= max_rows:
                            rows = rows[:max_rows]
                            break
                else:
                    conn.commit()
                return columns, rows
            except pyodbc.Error as e:
                logger.exception("Query execution error: %s", e)
                raise DatabaseError(f"Query execution failed: {e}") from e
            finally:
                try:
                    cursor.close()
                except Exception as e:
                    logger.warning("Error closing cursor: %s", e)

    try:
        return await asyncio.wait_for(asyncio.to_thread(_sync_execute), timeout=timeout)
    except asyncio.TimeoutError:
        raise QueryTimeoutError(f"Query execution exceeded {timeout}s timeout") from None


async def execute_schema_query(
    connection_string: str, sql: str, timeout: Optional[int] = None
) -> Tuple[List[str], List[Tuple[Any, ...]]]:
    """Run a metadata query with a relaxed row cap (for schema/table listing)."""
    timeout = timeout if timeout is not None else QUERY_TIMEOUT
    return await execute_query(connection_string, sql, timeout=timeout, max_rows=SCHEMA_MAX_ROWS)


async def execute_explain(
    connection_string: str,
    sql: str,
    actual: bool = False,
    timeout: Optional[int] = None,
) -> str:
    """Return the XML query plan for ``sql`` as a single string.

    ``actual=False`` (default) uses ``SET SHOWPLAN_XML ON`` — the statement is
    compiled but NOT executed (estimated plan). ``actual=True`` uses
    ``SET STATISTICS XML ON`` — the statement runs and the real plan with
    runtime counters is returned. The SET toggle must be its own batch, so the
    whole sequence runs on one dedicated connection."""
    timeout = timeout if timeout is not None else QUERY_TIMEOUT
    setting = "STATISTICS XML" if actual else "SHOWPLAN_XML"

    def _sync_explain() -> str:
        with _get_connection(connection_string) as conn:
            cursor = conn.cursor()
            try:
                cursor.execute(f"SET {setting} ON")
                plans: List[str] = []
                cursor.execute(sql)
                while True:
                    desc = cursor.description
                    is_plan = bool(
                        desc and len(desc) == 1 and "Showplan" in str(desc[0][0])
                    )
                    if desc:
                        try:
                            rows = cursor.fetchall()
                        except pyodbc.Error:
                            rows = []
                        if is_plan:
                            plans.extend(str(r[0]) for r in rows if r and r[0])
                    if not cursor.nextset():
                        break
                return "\n".join(plans)
            except pyodbc.Error as e:
                logger.exception("Explain execution error: %s", e)
                raise DatabaseError(f"Explain failed: {e}") from e
            finally:
                try:
                    cursor.execute(f"SET {setting} OFF")
                except Exception:
                    pass
                try:
                    cursor.close()
                except Exception as e:
                    logger.warning("Error closing cursor: %s", e)

    try:
        return await asyncio.wait_for(
            asyncio.to_thread(_sync_explain), timeout=timeout
        )
    except asyncio.TimeoutError:
        raise QueryTimeoutError(f"Explain exceeded {timeout}s timeout") from None


async def fetch_database_info(connection_string: str) -> dict:
    """Probe the target for database/server identity. Used to validate a connection."""
    try:
        sql = """
        SELECT
            DB_NAME() as database_name,
            CAST(@@VERSION AS NVARCHAR(4000)) as version,
            CAST(SERVERPROPERTY('MachineName') AS NVARCHAR(256)) as machine_name,
            CAST(SERVERPROPERTY('InstanceName') AS NVARCHAR(256)) as instance_name
        """
        cols, rows = await execute_schema_query(connection_string, sql)
        if rows:
            return dict(zip(cols, rows[0]))
        return {}
    except Exception as e:
        logger.exception("Error fetching database info: %s", e)
        return {"error": str(e)}


# ----------------------------------------------------------------------------
# Per-caller identity (set by the auth middleware)
# ----------------------------------------------------------------------------
@dataclass(frozen=True)
class UserIdentity:
    """Caller identity captured by the auth middleware from request headers.

    ``username``/``password`` come from the optional X-DB-User / X-DB-Password
    headers (either may be empty). The streamable-http transport freezes this
    context at the session's initialize request, so the per-request MCP session
    id is read from the live fastmcp Context (see ``SessionRegistry``) rather
    than from a header here.
    """

    username: str = ""
    password: str = ""
    peer_ip: str = ""
    forwarded_for: str = ""


current_user: contextvars.ContextVar[Optional[UserIdentity]] = contextvars.ContextVar(
    "current_user", default=None
)


def current_session_id() -> str:
    """MCP transport session id for the in-flight tool call, or '' if none.

    Stable for the life of a client's MCP session and unique per client, so it
    serves as the credential-cache key when no X-DB-User header is supplied."""
    try:
        return get_context().session_id or ""
    except Exception:
        return ""


def caller_source() -> dict:
    """Identification fields for the current caller, for audit logging."""
    user = current_user.get()
    if not user:
        return {}
    return {
        "client_ip": user.forwarded_for or user.peer_ip or "",
        "peer_ip": user.peer_ip or "",
    }


@dataclass
class _CachedCredentials:
    username: str
    password: str
    last_used: float


class _Session:
    __slots__ = (
        "session_id",
        "server",
        "database",
        "port",
        "connection_string",
        "connected_at",
        "last_used",
        "query_count",
    )

    def __init__(
        self,
        session_id: str,
        server: str,
        database: str,
        port: int,
        connection_string: str,
    ) -> None:
        self.session_id = session_id
        self.server = server
        self.database = database
        self.port = port
        self.connection_string = connection_string
        self.connected_at = time.time()
        self.last_used = self.connected_at
        self.query_count = 0


class SessionManager:
    """Per-user pool of SQL Server sessions.

    Each session is keyed by ``server[:port]/database`` and stores a pyodbc
    connection string (credentials baked in). Queries open pooled connections
    from that string, so there is no long-lived cursor to manage.
    """

    def __init__(self, username: str, password: str) -> None:
        self._username = username
        self._password = password
        self._sessions: dict[str, _Session] = {}
        self._lock = threading.Lock()

    def _make_session_id(self, server: str, database: str, port: int) -> str:
        host = server if port == DEFAULT_PORT else f"{server}:{port}"
        return f"{host}/{database}"

    async def connect(
        self, server: str, database: str, port: Optional[int] = None
    ) -> dict[str, Any]:
        port = port or DEFAULT_PORT
        session_id = self._make_session_id(server, database, port)

        with self._lock:
            if session_id in self._sessions:
                logger.info("Already connected to %s", session_id)
                return {
                    "session_id": session_id,
                    "status": "already_connected",
                    "server": server,
                    "database": database,
                    "port": port,
                }

        connection_string = build_connection_string(
            server=server,
            database=database,
            username=self._username,
            password=self._password,
            port=port,
        )

        try:
            info = await fetch_database_info(connection_string)
            if "error" in info:
                logger.warning("Connect failed for %s: %s", session_id, info["error"])
                audit_block(
                    "CONNECT FAILED",
                    {
                        "user": self._username,
                        **caller_source(),
                        "server": server,
                        "database": database,
                        "port": port,
                        "error": str(info["error"])[:300],
                    },
                )
                return {"error": info["error"], "server": server, "database": database}

            with self._lock:
                self._sessions[session_id] = _Session(
                    session_id, server, database, port, connection_string
                )

            logger.info(
                "Connected %s as %s (%s)",
                session_id,
                self._username,
                info.get("database_name", database),
            )
            audit_block(
                "CONNECT",
                {
                    "user": self._username,
                    **caller_source(),
                    "session": session_id,
                    "server": server,
                    "database": info.get("database_name", database),
                    "port": port,
                    "machine": info.get("machine_name", ""),
                },
            )
            return {
                "session_id": session_id,
                "status": "connected",
                "server": server,
                "database": info.get("database_name", database),
                "port": port,
                "user": self._username,
                "machine_name": info.get("machine_name"),
                "instance_name": info.get("instance_name"),
                "version": (str(info.get("version", "")).splitlines() or [""])[0],
            }
        except Exception as e:
            logger.warning("Connect error for %s: %s", session_id, e)
            audit_block(
                "CONNECT ERROR",
                {
                    "user": self._username,
                    **caller_source(),
                    "server": server,
                    "database": database,
                    "port": port,
                    "error": str(e)[:300],
                },
            )
            return {"error": str(e), "server": server, "database": database}

    def disconnect(self, session_id: str) -> dict[str, Any]:
        with self._lock:
            if session_id in self._sessions:
                s = self._sessions.pop(session_id)
                logger.info(
                    "Disconnected %s (ran %d queries)", session_id, s.query_count
                )
                audit_block(
                    "DISCONNECT",
                    {
                        "user": self._username,
                        **caller_source(),
                        "session": session_id,
                        "queries_run": s.query_count,
                    },
                )
                return {"session_id": session_id, "status": "disconnected"}
        return {"error": f"Session not found: {session_id}"}

    def disconnect_all(self) -> dict[str, Any]:
        with self._lock:
            count = len(self._sessions)
            self._sessions.clear()
        logger.info("Disconnected all (%d sessions)", count)
        return {"status": "disconnected_all", "count": count}

    def list_sessions(self) -> dict[str, Any]:
        with self._lock:
            items = []
            for sid, s in self._sessions.items():
                items.append(
                    {
                        "session_id": sid,
                        "server": s.server,
                        "database": s.database,
                        "port": s.port,
                        "connected_at": time.strftime(
                            "%Y-%m-%d %H:%M:%S", time.localtime(s.connected_at)
                        ),
                        "last_used": time.strftime(
                            "%Y-%m-%d %H:%M:%S", time.localtime(s.last_used)
                        ),
                        "query_count": s.query_count,
                    }
                )
            return {"sessions": items, "count": len(items)}

    def _get_connection_string(self, session_id: str) -> str:
        with self._lock:
            s = self._sessions.get(session_id)
            if not s:
                raise KeyError(session_id)
            s.last_used = time.time()
            s.query_count += 1
            return s.connection_string

    async def _run_and_audit(
        self,
        session_id: str,
        sql: str,
        tool_name: str,
        timeout: Optional[int],
        max_rows: Optional[int],
    ) -> Tuple[List[str], List[Tuple[Any, ...]]]:
        conn_str = self._get_connection_string(session_id)
        start = time.perf_counter()
        try:
            cols, rows = await execute_query(
                conn_str, sql, timeout=timeout, max_rows=max_rows
            )
            elapsed_ms = int((time.perf_counter() - start) * 1000)
            audit_block(
                f"QUERY [{tool_name}]",
                {
                    "user": self._username,
                    **caller_source(),
                    "session": session_id,
                    "status": "ok",
                    "rows": len(rows),
                    "elapsed_ms": elapsed_ms,
                },
                body=sql,
            )
            return cols, rows
        except Exception as e:
            elapsed_ms = int((time.perf_counter() - start) * 1000)
            audit_block(
                f"QUERY ERROR [{tool_name}]",
                {
                    "user": self._username,
                    **caller_source(),
                    "session": session_id,
                    "status": "error",
                    "elapsed_ms": elapsed_ms,
                    "error": str(e)[:300],
                },
                body=sql,
            )
            raise

    async def run_query(
        self,
        session_id: str,
        sql: str,
        timeout: Optional[int] = None,
        max_rows: Optional[int] = None,
        tool_name: str = "execute_sql",
    ) -> Tuple[List[str], List[Tuple[Any, ...]]]:
        return await self._run_and_audit(
            session_id, sql, tool_name, timeout, max_rows
        )

    async def explain(
        self,
        session_id: str,
        sql: str,
        actual: bool = False,
        timeout: Optional[int] = None,
        tool_name: str = "explain_query",
    ) -> str:
        conn_str = self._get_connection_string(session_id)
        start = time.perf_counter()
        try:
            plan = await execute_explain(conn_str, sql, actual=actual, timeout=timeout)
            elapsed_ms = int((time.perf_counter() - start) * 1000)
            audit_block(
                f"EXPLAIN [{tool_name}]",
                {
                    "user": self._username,
                    **caller_source(),
                    "session": session_id,
                    "status": "ok",
                    "actual": actual,
                    "elapsed_ms": elapsed_ms,
                },
                body=sql,
            )
            return plan
        except Exception as e:
            elapsed_ms = int((time.perf_counter() - start) * 1000)
            audit_block(
                f"EXPLAIN ERROR [{tool_name}]",
                {
                    "user": self._username,
                    **caller_source(),
                    "session": session_id,
                    "status": "error",
                    "actual": actual,
                    "elapsed_ms": elapsed_ms,
                    "error": str(e)[:300],
                },
                body=sql,
            )
            raise


class SessionRegistry:
    """Per-caller SessionManager pool.

    Credentials are resolved flexibly from the request context:
      * X-DB-User + X-DB-Password headers   -> used as-is (no elicitation)
      * X-DB-User only                      -> password elicited once, cached
      * neither                             -> username + password elicited

    Cached credentials are keyed by the caller's ``principal_key`` (the username
    header when present, otherwise the MCP session id) so they survive across
    the connect/query/disconnect calls and stay isolated between concurrent
    clients. They are held in memory with an idle TTL and never logged.
    """

    def __init__(self, password_idle_ttl_seconds: int = 3600) -> None:
        self._managers: dict[str, SessionManager] = {}
        self._creds: dict[str, _CachedCredentials] = {}
        self._lock = threading.Lock()
        self._password_idle_ttl_seconds = password_idle_ttl_seconds

    def _identity(self) -> UserIdentity:
        ident = current_user.get()
        if ident is None:
            raise RuntimeError("No caller identity in request context")
        return ident

    def _principal(self) -> Optional[str]:
        """Stable cache key for this caller: the username header when present,
        otherwise the MCP session id. None if neither is available."""
        ident = self._identity()
        if ident.username:
            return f"u:{ident.username}"
        sid = current_session_id()
        if sid:
            return f"s:{sid}"
        return None

    def _require_principal(self) -> str:
        key = self._principal()
        if key is None:
            raise RuntimeError(
                "Cannot identify caller: provide an X-DB-User header or use an "
                "MCP session so credentials can be cached."
            )
        return key

    def header_credentials(self) -> Tuple[str, str]:
        """The (username, password) supplied via headers; either may be empty."""
        ident = self._identity()
        return ident.username, ident.password

    def _is_expired(self, cached: _CachedCredentials, now: float) -> bool:
        ttl = self._password_idle_ttl_seconds
        return ttl > 0 and now - cached.last_used > ttl

    def has_credentials(self) -> bool:
        """True if valid credentials are already available for this caller.

        Header-supplied user+password always count; otherwise a non-expired
        cached entry counts. Expired entries are evicted as a side effect.
        """
        ident = self._identity()
        if ident.username and ident.password:
            return True

        key = self._principal()
        if key is None:
            return False

        expired_mgr: Optional[SessionManager] = None
        now = time.monotonic()
        with self._lock:
            cached = self._creds.get(key)
            if cached is None:
                return False
            if self._is_expired(cached, now):
                self._creds.pop(key, None)
                expired_mgr = self._managers.pop(key, None)
                cached = None
            else:
                cached.last_used = now

        if expired_mgr is not None:
            expired_mgr.disconnect_all()

        return cached is not None

    def invalidate(self) -> None:
        """Drop cached credentials (and sessions) for the current caller.

        Called after an authentication failure so the next connect re-prompts.
        """
        key = self._principal()
        if key is None:
            return
        with self._lock:
            self._creds.pop(key, None)
            mgr = self._managers.pop(key, None)
        if mgr is not None:
            mgr.disconnect_all()

    async def connect_with_credentials(
        self,
        username: str,
        password: str,
        server: str,
        database: str,
        port: Optional[int] = None,
    ) -> dict[str, Any]:
        """Validate credentials by connecting, caching them only on success.

        Reuses the caller's existing manager when the credentials are unchanged
        so sessions already open against other servers survive a new connect.
        Credentials are never cached if the connection fails; only a credential
        change (rotation) replaces the manager and drops its stale sessions."""
        key = self._require_principal()

        with self._lock:
            existing = self._managers.get(key)
            cached = self._creds.get(key)
            reuse = (
                existing is not None
                and cached is not None
                and cached.username == username
                and cached.password == password
            )
        mgr = existing if reuse else SessionManager(username=username, password=password)

        result = await mgr.connect(server, database, port)

        if isinstance(result, dict) and "error" not in result:
            now = time.monotonic()
            old_mgr: Optional[SessionManager] = None
            with self._lock:
                cached = self._creds.get(key)
                current = self._managers.get(key)
                creds_changed = cached is not None and (
                    cached.username != username or cached.password != password
                )
                if current is not None and current is not mgr and creds_changed:
                    old_mgr = current
                self._creds[key] = _CachedCredentials(username, password, now)
                self._managers[key] = mgr
            if old_mgr is not None:
                old_mgr.disconnect_all()
        return result

    def _get(self) -> SessionManager:
        ident = self._identity()
        key = self._require_principal()

        now = time.monotonic()
        expired_mgr: Optional[SessionManager] = None
        mgr: Optional[SessionManager] = None

        with self._lock:
            cached = self._creds.get(key)
            if cached is not None and self._is_expired(cached, now):
                self._creds.pop(key, None)
                expired_mgr = self._managers.pop(key, None)
                cached = None

            # Self-heal from header-supplied credentials (no elicitation needed).
            if cached is None and ident.username and ident.password:
                cached = _CachedCredentials(ident.username, ident.password, now)
                self._creds[key] = cached

            if cached is not None:
                cached.last_used = now
                mgr = self._managers.get(key)
                if mgr is None:
                    mgr = SessionManager(
                        username=cached.username, password=cached.password
                    )
                    self._managers[key] = mgr

        if expired_mgr is not None:
            expired_mgr.disconnect_all()

        if mgr is None:
            raise RuntimeError(
                "Not authenticated or session expired. Call connect to enter "
                "your DB credentials again."
            )

        return mgr

    # ---- delegate every public method ----

    async def connect(
        self, server: str, database: str, port: Optional[int] = None
    ) -> dict[str, Any]:
        return await self._get().connect(server, database, port)

    def disconnect(self, session_id: str) -> dict[str, Any]:
        return self._get().disconnect(session_id)

    def disconnect_all(self) -> dict[str, Any]:
        return self._get().disconnect_all()

    def list_sessions(self) -> dict[str, Any]:
        return self._get().list_sessions()

    async def run_query(
        self,
        session_id: str,
        sql: str,
        timeout: Optional[int] = None,
        max_rows: Optional[int] = None,
        tool_name: str = "execute_sql",
    ) -> Tuple[List[str], List[Tuple[Any, ...]]]:
        return await self._get().run_query(session_id, sql, timeout, max_rows, tool_name)

    async def explain(
        self,
        session_id: str,
        sql: str,
        actual: bool = False,
        timeout: Optional[int] = None,
        tool_name: str = "explain_query",
    ) -> str:
        return await self._get().explain(session_id, sql, actual, timeout, tool_name)
