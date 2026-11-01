# Build context: repository root (needs backend/ and database/)
FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PIP_NO_CACHE_DIR=1
WORKDIR /srv/backend

COPY backend/requirements.txt ./
RUN pip install -r requirements.txt

COPY database /srv/database
COPY backend /srv/backend

RUN useradd --create-home --uid 1000 app && chown -R app /srv
USER app

EXPOSE 8000
CMD ["gunicorn", "app.main:app", "-k", "uvicorn.workers.UvicornWorker", "-w", "4", "-b", "0.0.0.0:8000"]
