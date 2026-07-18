# Dockerfile
FROM python:3.11-slim

# Install the Microsoft ODBC driver 17 for SQL Server (needed by pyodbc)
RUN apt-get update && apt-get install -y unixodbc curl gnupg && \
    curl -sSL https://packages.microsoft.com/keys/microsoft.asc | gpg --dearmor -o /usr/share/keyrings/microsoft-prod.gpg && \
    echo "deb [signed-by=/usr/share/keyrings/microsoft-prod.gpg] https://packages.microsoft.com/debian/11/prod bullseye main" > /etc/apt/sources.list.d/mssql-release.list && \
    apt-get update && \
    ACCEPT_EULA=Y apt-get install -y msodbcsql17 && \
    apt-get clean && rm -rf /var/lib/apt/lists/*

RUN pip install --no-cache-dir \
    "fastmcp==3.2.3" \
    "pyodbc==5.1.0"

COPY app.py /app/app.py
COPY agent  /app/agent

COPY entrypoint.sh /app/entrypoint.sh
RUN chmod +x /app/entrypoint.sh

RUN mkdir -p /app/logs

WORKDIR /app
ENTRYPOINT ["/app/entrypoint.sh"]
