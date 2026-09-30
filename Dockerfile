# Thimble as a website (Google Cloud Run behind Firebase Hosting).
# THIMBLE_HOSTED=1: visitors' API keys and projects stay in their own browsers.
FROM python:3.13-slim

ENV PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    THIMBLE_HOSTED=1 \
    THIMBLE_NO_BROWSER=1

# libglib/libgomp: runtime libraries OpenCV and scikit-image need on a slim image
RUN apt-get update \
 && apt-get install -y --no-install-recommends libglib2.0-0 libgomp1 \
 && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY requirements.txt .
RUN pip install -r requirements.txt gunicorn==23.0.0

COPY . .

# one process (progress and uploads live in memory), several threads for concurrent visitors
CMD exec gunicorn --bind :$PORT --workers 1 --threads 8 --timeout 600 app:app
