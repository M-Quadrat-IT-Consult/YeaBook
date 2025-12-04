import sqlite3
from pathlib import Path
from typing import Iterable, List, Mapping, Optional, Set

from flask import current_app, g

TARGET_SCHEMA_VERSION = 2


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
            group_name TEXT NOT NULL DEFAULT 'Contacts',
            company TEXT
        )
        """
    )
    _apply_migrations(db)


def fetch_contacts() -> List[Mapping]:
    db = get_db()
    rows = db.execute(
        """
        SELECT id, name, telephone, mobile, other, group_name, company
        FROM contacts
        ORDER BY group_name COLLATE NOCASE, name COLLATE NOCASE
        """
    ).fetchall()
    return [dict(row) for row in rows]


def fetch_contact(contact_id: int) -> Optional[Mapping]:
    db = get_db()
    row = db.execute(
        """
        SELECT id, name, telephone, mobile, other, group_name, company
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
    company: str = "",
) -> None:
    db = get_db()
    db.execute(
        """
        INSERT INTO contacts (name, telephone, mobile, other, group_name, company)
        VALUES (?, ?, ?, ?, ?, ?)
        """,
        (name, telephone or None, mobile or None, other or None, group_name, company or None),
    )
    db.commit()


def update_contact(
    contact_id: int,
    name: str,
    telephone: str,
    mobile: str,
    other: str,
    group_name: str,
    company: str = "",
) -> bool:
    db = get_db()
    cursor = db.execute(
        """
        UPDATE contacts
        SET name = ?, telephone = ?, mobile = ?, other = ?, group_name = ?, company = ?
        WHERE id = ?
        """,
        (
            name,
            telephone or None,
            mobile or None,
            other or None,
            group_name,
            company or None,
            contact_id,
        ),
    )
    db.commit()
    return cursor.rowcount > 0


def delete_contact(contact_id: int) -> None:
    db = get_db()
    db.execute("DELETE FROM contacts WHERE id = ?", (contact_id,))
    db.commit()


def _get_schema_version(db: sqlite3.Connection) -> int:
    db.execute(
        """
        CREATE TABLE IF NOT EXISTS schema_migrations (
            version INTEGER NOT NULL
        )
        """
    )
    row = db.execute("SELECT version FROM schema_migrations LIMIT 1").fetchone()
    if row is None:
        columns: Set[str] = {
            row["name"] for row in db.execute("PRAGMA table_info(contacts)").fetchall()
        }
        inferred = 2 if "company" in columns else 1
        db.execute("INSERT INTO schema_migrations (version) VALUES (?)", (inferred,))
        db.commit()
        return inferred
    return int(row["version"])


def _set_schema_version(db: sqlite3.Connection, version: int) -> None:
    updated = db.execute("UPDATE schema_migrations SET version = ?", (version,))
    if updated.rowcount == 0:
        db.execute("INSERT INTO schema_migrations (version) VALUES (?)", (version,))
    db.commit()


def _migrate_to_v2_add_company(db: sqlite3.Connection) -> None:
    columns: Set[str] = {
        row["name"] for row in db.execute("PRAGMA table_info(contacts)").fetchall()
    }
    if "company" not in columns:
        db.execute("ALTER TABLE contacts ADD COLUMN company TEXT")


def _apply_migrations(db: sqlite3.Connection) -> None:
    original_version = _get_schema_version(db)
    current_version = original_version

    if current_version < 2 <= TARGET_SCHEMA_VERSION:
        _migrate_to_v2_add_company(db)
        current_version = 2

    if current_version != original_version:
        current_app.logger.info(
            "Database schema upgraded from v%s to v%s", original_version, current_version
        )
    else:
        current_app.logger.info("Database schema is up to date (v%s)", current_version)

    _set_schema_version(db, current_version)
