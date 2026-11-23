FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

COPY requirements.txt .
RUN pip install -r requirements.txt

COPY . .

RUN useradd --create-home --uid 10001 stub \
    && mkdir -p /data \
    && chown stub:stub /data
USER stub

ENV CALENDAR_STUB_LOG=/data/calls.jsonl
EXPOSE 9000

CMD ["python", "calendar_mcp.py"]
