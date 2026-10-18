# ListHoard — the list browser and arr importer. Built locally (like wrestlingsync);
# no registry image exists to pin, so every other layer is pinned by explicit tag.
FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

RUN addgroup --system --gid 1000 listhoard \
    && adduser --system --uid 1000 --gid 1000 --home /app listhoard

COPY requirements.txt /app/requirements.txt
RUN pip install --no-cache-dir -r /app/requirements.txt

COPY app/ /app/app/
COPY templates/ /app/templates/
COPY static/ /app/static/

WORKDIR /app
EXPOSE 8675
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8675"]
