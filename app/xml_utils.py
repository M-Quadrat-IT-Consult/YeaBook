from collections import defaultdict
from pathlib import Path
from typing import Dict, Iterable, Mapping
from xml.etree import ElementTree as ET


def contacts_to_elementtree(
    contacts: Iterable[Mapping],
    *,
    title: str,
    prompt: str,
    default_group: str,
) -> ET.ElementTree:
    root = ET.Element("YealinkIPPhoneBook")
    title_el = ET.SubElement(root, "Title")
    title_el.text = title
    if prompt:
        prompt_el = ET.SubElement(root, "Prompt")
        prompt_el.text = prompt

    grouped_contacts: Dict[str, list[Mapping]] = defaultdict(list)
    for entry in contacts:
        group_name = (entry.get("group_name") or "").strip() or default_group
        grouped_contacts[group_name].append(entry)

    for group_name in sorted(grouped_contacts.keys(), key=str.lower):
        menu_el = ET.SubElement(root, "Menu", Name=group_name)
        for entry in sorted(
            grouped_contacts[group_name],
            key=lambda item: (item.get("name") or "").lower(),
        ):
            name = (entry.get("name") or "").strip()
            unit_attrs = {
                "Name": name,
                "Phone1": (entry.get("telephone") or "").strip(),
                "Phone2": (entry.get("mobile") or "").strip(),
                "Phone3": (entry.get("other") or "").strip(),
                "default_photo": "Resource:",
            }
            # Yealink expects empty strings for missing phone numbers
            for key, value in unit_attrs.items():
                if value is None:
                    unit_attrs[key] = ""
            ET.SubElement(menu_el, "Unit", **unit_attrs)

    return ET.ElementTree(root)


def write_phonebook_xml(
    contacts: Iterable[Mapping],
    output_path: Path,
    *,
    title: str,
    prompt: str,
    default_group: str,
) -> str:
    sanitized_contacts = []
    for entry in contacts:
        updated = dict(entry)
        updated["name"] = (entry.get("name") or "").strip()
        group_name = (entry.get("group_name") or "").strip() or default_group
        updated["group_name"] = group_name
        sanitized_contacts.append(updated)

    tree = contacts_to_elementtree(
        sanitized_contacts,
        title=title,
        prompt=prompt,
        default_group=default_group,
    )
    output_path.parent.mkdir(parents=True, exist_ok=True)
    tree.write(output_path, encoding="utf-8", xml_declaration=True)
    return ET.tostring(tree.getroot(), encoding="unicode")
