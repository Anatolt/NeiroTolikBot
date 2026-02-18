FROM python:3.11-slim

WORKDIR /app
ENV PYTHONUNBUFFERED=1

RUN apt-get update \
    && apt-get install -y --no-install-recommends ffmpeg \
    && rm -rf /var/lib/apt/lists/*

COPY tools/voice_recv_worker/requirements.txt /tmp/voice_recv_requirements.txt
RUN pip install --no-cache-dir -r /tmp/voice_recv_requirements.txt

COPY . /app

CMD ["python", "/app/tools/voice_recv_worker/worker.py"]
