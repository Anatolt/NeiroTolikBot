FROM python:3.11-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

RUN apt-get update && apt-get install -y --no-install-recommends \
    ffmpeg \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
RUN pip install --no-cache-dir py-cord==2.6.1 PyNaCl>=1.5.0

COPY tools/voice_test_sender/send_voice.py /app/send_voice.py

ENTRYPOINT ["python", "/app/send_voice.py"]
