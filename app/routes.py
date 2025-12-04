import contextlib
import csv
import io
import json
import re
import uuid
from pathlib import Path
from typing import Dict, List, Mapping, Optional, Sequence, Tuple, Union

from werkzeug.datastructures import FileStorage

from flask import (
    Blueprint,
    Response,
    current_app,
    flash,
    jsonify,
    redirect,
    render_template,
    request,
    session,
    url_for,
)

from .db import (
    close_db,
    delete_contact,
    fetch_contact,
    fetch_contacts,
    init_db,
    insert_contact,
    update_contact,
)
from .i18n import get_language_options, get_message, get_ui_strings, resolve_language
from .status import compare_versions, get_release_status
from .xml_utils import write_phonebook_xml

bp = Blueprint("main", __name__)

PHONE_PATTERN = re.compile(r"^\+?[0-9]+$")
PHONE_LABEL_KEYS: Dict[str, str] = {
    "telephone": "form_telephone_label",
    "mobile": "form_mobile_label",
    "other": "form_other_label",
}
IMPORT_TARGET_FIELDS: Dict[str, str] = {
    "name": "import_field_name",
    "group_name": "import_field_group",
    "telephone": "import_field_telephone",
    "mobile": "import_field_mobile",
    "other": "import_field_other",
    "company": "import_field_company",
}
MAX_IMPORT_ROWS = 1000


def _publish_phonebook() -> str:
    contacts = fetch_contacts()
    xml_path = Path(current_app.config["XML_FILE"])
    title = current_app.config["PHONEBOOK_TITLE"]
    prompt = current_app.config["PHONEBOOK_PROMPT"]
    default_group = current_app.config["DEFAULT_GROUP_NAME"]
    return write_phonebook_xml(
        contacts,
        xml_path,
        title=title,
        prompt=prompt,
        default_group=default_group,
    )


def _get_import_cache_dir() -> Path:
    data_dir = Path(current_app.config["DATABASE"]).parent
    cache_dir = data_dir / "import-cache"
    cache_dir.mkdir(parents=True, exist_ok=True)
    return cache_dir


def _import_payload_path(import_id: str) -> Path:
    return _get_import_cache_dir() / f"{import_id}.json"


def _save_import_payload(headers: List[str], rows: List[List[str]]) -> str:
    payload = {"headers": headers, "rows": rows[:MAX_IMPORT_ROWS]}
    import_id = uuid.uuid4().hex
    _import_payload_path(import_id).write_text(
        json.dumps(payload, ensure_ascii=False),
        encoding="utf-8",
    )
    return import_id


def _load_import_payload(import_id: str) -> Optional[Dict[str, Union[List[str], List[List[str]]]]]:
    if not import_id or not re.fullmatch(r"[a-f0-9]{32}", import_id):
        return None
    path = _import_payload_path(import_id)
    if not path.exists():
        return None
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return None
    headers = payload.get("headers") or []
    rows = payload.get("rows") or []
    if not isinstance(headers, list) or not isinstance(rows, list):
        return None
    return {"headers": headers, "rows": rows}


def _delete_import_payload(import_id: str) -> None:
    path = _import_payload_path(import_id)
    with contextlib.suppress(FileNotFoundError):
        path.unlink()


def _parse_csv_upload(
    upload: Optional["FileStorage"],
) -> Tuple[Optional[List[str]], Optional[List[List[str]]]]:
    if upload is None or not upload.filename:
        return None, None
    try:
        raw_content = upload.read()
    except Exception:
        return None, None
    if not raw_content:
        return None, []

    try:
        text = raw_content.decode("utf-8-sig")
    except UnicodeDecodeError:
        return None, None

    sample = text[:2048]
    try:
        dialect = csv.Sniffer().sniff(sample)
    except csv.Error:
        dialect = csv.excel
    try:
        has_header = csv.Sniffer().has_header(sample)
    except csv.Error:
        has_header = True

    reader = csv.reader(io.StringIO(text), dialect)
    rows = list(reader)
    if not rows:
        return [], []

    header_row: Sequence[str]
    data_rows: List[Sequence[str]]
    if has_header:
        header_row = rows[0]
        data_rows = rows[1:]
    else:
        width = max(len(row) for row in rows)
        header_row = [f"Column {index + 1}" for index in range(width)]
        data_rows = rows

    headers = [(value or "").strip() or f"Column {index + 1}" for index, value in enumerate(header_row)]
    parsed_rows: List[List[str]] = []
    for row in data_rows:
        padded = list(row) + [""] * (len(headers) - len(row))
        parsed_rows.append([str(value).strip() for value in padded[: len(headers)]])

    return headers, parsed_rows


def _guess_mapping(headers: Sequence[str]) -> List[str]:
    guesses: List[str] = []
    for header in headers:
        normalized = re.sub(r"[^a-z0-9]", "", header.casefold())
        if normalized in {"name", "fullname", "kontakt", "contactname"} or normalized.startswith(
            ("name", "fullname")
        ):
            guesses.append("name")
        elif normalized.startswith(("company", "firma", "organization", "organisation")):
            guesses.append("company")
        elif normalized.startswith(("group", "department", "team")):
            guesses.append("group_name")
        elif normalized.startswith(("mobile", "cell", "gsm")):
            guesses.append("mobile")
        elif normalized.startswith(("phone", "telephone", "tel", "office")):
            guesses.append("telephone")
        elif normalized.startswith(("other", "alt", "alternate")):
            guesses.append("other")
        else:
            guesses.append("skip")
    return guesses


def _first_value(values: Sequence[str]) -> str:
    for value in values:
        if value:
            return value
    return ""


def _normalize_phone(value: str) -> str:
    """Remove spaces from phone numbers to handle CSVs with spaced digits."""
    return re.sub(r"\s+", "", value)


@bp.record_once
def _setup(state) -> None:
    app = state.app
    app.teardown_appcontext(close_db)
    with app.app_context():
        init_db()
        _publish_phonebook()


@bp.route("/", methods=["GET"])
def index():
    language = _get_language()
    ui_strings = get_ui_strings(language)
    contacts = fetch_contacts()
    default_group = current_app.config["DEFAULT_GROUP_NAME"]
    groups = sorted(
        {contact.get("group_name") or default_group for contact in contacts},
        key=lambda value: value.casefold(),
    )
    if not groups:
        groups = [default_group]
    elif default_group not in groups:
        groups.insert(0, default_group)
    edit_contact: Optional[Dict] = None
    edit_id = request.args.get("edit", type=int)
    if edit_id is not None:
        edit_contact = fetch_contact(edit_id)
        if edit_contact is None:
            flash(get_message(language, "contact_missing"), "error")
    editing_notice = (
        get_message(language, "editing_contact", name=edit_contact["name"])
        if edit_contact is not None
        else None
    )
    import_preview = None
    import_id = request.args.get("import_id")
    if import_id:
        payload = _load_import_payload(import_id)
        headers = payload.get("headers") if payload else None
        rows = payload.get("rows") if payload else None
        if headers and rows:
            import_preview = {
                "id": import_id,
                "headers": headers,
                "rows": rows[:5],
                "total_rows": len(rows),
                "suggested": _guess_mapping(headers),
            }
        elif payload is not None:
            flash(get_message(language, "import_no_rows"), "error")
        elif payload is None:
            flash(get_message(language, "import_session_missing"), "error")
    import_mapping_options = [
        {"value": "name", "label": ui_strings["import_field_name"]},
        {"value": "group_name", "label": ui_strings["import_field_group"]},
        {"value": "company", "label": ui_strings["import_field_company"]},
        {"value": "telephone", "label": ui_strings["import_field_telephone"]},
        {"value": "mobile", "label": ui_strings["import_field_mobile"]},
        {"value": "other", "label": ui_strings["import_field_other"]},
        {"value": "skip", "label": ui_strings["import_field_skip"]},
    ]
    return render_template(
        "index.html",
        contacts=contacts,
        ui=ui_strings,
        languages=get_language_options(),
        current_language=language,
        groups=groups,
        default_group=default_group,
        edit_contact=edit_contact,
        is_editing=edit_contact is not None,
        editing_notice=editing_notice,
        app_version=current_app.config["APP_VERSION"],
        status_endpoint=url_for("main.status_api"),
        import_preview=import_preview,
        import_mapping_options=import_mapping_options,
    )


@bp.route("/import/preview", methods=["POST"])
def import_preview_route():
    language = _get_language()
    upload = request.files.get("file")
    if upload is None or not upload.filename:
        flash(get_message(language, "import_file_required"), "error")
        return redirect(url_for("main.index"))

    headers, rows = _parse_csv_upload(upload)
    if headers is None and rows is None:
        flash(get_message(language, "import_invalid_csv"), "error")
        return redirect(url_for("main.index"))
    if not rows:
        flash(get_message(language, "import_no_rows"), "error")
        return redirect(url_for("main.index"))

    import_id = _save_import_payload(headers, rows)
    flash(
        get_message(language, "import_preview_ready", rows=min(len(rows), MAX_IMPORT_ROWS)),
        "success",
    )
    return redirect(url_for("main.index", import_id=import_id))


@bp.route("/import/apply", methods=["POST"])
def import_apply():
    language = _get_language()
    ui_strings = get_ui_strings(language)
    import_id = request.form.get("import_id", "")
    payload = _load_import_payload(import_id)
    headers = payload.get("headers") if payload else None
    rows = payload.get("rows") if payload else None
    if not payload or not isinstance(headers, list) or not isinstance(rows, list):
        flash(get_message(language, "import_session_missing"), "error")
        return redirect(url_for("main.index"))
    if not rows:
        flash(get_message(language, "import_no_rows"), "error")
        _delete_import_payload(import_id)
        return redirect(url_for("main.index"))

    mapping: Dict[int, str] = {}
    for index in range(len(headers)):
        selection = request.form.get(f"map_{index}", "skip")
        if selection in IMPORT_TARGET_FIELDS:
            mapping[index] = selection
        elif selection == "company":
            mapping[index] = "company"

    if "name" not in mapping.values():
        flash(get_message(language, "import_name_required"), "error")
        return redirect(url_for("main.index", import_id=import_id))

    inserted = 0
    skipped_missing_name = 0
    skipped_invalid_phone = 0

    for row in rows:
        values: Dict[str, List[str]] = {}
        for idx, target in mapping.items():
            cell = row[idx].strip() if idx < len(row) else ""
            if cell:
                values.setdefault(target, []).append(cell)

        name_parts = [part for part in values.get("name", []) if part]
        name = " ".join(name_parts).strip()
        if not name:
            skipped_missing_name += 1
            continue

        telephone = _normalize_phone(_first_value(values.get("telephone", [])))
        mobile = _normalize_phone(_first_value(values.get("mobile", [])))
        other = _normalize_phone(_first_value(values.get("other", [])))
        invalid_labels = _invalid_phone_labels(
            {"telephone": telephone, "mobile": mobile, "other": other},
            ui_strings,
        )
        if invalid_labels:
            skipped_invalid_phone += 1
            continue

        group_name = _first_value(values.get("group_name", [])) or current_app.config["DEFAULT_GROUP_NAME"]
        company = _first_value(values.get("company", []))
        insert_contact(name, telephone, mobile, other, group_name, company)
        inserted += 1

    if inserted:
        _publish_phonebook()

    _delete_import_payload(import_id)
    flash(
        get_message(
            language,
            "import_result",
            imported=inserted,
            skipped_missing_name=skipped_missing_name,
            skipped_invalid_phone=skipped_invalid_phone,
        ),
        "success" if inserted else "info",
    )
    return redirect(url_for("main.index"))


@bp.route("/import/cancel", methods=["POST"])
def import_cancel():
    import_id = request.form.get("import_id", "")
    if import_id:
        _delete_import_payload(import_id)
    return redirect(url_for("main.index"))


@bp.route("/contacts", methods=["POST"])
def create_contact():
    language = _get_language()
    ui_strings = get_ui_strings(language)
    name = (request.form.get("name") or "").strip()
    company = (request.form.get("company") or "").strip()
    telephone = (request.form.get("telephone") or "").strip()
    mobile = (request.form.get("mobile") or "").strip()
    other = (request.form.get("other") or "").strip()
    group_choice = (request.form.get("group_name") or "").strip()
    custom_group = (request.form.get("custom_group_name") or "").strip()
    group_name = custom_group if group_choice == "__custom__" else group_choice
    if not group_name:
        group_name = current_app.config["DEFAULT_GROUP_NAME"]

    if not name:
        flash(get_message(language, "contact_name_required"), "error")
        return redirect(url_for("main.index"))

    invalid_labels = _invalid_phone_labels(
        {"telephone": telephone, "mobile": mobile, "other": other},
        ui_strings,
    )
    if invalid_labels:
        flash(
            get_message(
                language,
                "invalid_phone",
                fields=", ".join(invalid_labels),
            ),
            "error",
        )
        return redirect(url_for("main.index"))

    insert_contact(name, telephone, mobile, other, group_name, company)
    _publish_phonebook()
    flash(get_message(language, "contact_added", name=name), "success")
    return redirect(url_for("main.index"))


@bp.route("/contacts/<int:contact_id>/update", methods=["POST"])
def update_contact_route(contact_id: int):
    language = _get_language()
    ui_strings = get_ui_strings(language)
    existing = fetch_contact(contact_id)
    if existing is None:
        flash(get_message(language, "contact_missing"), "error")
        return redirect(url_for("main.index"))

    name = (request.form.get("name") or "").strip()
    company = (request.form.get("company") or "").strip()
    telephone = (request.form.get("telephone") or "").strip()
    mobile = (request.form.get("mobile") or "").strip()
    other = (request.form.get("other") or "").strip()
    group_choice = (request.form.get("group_name") or "").strip()
    custom_group = (request.form.get("custom_group_name") or "").strip()
    group_name = custom_group if group_choice == "__custom__" else group_choice
    if not group_name:
        group_name = current_app.config["DEFAULT_GROUP_NAME"]

    if not name:
        flash(get_message(language, "contact_name_required"), "error")
        return redirect(url_for("main.index", edit=contact_id))

    invalid_labels = _invalid_phone_labels(
        {"telephone": telephone, "mobile": mobile, "other": other},
        ui_strings,
    )
    if invalid_labels:
        flash(
            get_message(
                language,
                "invalid_phone",
                fields=", ".join(invalid_labels),
            ),
            "error",
        )
        return redirect(url_for("main.index", edit=contact_id))

    was_updated = update_contact(
        contact_id,
        name,
        telephone,
        mobile,
        other,
        group_name,
        company,
    )
    if not was_updated:
        flash(get_message(language, "contact_missing"), "error")
        return redirect(url_for("main.index"))

    _publish_phonebook()
    flash(get_message(language, "contact_updated", name=name), "success")
    return redirect(url_for("main.index"))


@bp.route("/contacts/<int:contact_id>/delete", methods=["POST"])
def remove_contact(contact_id: int):
    language = _get_language()
    delete_contact(contact_id)
    _publish_phonebook()
    flash(get_message(language, "contact_removed"), "success")
    return redirect(url_for("main.index"))


@bp.route("/phonebook.xml", methods=["GET"])
def phonebook() -> Response:
    xml_path = Path(current_app.config["XML_FILE"])
    if not xml_path.exists():
        xml_content = _publish_phonebook()
    else:
        xml_content = xml_path.read_text(encoding="utf-8")
    return Response(xml_content, content_type="application/xml; charset=utf-8")


@bp.route("/set-language", methods=["POST"])
def set_language():
    language = resolve_language(request.form.get("language"))
    session["language"] = language
    return redirect(url_for("main.index"))


@bp.route("/status.json", methods=["GET"])
def status_api():
    raw_status = get_release_status()
    status = {
        key: dict(value) if isinstance(value, dict) else value
        for key, value in raw_status.items()
    }
    current_version = current_app.config["APP_VERSION"]
    for source in ("github", "docker"):
        info = status.get(source, {})
        remote_version = info.get("version")
        state = info.get("status")
        if state == "up_to_date" and isinstance(remote_version, str) and remote_version:
            comparison = compare_versions(current_version, remote_version)
            if comparison == 0:
                info["status"] = "current"
            elif comparison == 1:
                info["status"] = "new_release"
            elif comparison == -1:
                info["status"] = "current"
        elif not state:
            info["status"] = "unknown"
    status["current_version"] = current_version
    return jsonify(status)


def _get_language() -> str:
    language = session.get("language")
    resolved = resolve_language(language)
    session.setdefault("language", resolved)
    return resolved


def _invalid_phone_labels(
    phone_values: Dict[str, str],
    ui_strings: Mapping[str, str],
) -> List[str]:
    labels: List[str] = []
    for field, value in phone_values.items():
        if not value:
            continue
        if PHONE_PATTERN.fullmatch(value):
            continue
        label_key = PHONE_LABEL_KEYS.get(field)
        if label_key:
            labels.append(ui_strings[label_key])
        else:
            labels.append(field)
    return labels
