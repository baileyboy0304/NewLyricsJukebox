# NewLyricsJukebox - Home Assistant add-on
# Configuration is read from /data/options.json by config.py via run.sh.

FROM python:3.12-slim-bookworm AS builder

RUN apt-get update && apt-get install -y --no-install-recommends \
    gcc g++ libc-dev libffi-dev pkg-config \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir --upgrade pip \
    && pip install --no-cache-dir -r requirements.txt

FROM python:3.12-slim-bookworm

RUN apt-get update && apt-get install -y --no-install-recommends \
    libffi8 ffmpeg \
    && rm -rf /var/lib/apt/lists/* && apt-get clean

WORKDIR /app
COPY --from=builder /usr/local/lib/python3.12/site-packages /usr/local/lib/python3.12/site-packages
COPY --from=builder /usr/local/bin /usr/local/bin
COPY . .

COPY run.sh /run.sh
RUN chmod +x /run.sh && mkdir -p /data /config

CMD ["/run.sh"]
