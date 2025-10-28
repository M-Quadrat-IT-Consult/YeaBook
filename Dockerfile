FROM python:3.11-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    DATA_DIR=/data \
    PHONEBOOK_TITLE="YeaBook Directory" \
    PHONEBOOK_PROMPT="Select a contact" \
    DEFAULT_GROUP_NAME="Contacts" \
    FLASK_APP=app

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

RUN useradd --create-home appuser \
    && mkdir -p "${DATA_DIR}" \
    && chown -R appuser:appuser "${DATA_DIR}"

USER appuser

EXPOSE 8000

CMD ["gunicorn", "--bind", "0.0.0.0:8000", "app:app"]
