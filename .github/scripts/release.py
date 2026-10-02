"""Validate release inputs and write metadata for the publishing workflow."""

import os
import re
import subprocess
import sys


VERSION_PATTERN = re.compile(
    r"v(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)"
    r"(?:-[0-9A-Za-z-]+(?:\.[0-9A-Za-z-]+)*)?"
)
IMAGE_PATTERN = re.compile(r"[a-z0-9]+(?:[._-]+[a-z0-9]+)*/[a-z0-9]+(?:[._-]+[a-z0-9]+)*")


def release_metadata(tag):
    if not VERSION_PATTERN.fullmatch(tag) or len(tag) > 128:
        raise ValueError("Select an existing version tag, e.g. v0.1.3 or v0.1.3-dev. Build metadata (+...) is not supported in image tags.")
    try:
        sha = subprocess.check_output(
            ["git", "rev-parse", "--verify", f"refs/tags/{tag}^{{commit}}"],
            text=True, stderr=subprocess.DEVNULL,
        ).strip()
    except subprocess.CalledProcessError as error:
        raise ValueError("The requested release tag does not exist in this repository.") from error
    if os.environ.get("GITHUB_EVENT_NAME") == "push":
        expected_sha = subprocess.check_output(
            ["git", "rev-parse", "--verify", f"{os.environ['GITHUB_SHA']}^{{commit}}"], text=True,
        ).strip()
        if sha != expected_sha:
            raise ValueError("The release tag moved after this workflow was triggered; refusing to publish different code.")
    return {"tag": tag, "sha": sha, "latest": str("-" not in tag).lower()}


def registry_metadata(repository, username, token, docker_repository):
    ghcr_repository = repository.lower()
    if not IMAGE_PATTERN.fullmatch(ghcr_repository):
        raise ValueError("GITHUB_REPOSITORY must be in owner/repository format.")
    configured = bool(username or token or docker_repository)
    if configured and not (username and token):
        raise ValueError("Docker Hub configuration is incomplete: set both DOCKERHUB_USERNAME and DOCKERHUB_TOKEN, or remove all DOCKERHUB settings to publish only to GHCR.")
    docker_image = ""
    if configured:
        docker_image = docker_repository or f"{username.lower()}/{ghcr_repository.split('/')[1]}"
        if not IMAGE_PATTERN.fullmatch(docker_image):
            raise ValueError("DOCKERHUB_REPOSITORY must be a lowercase namespace/repository without a registry URL or tag.")
    return {
        "ghcr_image": f"ghcr.io/{ghcr_repository}",
        "docker_enabled": str(configured).lower(),
        "docker_image": docker_image,
    }


def main():
    if sys.argv[1] == "release":
        metadata = release_metadata(os.environ.get("RELEASE_TAG", ""))
    elif sys.argv[1] == "registries":
        metadata = registry_metadata(
            os.environ["GITHUB_REPOSITORY"],
            os.environ.get("DOCKERHUB_USERNAME", ""),
            os.environ.get("DOCKERHUB_TOKEN", ""),
            os.environ.get("DOCKERHUB_REPOSITORY", ""),
        )
        if metadata["docker_enabled"] == "false":
            print("::notice::Docker Hub is not configured; this release will be published to GHCR only.")
    else:
        raise ValueError("Unknown metadata command.")
    with open(os.environ["GITHUB_OUTPUT"], "a", encoding="utf-8") as output:
        for name, value in metadata.items():
            output.write(f"{name}={value}\n")


if __name__ == "__main__":
    try:
        main()
    except ValueError as error:
        print(f"::error::{error}", file=sys.stderr)
        sys.exit(1)
