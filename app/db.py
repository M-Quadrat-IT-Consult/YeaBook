import sqlite3
from pathlib import Path
from typing import Iterable, List, Mapping, Optional, Set

from flask import current_app, g


def get_db() -> sqlite3.Connection:
    if "db" not in g:
        db_path: Path = Path(current_app.config["DATABASE"])
        db_path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(db_path)
        conn.row_factory = sqlite3.Row
        g.db = conn
    return g.db  # type: ignore[return-value]


def close_db(_: Optional[BaseException] = None) -> None:
    db: Optional[sqlite3.Connection] = g.pop("db", None)
    if db is not None:
        db.close()


def init_db() -> None:
    db = get_db()
    db.execute(
        """
        CREATE TABLE IF NOT EXISTS contacts (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL,
            telephone TEXT,
            mobile TEXT,
            other TEXT,
            group_name TEXT NOT NULL DEFAULT 'Contacts'
        )
        """
    )
    columns: Set[str] = {
        row["name"]
        for row in db.execute("PRAGMA table_info(contacts)").fetchall()
    }
    if "group_name" not in columns:
        db.execute(
            "ALTER TABLE contacts ADD COLUMN group_name TEXT NOT NULL DEFAULT 'Contacts'"
        )
    db.commit()


def fetch_contacts() -> List[Mapping]:
    db = get_db()
    rows = db.execute(
        """
        SELECT id, name, telephone, mobile, other, group_name
        FROM contacts
        ORDER BY group_name COLLATE NOCASE, name COLLATE NOCASE
        """
    ).fetchall()
    return [dict(row) for row in rows]


def fetch_contact(contact_id: int) -> Optional[Mapping]:
    db = get_db()
    row = db.execute(
        """
        SELECT id, name, telephone, mobile, other, group_name
        FROM contacts
        WHERE id = ?
        """,
        (contact_id,),
    ).fetchone()
    return dict(row) if row else None


def insert_contact(
    name: str,
    telephone: str,
    mobile: str,
    other: str,
    group_name: str,
) -> None:
    db = get_db()
    db.execute(
        """
        INSERT INTO contacts (name, telephone, mobile, other, group_name)
        VALUES (?, ?, ?, ?, ?)
        """,
        (name, telephone or None, mobile or None, other or None, group_name),
    )
    db.commit()


def update_contact(
    contact_id: int,
    name: str,
    telephone: str,
    mobile: str,
    other: str,
    group_name: str,
) -> bool:
    db = get_db()
    cursor = db.execute(
        """
        UPDATE contacts
        SET name = ?, telephone = ?, mobile = ?, other = ?, group_name = ?
        WHERE id = ?
        """,
        (name, telephone or None, mobile or None, other or None, group_name, contact_id),
    )
    db.commit()
    return cursor.rowcount > 0


def delete_contact(contact_id: int) -> None:
    db = get_db()
    db.execute("DELETE FROM contacts WHERE id = ?", (contact_id,))
    db.commit()
