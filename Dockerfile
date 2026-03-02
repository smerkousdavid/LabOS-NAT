FROM python:3.11.14-slim-bookworm

WORKDIR /app

RUN apt-get update && apt-get install -y --no-install-recommends \
    gcc g++ curl ca-certificates \
    libgl1 libglib2.0-0 \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY server.py agent.py config.py frame_source.py ws_handler.py ws_protocol.py ./
COPY configs/ ./configs/
COPY context/ ./context/
COPY tools/ ./tools/

EXPOSE 8002

CMD ["uvicorn", "server:app", "--host", "0.0.0.0", "--port", "8002"]
