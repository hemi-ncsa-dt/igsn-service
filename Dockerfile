FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

WORKDIR /app

COPY pyproject.toml README.md ./
COPY app ./app

RUN pip install --no-cache-dir '.[backfill]'

EXPOSE 8000

# Single worker by default. The publisher loop claims work with an atomic
# find_one_and_update, so extra replicas are safe -- but each one runs its own
# loop, so scale deliberately rather than by accident.
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
