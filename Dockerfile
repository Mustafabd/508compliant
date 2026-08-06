FROM python:3.11-slim

WORKDIR /app

COPY backend/requirements.txt backend/requirements.txt
RUN pip install --no-cache-dir -r backend/requirements.txt

COPY backend backend
COPY frontend frontend

WORKDIR /app/backend
ENV PYTHONUNBUFFERED=1
EXPOSE 8000

# Render/Railway inject $PORT; fall back to 8000 for plain `docker run`.
CMD uvicorn app.main:app --host 0.0.0.0 --port ${PORT:-8000}
