FROM python:3.11-slim

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY . .

# Persistent state lives on a mounted volume at /data (set up the volume on your host).
# OMNIOS_CLINIC_DB = the clinic system-of-record; RANTEVU_* = brain/audit chain.
ENV OMNIOS_CLINIC_DB=/data/clinic.db \
    RANTEVU_DB=/data/brain.db \
    RANTEVU_SAGA_DB=/data/saga.db \
    RANTEVU_TICK_SECONDS=30
EXPOSE 8000

# Ships the OmniOS API (api:app), NOT the legacy main:app. uvicorn honours $PORT if set.
CMD ["sh", "-c", "uvicorn api:app --host 0.0.0.0 --port ${PORT:-8000}"]
