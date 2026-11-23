FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

# Built from the repo root (context: .) so the package import path gcal_mcp.* works
COPY gcal_mcp/requirements.txt .
RUN pip install -r requirements.txt

COPY gcal_mcp /app/gcal_mcp

RUN useradd --create-home --uid 10002 gcal
USER gcal

EXPOSE 9100

CMD ["python", "-m", "gcal_mcp.server"]
