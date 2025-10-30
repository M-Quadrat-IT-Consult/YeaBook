FROM python:3.11-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    DATA_DIR=/data \
    PHONEBOOK_TITLE="YeaBook Directory" \
    PHONEBOOK_PROMPT="Select a contact" \
    DEFAULT_GROUP_NAME="Contacts" \
    FLASK_APP=app \
    APP_USER=appuser \
    APP_UID=1000 \
    APP_GID=1000 \
    PUID=1000 \
    PGID=1000 \
    APP_WORKDIR=/app

RUN apt-get update \
    && apt-get install -y --no-install-recommends gosu \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

COPY docker-entrypoint.sh /entrypoint.sh
RUN chmod +x /entrypoint.sh

ENTRYPOINT ["/entrypoint.sh"]

EXPOSE 8000

CMD ["gunicorn", "--bind", "0.0.0.0:8000", "app:app"]
