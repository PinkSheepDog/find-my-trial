# syntax=docker/dockerfile:1
# ============================================================================
#  Find My Trial — single-image deploy (API + built frontend on one origin).
#  Stage 1 builds the React SPA; stage 2 runs FastAPI and serves that build so
#  the SameSite=Strict session cookie works without any cross-site relaxation.
# ============================================================================

# ---- Stage 1: build the React frontend ----
FROM node:20-slim AS frontend
WORKDIR /build
COPY frontend/package.json frontend/package-lock.json ./
RUN npm ci
COPY frontend/ ./
RUN npm run build          # -> /build/dist

# ---- Stage 2: Python runtime ----
FROM python:3.11-slim AS runtime
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1
WORKDIR /app

# Backend deps first (better layer caching).
COPY backend/requirements.txt ./backend/requirements.txt
RUN pip install --no-cache-dir -r backend/requirements.txt

# Backend source, then the built frontend at the path FastAPI expects
# (<repo>/frontend/dist relative to backend/app/main.py -> parents[2]/frontend/dist).
COPY backend/ ./backend/
COPY --from=frontend /build/dist ./frontend/dist

WORKDIR /app/backend
EXPOSE 8000
# Render (and most PaaS) inject $PORT; default to 8000 for local `docker run`.
CMD ["sh", "-c", "uvicorn app.main:app --host 0.0.0.0 --port ${PORT:-8000}"]
