FROM python:3.11-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    DATA_DIR=/data \
    PHONEBOOK_TITLE="YeaBook Directory" \
    PHONEBOOK_PROMPT="Select a contact" \
    DEFAULT_GROUP_NAME="Contacts" \
    FLASK_APP=app

RUN apt-get update \
    && apt-get install -y --no-install-recommends gosu \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY LICENSE .
COPY app ./app

COPY docker-entrypoint.sh /entrypoint.sh
RUN chmod +x /entrypoint.sh

ARG APP_VERSION=0.1.0
ENV APP_VERSION=${APP_VERSION}

ENTRYPOINT ["/entrypoint.sh"]

EXPOSE 8000

VOLUME ["/data"]

CMD ["gunicorn", "--bind", "0.0.0.0:8000", "app:app"]
