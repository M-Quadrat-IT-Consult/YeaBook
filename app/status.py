from __future__ import annotations

import json
import re
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from typing import Dict, Iterable, Optional, Tuple

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
    api_url = (
        f"https://hub.docker.com/v2/repositories/{namespace}/{name}/tags?page_size=25&page=1"
    )
    try:
        body = _http_get(api_url)
        payload = json.loads(body)
        results = payload.get("results") or []
        versions = _filter_semver_tags(result.get("name") for result in results)
        if versions:
            version = max(versions, key=_semver_key)
            return SourceStatus(status="up_to_date", version=version)
    except urllib.error.URLError:
        return SourceStatus(status="unreachable", version=None)
    except json.JSONDecodeError:
        return SourceStatus(status="unknown", version=None)
    return SourceStatus(status="unknown", version=None)


def _filter_semver_tags(names: Iterable[Optional[str]]) -> Tuple[str, ...]:
    valid = []
    for name in names:
        if not isinstance(name, str):
            continue
        tag = name.strip()
        if not tag or tag.lower() == "latest":
            continue
        if _SEMVER_PATTERN.fullmatch(tag):
            valid.append(tag)
    return tuple(valid)


_SEMVER_PATTERN = re.compile(
    r"""
    ^
    v?
    (?P<major>0|[1-9]\d*)
    \.
    (?P<minor>0|[1-9]\d*)
    \.
    (?P<patch>0|[1-9]\d*)
    (?:-(?P<prerelease>[0-9A-Za-z.-]+))?
    $
    """,
    re.VERBOSE,
)


def _semver_key(tag: str) -> Tuple[int, int, int, int, str]:
    parsed = _parse_semver(tag)
    if not parsed:
        return (0, 0, 0, 1, tag)
    major, minor, patch, prerelease = parsed
    prerelease_rank = 0 if prerelease is None else -1
    return (major, minor, patch, prerelease_rank, prerelease or "")


def _parse_semver(tag: str) -> Optional[Tuple[int, int, int, Optional[str]]]:
    match = _SEMVER_PATTERN.fullmatch(tag.strip())
    if not match:
        return None
    return (
        int(match.group("major")),
        int(match.group("minor")),
        int(match.group("patch")),
        match.group("prerelease"),
    )


def compare_versions(current_version: str, remote_version: str) -> Optional[int]:
    current_parsed = _parse_semver(current_version)
    remote_parsed = _parse_semver(remote_version)
    if current_parsed and remote_parsed:
        current_key = _semver_key(current_version)
        remote_key = _semver_key(remote_version)
        if remote_key == current_key:
            return 0
        return 1 if remote_key > current_key else -1

    current_normalized = _normalize_version_string(current_version)
    remote_normalized = _normalize_version_string(remote_version)
    if current_normalized and remote_normalized:
        if current_normalized == remote_normalized:
            return 0
    return None


def _normalize_version_string(value: str) -> Optional[str]:
    stripped = value.strip()
    if not stripped:
        return None
    return stripped[1:] if stripped.startswith("v") else stripped


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
