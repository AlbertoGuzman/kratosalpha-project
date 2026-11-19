# Imagen para Cloud Run Jobs (proceso batch ConnorsRSI).
FROM python:3.11-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    TZ=America/New_York

# git (publicación del track record), curl/gnupg (repo apt de Google),
# tzdata (TZ) y google-cloud-cli (gcloud storage).
RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        git curl ca-certificates gnupg tzdata \
    && curl -fsSL https://packages.cloud.google.com/apt/doc/apt-key.gpg \
        | gpg --dearmor -o /usr/share/keyrings/cloud.google.gpg \
    && echo "deb [signed-by=/usr/share/keyrings/cloud.google.gpg] https://packages.cloud.google.com/apt cloud-sdk main" \
        > /etc/apt/sources.list.d/google-cloud-sdk.list \
    && apt-get update \
    && apt-get install -y --no-install-recommends google-cloud-cli \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY trading_system/requirements.txt trading_system/requirements.txt
RUN pip install -r trading_system/requirements.txt

COPY . .
RUN sed -i 's/\r$//' entrypoint.sh && chmod +x entrypoint.sh

ENTRYPOINT ["/app/entrypoint.sh"]
