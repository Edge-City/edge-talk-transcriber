FROM python:3.12-slim

# ffmpeg for audio extraction. Clean apt cache to keep the image lean.
RUN apt-get update \
    && apt-get install -y --no-install-recommends ffmpeg \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Install Python deps first so this layer is cached across code changes.
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Pre-download the whisper model into the image so cold starts don't pull it
# from HuggingFace at runtime (faster, no runtime network dependency).
# Override WHISPER_MODEL at build time to bake a different default.
ARG WHISPER_MODEL=large-v3-turbo
RUN python -c "from faster_whisper import WhisperModel; WhisperModel('${WHISPER_MODEL}', device='cpu', compute_type='int8')"

# Copy only source (NEVER copy the whole context — keeps any local .env / keys
# out of the image; .dockerignore is the backstop). Trailing slash required
# because the glob matches multiple files.
COPY *.py ./

ENV PYTHONUNBUFFERED=1

ENTRYPOINT ["python", "main.py"]
