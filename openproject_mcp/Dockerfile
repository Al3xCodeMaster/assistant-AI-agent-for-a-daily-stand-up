FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

# Built from the repo root (context: .) so the package import path openproject_mcp.* works
COPY openproject_mcp/requirements.txt .
RUN pip install -r requirements.txt

COPY openproject_mcp /app/openproject_mcp

RUN useradd --create-home --uid 10003 openproject
USER openproject

EXPOSE 9200

CMD ["python", "-m", "openproject_mcp.server"]
