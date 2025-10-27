FROM python:3.11-slim

# Create unprivileged user early (pick stable IDs to match host if you bind-mount)
RUN groupadd -g 1000 appuser && useradd -m -u 1000 -g 1000 appuser

# System deps
RUN apt-get update && apt-get install -y --no-install-recommends p7zip-full \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Install deps as root
COPY --chown=appuser:appuser requirements.txt /app/requirements.txt
RUN pip install --no-cache-dir -r requirements.txt

# Copy app code with correct ownership and readable perms
COPY --chown=appuser:appuser app.py /app/app.py
# (Optional hardening: ensure read/execute on folders; read on files)
RUN chmod -R a+rX /app

# Runtime dirs (ensure they exist and are writable by appuser)
ENV BASE_DIR=/data \
    OUT_DIR=/output \
    API_TOKEN=changeme

RUN mkdir -p "$BASE_DIR" "$OUT_DIR" && chown -R appuser:appuser "$BASE_DIR" "$OUT_DIR"

USER appuser

EXPOSE 3256
CMD ["uvicorn", "app:app", "--host", "0.0.0.0", "--port", "3256"]
