# YeaBook – Yealink phonebook generator

Simple Flask application for managing a Yealink-compatible phonebook. The HTML5 interface lets you add, edit, and remove contacts (including Yealink “groups”), while the generated `phonebook.xml` file is served over HTTP so Yealink phones can fetch the latest version automatically. The XML output follows the remote phonebook structure described in [Christopher Wilkinson’s article](https://christopherwilkinson.co.uk/2025/yealink-telephone-xml-remote-phonebook-hosting/), using `<Menu>` nodes per group and `<Unit>` entries with `Phone1/Phone2/Phone3` attributes.

## Run with Docker

```bash
docker build -t yeabook .
docker run --rm -p 8000:8000 -v $(pwd)/data:/data yeabook
```

- Web UI: http://localhost:8000/
- XML feed: http://localhost:8000/phonebook.xml
- The host `data/` directory stores the SQLite database (`contacts.db`) and the generated `phonebook.xml`, so data persists across container restarts.

The UI pings GitHub and Docker Hub (configurable) to display a release status indicator. When a newer image/release is detected, a green LED appears beside the top-right icons.

### Switching the interface language

The UI can be displayed in English, German, or Polish. Use the language selector in the top-right corner of the page to switch instantly between translations.

### Yealink XML structure

Each contact belongs to a group (for example “Staff”, “Suppliers”, “Support”). Groups become `<Menu Name="...">` elements in the generated XML so handsets can browse contacts by section. Up to three numbers per entry are mapped to the `Phone1`, `Phone2`, and `Phone3` attributes (office, mobile, and other respectively):

```xml
<?xml version="1.0" encoding="UTF-8"?>
<YealinkIPPhoneBook>
  <Title>YeaBook Directory</Title>
  <Prompt>Select a contact</Prompt>
  <Menu Name="Staff">
    <Unit Name="Example Person" Phone1="01234567890" Phone2="07777777777" Phone3="" default_photo="Resource:"/>
  </Menu>
</YealinkIPPhoneBook>
```

The default group can be changed globally with the `DEFAULT_GROUP_NAME` environment variable or per-contact in the web form. Contacts are sorted by group and then by name before being written to the XML file.

- When filling out the form, choose an existing group from the dropdown or pick *Other (custom)…* to supply a new group name without leaving the page.

### Phone number validation

Office, mobile, and other number fields accept only `+` and digits (`0–9`). Invalid inputs are blocked both in the browser UI and server-side, ensuring the exported XML stays compatible with Yealink’s expectations.

### Customising the XML title and prompt

Override the defaults with environment variables:

```bash
docker run --rm \
  -p 8000:8000 \
  -v $(pwd)/data:/data \
  -e PHONEBOOK_TITLE="Office" \
  -e PHONEBOOK_PROMPT="Select a team member" \
  -e DEFAULT_GROUP_NAME="Staff" \
  yeabook
```

### Pointing Yealink phones to the XML feed

In the Yealink web interface, configure the remote phonebook URL to:

```
http://<server-address>:8000/phonebook.xml
```

The phone will download the latest XML every time the directory is refreshed.

> **Security reminder:** Remote phonebooks typically contain sensitive contact details. Follow the guidance from the article above—host the XML on an internal-only server or protect it behind authentication if it must be exposed on the public internet.

### Release status checks

The header buttons query GitHub and Docker Hub using the defaults defined in `app/version.py`. You can override them with environment variables (`APP_VERSION`, `GITHUB_REPO`, `DOCKER_IMAGE`) if you fork the project or host your own image. The result is cached for 5 minutes (`STATUS_CACHE_TTL`) to avoid rate limits. If a newer tag than `APP_VERSION` is discovered, the Docker icon lights up green and the tooltip shows the remote version.

## Building multi-arch images locally

Use Docker Buildx to build and push a manifest that supports both AMD64 and ARM64:

```bash
docker buildx create --use --name yeabook-builder
docker buildx build \
  --platform linux/amd64,linux/arm64 \
  --tag mxm-it/yeabook:latest \
  --tag mxm-it/yeabook:0.1.0 \
  --push \
  .
```

Replace the tags with your own Docker Hub namespace or registry.

## Automated builds via GitHub Actions

The workflow `.github/workflows/docker-release.yml` builds a multi-arch image and pushes it to:

- GitHub Container Registry: `ghcr.io/<owner>/<repo>`
- Docker Hub: optional (enabled when secrets are configured)

### Required GitHub secrets

| Secret | Description |
| ------ | ----------- |
| `DOCKERHUB_USERNAME` | Docker Hub account (optional – omit to skip Docker Hub pushes) |
| `DOCKERHUB_TOKEN` | Docker Hub access token with `write` scope (optional) |
| `DOCKERHUB_REPOSITORY` | Full Docker Hub repository name (e.g. `mxm-it/yeabook`). Defaults to `<username>/yeabook` when omitted. |

The workflow triggers automatically whenever a tag matching `v*` is pushed and can also be run manually via *Run workflow* in GitHub. Each build publishes the tag version plus `latest` to every configured registry.
