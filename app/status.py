from __future__ import annotations

import json
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from typing import Dict, Optional

from flask import current_app

StatusPayload = Dict[str, Dict[str, Optional[str]]]

_CACHE: Dict[str, object] = {"timestamp": 0.0, "data": None}


@dataclass
class SourceStatus:
    status: str
    version: Optional[str]


def _http_get(url: str, timeout: float = 5.0) -> str:
    request = urllib.request.Request(
        url,
        headers={
            "Accept": "application/json",
            "User-Agent": "YeaBook/1.0",
        },
    )
    with urllib.request.urlopen(request, timeout=timeout) as response:  # type: ignore[call-arg]
        data = response.read().decode("utf-8")
    return data


def _fetch_github_latest(repo: str) -> SourceStatus:
    api_url = f"https://api.github.com/repos/{repo}/releases/latest"
    try:
        body = _http_get(api_url)
        payload = json.loads(body)
        tag_name = payload.get("tag_name")
        if isinstance(tag_name, str) and tag_name.strip():
            return SourceStatus(status="up_to_date", version=tag_name.strip())
    except urllib.error.URLError:
        return SourceStatus(status="unreachable", version=None)
    except json.JSONDecodeError:
        return SourceStatus(status="unknown", version=None)
    return SourceStatus(status="unknown", version=None)


def _fetch_docker_latest(image: str) -> SourceStatus:
    namespace, _, name = image.partition("/")
    if not name:
        # If no namespace was provided assume 'library'
        namespace, name = "library", namespace
    api_url = f"https://hub.docker.com/v2/repositories/{namespace}/{name}/tags?page_size=1&page=1"
    try:
        body = _http_get(api_url)
        payload = json.loads(body)
        results = payload.get("results") or []
        if results:
            latest = results[0]
            version = latest.get("name")
            if isinstance(version, str):
                return SourceStatus(status="up_to_date", version=version.strip())
    except urllib.error.URLError:
        return SourceStatus(status="unreachable", version=None)
    except json.JSONDecodeError:
        return SourceStatus(status="unknown", version=None)
    return SourceStatus(status="unknown", version=None)


def get_release_status() -> StatusPayload:
    ttl = float(current_app.config.get("STATUS_CACHE_TTL", 300))
    now = time.time()
    cached_ts = _CACHE.get("timestamp", 0.0)
    cached_data = _CACHE.get("data")
    if cached_data and isinstance(cached_ts, float) and now - cached_ts < ttl:
        return cached_data  # type: ignore[return-value]

    github_repo = current_app.config["GITHUB_REPO"]
    docker_image = current_app.config["DOCKER_IMAGE"]

    github_status = _fetch_github_latest(github_repo)
    docker_status = _fetch_docker_latest(docker_image)

    data: StatusPayload = {
        "github": {"status": github_status.status, "version": github_status.version},
        "docker": {"status": docker_status.status, "version": docker_status.version},
    }

    _CACHE["timestamp"] = now
    _CACHE["data"] = data
    return data
