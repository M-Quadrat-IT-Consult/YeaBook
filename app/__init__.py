import os
from pathlib import Path

from flask import Flask

from .routes import bp
from .version import (
    DEFAULT_APP_VERSION,
    DEFAULT_DOCKER_IMAGE,
    DEFAULT_GITHUB_REPO,
)


def create_app() -> Flask:
    app = Flask(__name__)

    data_dir = Path(os.environ.get("DATA_DIR", "data"))
    data_dir.mkdir(parents=True, exist_ok=True)
    app.config.update(
        SECRET_KEY=os.environ.get("SECRET_KEY", "change-me"),
        DATABASE=str(data_dir / "contacts.db"),
        XML_FILE=str(data_dir / "phonebook.xml"),
        PHONEBOOK_TITLE=os.environ.get("PHONEBOOK_TITLE", "YeaBook Directory"),
        PHONEBOOK_PROMPT=os.environ.get("PHONEBOOK_PROMPT", "Select a contact"),
        DEFAULT_GROUP_NAME=os.environ.get("DEFAULT_GROUP_NAME", "Contacts"),
        APP_VERSION=os.environ.get("APP_VERSION", DEFAULT_APP_VERSION),
        GITHUB_REPO=os.environ.get("GITHUB_REPO", DEFAULT_GITHUB_REPO),
        DOCKER_IMAGE=os.environ.get("DOCKER_IMAGE", DEFAULT_DOCKER_IMAGE),
        STATUS_CACHE_TTL=float(os.environ.get("STATUS_CACHE_TTL", "300")),
    )

    app.register_blueprint(bp)

    return app


app = create_app()
