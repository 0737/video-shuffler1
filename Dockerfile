FROM python:3.11-slim

# Install ffmpeg
RUN apt-get update && \
    apt-get install -y --no-install-recommends ffmpeg && \
    rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

ENV PORT=8080
EXPOSE 8080

CMD gunicorn app:app \
    --bind 0.0.0.0:$PORT \
    --workers 2 \
    --timeout 3600 \
    --keep-alive 5
