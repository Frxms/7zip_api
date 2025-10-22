FROM python:3.11-slim

# Install 7-Zip CLI
RUN apt-get update && apt-get install -y --no-install-recommends p7zip-full \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY requirements.txt /app/
RUN pip install --no-cache-dir -r requirements.txt
COPY app.py /app/

# Unprivileged user
RUN useradd -m appuser
USER appuser

ENV API_TOKEN=changeme \
    BASE_DIR=/data \
    OUT_DIR=/output

EXPOSE 3256
CMD ["uvicorn", "app:app", "--host", "0.0.0.0", "--port", "3256"]

