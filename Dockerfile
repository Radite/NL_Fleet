# Production image for Azure App Service (Web App for Containers).
# Includes msodbcsql18 so pyodbc can talk to SQL Server — this is why prod
# uses a container instead of App Service's plain "Python 3.12" runtime,
# which doesn't have the ODBC driver preinstalled.

FROM python:3.12-slim

# --- Microsoft ODBC Driver 18 for SQL Server --------------------------------
RUN apt-get update && apt-get install -y --no-install-recommends \
        curl gnupg2 apt-transport-https ca-certificates \
    && curl https://packages.microsoft.com/keys/microsoft.asc | tee /etc/apt/trusted.gpg.d/microsoft.asc \
    && curl https://packages.microsoft.com/config/debian/12/prod.list > /etc/apt/sources.list.d/mssql-release.list \
    && apt-get update \
    && ACCEPT_EULA=Y apt-get install -y --no-install-recommends msodbcsql18 unixodbc-dev \
    && apt-get purge -y curl gnupg2 \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt requirements-prod.txt ./
RUN pip install --no-cache-dir -r requirements.txt -r requirements-prod.txt

COPY app/ ./app/
COPY static/ ./static/

# Note: data/ and seed_db.py are deliberately NOT copied into the prod
# image — production reads real data via DB_BACKEND=sqlserver, and
# shipping the demo seed script/db alongside a real deployment invites
# someone to accidentally point it at the wrong thing.

ENV DB_BACKEND=sqlserver
EXPOSE 8000

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000", "--workers", "1"]
