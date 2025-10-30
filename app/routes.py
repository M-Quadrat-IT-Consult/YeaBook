import re
from pathlib import Path
from typing import Dict, List, Mapping, Optional

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
    )


@bp.route("/contacts", methods=["POST"])
def create_contact():
    language = _get_language()
    ui_strings = get_ui_strings(language)
    name = (request.form.get("name") or "").strip()
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

    insert_contact(name, telephone, mobile, other, group_name)
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

    was_updated = update_contact(contact_id, name, telephone, mobile, other, group_name)
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
