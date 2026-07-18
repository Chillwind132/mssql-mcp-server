USE [master];
GO

SET XACT_ABORT ON;
GO

BEGIN TRY
    BEGIN TRANSACTION;

    DECLARE @MCP_READ_USER     SYSNAME       = 'mcp_read';
    DECLARE @MCP_READ_PASSWORD NVARCHAR(128) = '<CHANGE_ME>';  -- set a strong password before running
    DECLARE @loginSql          NVARCHAR(MAX);

    -- Validate password is not empty
    IF LTRIM(RTRIM(ISNULL(@MCP_READ_PASSWORD, ''))) = ''
    BEGIN
        RAISERROR('Password cannot be empty', 16, 1);
        RETURN;
    END

    -- Create the login if missing, otherwise reset its password in place.
    -- We deliberately do NOT drop/recreate: DROP LOGIN fails while the login is
    -- connected, and recreating re-mints the SID, orphaning existing DB users.
    DECLARE @escPwd NVARCHAR(258) = REPLACE(@MCP_READ_PASSWORD, '''', '''''');
    IF NOT EXISTS (SELECT 1 FROM sys.server_principals WHERE name = @MCP_READ_USER)
    BEGIN
        PRINT 'Creating login ' + @MCP_READ_USER + '...';
        SET @loginSql =
            N'CREATE LOGIN ' + QUOTENAME(@MCP_READ_USER) +
            N' WITH PASSWORD = N''' + @escPwd + N''',' +
            N' DEFAULT_DATABASE = [master],' +
            N' DEFAULT_LANGUAGE = [us_english],' +
            N' CHECK_EXPIRATION = OFF,' +
            N' CHECK_POLICY     = OFF;';
    END
    ELSE
    BEGIN
        PRINT 'Login ' + @MCP_READ_USER + ' already exists. Resetting password in place...';
        SET @loginSql =
            N'ALTER LOGIN ' + QUOTENAME(@MCP_READ_USER) +
            N' WITH PASSWORD = N''' + @escPwd + N''';';
    END
    EXEC (@loginSql);

    -- Server-scoped READ-ONLY permissions (for agentic troubleshooting / safe queries)
    PRINT 'Granting server-level read-only permissions to ' + @MCP_READ_USER + '...';
    EXEC('GRANT CONNECT SQL        TO [' + @MCP_READ_USER + ']'); -- allow connection
    EXEC('GRANT VIEW SERVER STATE  TO [' + @MCP_READ_USER + ']'); -- DMVs, waits, sessions, sp_WhoIsActive
    EXEC('GRANT VIEW ANY DEFINITION TO [' + @MCP_READ_USER + ']'); -- inspect schema/object definitions
    EXEC('GRANT VIEW ANY DATABASE  TO [' + @MCP_READ_USER + ']'); -- enumerate databases

    -- SQL Server 2022 granular DMV permissions (subsets of VIEW SERVER STATE).
    -- Granted explicitly so database-scoped performance/security DMVs resolve
    -- reliably; VIEW SERVER PERFORMANCE/SECURITY STATE also cover their
    -- VIEW DATABASE * STATE counterparts across databases.
    EXEC('GRANT VIEW SERVER PERFORMANCE STATE TO [' + @MCP_READ_USER + ']'); -- perf DMVs (2022)
    EXEC('GRANT VIEW SERVER SECURITY STATE    TO [' + @MCP_READ_USER + ']'); -- security DMVs (2022)

    -- Auto read access to all current and future user databases (no per-DB
    -- mapping needed for SELECT). Per-DB grants below still cover SHOWPLAN and
    -- the database-scoped state permissions on instances where coverage differs.
    EXEC('GRANT CONNECT ANY DATABASE      TO [' + @MCP_READ_USER + ']'); -- connect to any DB
    EXEC('GRANT SELECT ALL USER SECURABLES TO [' + @MCP_READ_USER + ']'); -- read any user object

    -- Per-database READ-ONLY mapping across all online user databases.
    -- Adds the user, grants SELECT (db_datareader), schema inspection, and
    -- sp_WhoIsActive execute where installed. Idempotent and safe to re-run.
    PRINT 'Mapping ' + @MCP_READ_USER + ' as read-only in each user database...';

    DECLARE @dbName SYSNAME;
    DECLARE @sql    NVARCHAR(MAX);

    DECLARE db_cursor CURSOR LOCAL FAST_FORWARD FOR
        SELECT name
        FROM sys.databases
        WHERE database_id > 4                 -- skip system DBs
          AND state_desc = 'ONLINE'
          AND source_database_id IS NULL       -- skip snapshots
          AND is_read_only = 0;

    OPEN db_cursor;
    FETCH NEXT FROM db_cursor INTO @dbName;

    WHILE @@FETCH_STATUS = 0
    BEGIN
        SET @sql = N'
            USE ' + QUOTENAME(@dbName) + N';
            -- Re-link if the user already exists (fixes orphan after DROP/CREATE LOGIN),
            -- otherwise create it fresh. Either way it ends mapped to the current login.
            IF EXISTS (SELECT 1 FROM sys.database_principals WHERE name = ''mcp_read'')
                ALTER USER [mcp_read] WITH LOGIN = [mcp_read];
            ELSE
                CREATE USER [mcp_read] FOR LOGIN [mcp_read];
            ALTER ROLE [db_datareader] ADD MEMBER [mcp_read];   -- SELECT only
            GRANT VIEW DEFINITION TO [mcp_read];                 -- schema inspection
            GRANT VIEW DATABASE PERFORMANCE STATE TO [mcp_read]; -- db perf DMVs (2022)
            GRANT VIEW DATABASE SECURITY STATE    TO [mcp_read]; -- db security DMVs (2022)
            GRANT SHOWPLAN TO [mcp_read];                        -- estimated/actual execution plans
            IF OBJECT_ID(N''dbo.sp_WhoIsActive'', N''P'') IS NOT NULL
                GRANT EXECUTE ON OBJECT::dbo.sp_WhoIsActive TO [mcp_read];';

        PRINT '  -> ' + @dbName;
        EXEC (@sql);

        FETCH NEXT FROM db_cursor INTO @dbName;
    END

    CLOSE db_cursor;
    DEALLOCATE db_cursor;

    PRINT 'Done.';

    COMMIT TRANSACTION;
END TRY
BEGIN CATCH
    IF @@TRANCOUNT > 0
    BEGIN
        ROLLBACK TRANSACTION;
    END;

    DECLARE @ErrorMessage  NVARCHAR(4000) = ERROR_MESSAGE();
    DECLARE @ErrorSeverity INT           = ERROR_SEVERITY();
    DECLARE @ErrorState    INT           = ERROR_STATE();

    RAISERROR (@ErrorMessage, @ErrorSeverity, @ErrorState);
    RETURN;
END CATCH;

GO
