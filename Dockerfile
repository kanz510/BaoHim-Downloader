FROM python:3.12-slim
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
RUN apt-get update && apt-get install -y --no-install-recommends ffmpeg ca-certificates \
    && rm -rf /var/lib/apt/lists/* \
    && pip install --no-cache-dir 'yt-dlp>=2026.10.26'
WORKDIR /app
COPY server.py .
COPY public ./public
RUN useradd -r -u 10001 -m himtik
USER himtik
EXPOSE 8000
CMD ["python", "server.py"]
