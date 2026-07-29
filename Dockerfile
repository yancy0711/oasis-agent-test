# Bookworm provides GDAL and CBC packages for both amd64 and Apple Silicon arm64.
FROM python:3.11-slim-bookworm
ARG DEBIAN_FRONTEND=noninteractive
RUN apt-get -o Acquire::Retries=4 -o Acquire::http::Timeout=45 -o Acquire::https::Timeout=45 update \
    && apt-get install -y --no-install-recommends gdal-bin libgdal-dev build-essential coinor-cbc \
    && rm -rf /var/lib/apt/lists/*
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir --retries 4 --timeout 60 -r requirements.txt
COPY . .
RUN mkdir -p data/cache outputs_run tmp/uploads
EXPOSE 8000 8501
CMD ["uvicorn", "api.main:app", "--host", "0.0.0.0", "--port", "8000", "--workers", "1"]
