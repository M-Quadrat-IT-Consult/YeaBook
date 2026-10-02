import sqlite3
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, List, Mapping, Optional, Set
from uuid import uuid4

from flask import current_app, g

TARGET_SCHEMA_VERSION = 3
CONTACT_FIELDS = ("id", "name", "telephone", "mobile", "other", "group_name", "company", "comment")


class BackupValidationError(ValueError):
    """The uploaded file is not a compatible YeaBook backup."""


def get_db() -> sqlite3.Connection:
    if "db" not in g:
        db_path: Path = Path(current_app.config["DATABASE"])
        db_path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(db_path, timeout=30)
        conn.row_factory = sqlite3.Row
        g.db = conn
    return g.db  # type: ignore[return-value]


def close_db(_: Optional[BaseException] = None) -> None:
    db: Optional[sqlite3.Connection] = g.pop("db", None)
    if db is not None:
        db.close()


def init_db() -> None:
    _apply_migrations(get_db())


def fetch_contacts() -> List[Mapping]:
    db = get_db()
    rows = db.execute(
        """
        SELECT id, name, telephone, mobile, other, group_name, company, comment
        FROM contacts
        ORDER BY group_name COLLATE NOCASE, name COLLATE NOCASE
        """
    ).fetchall()
    return [dict(row) for row in rows]


def fetch_contact(contact_id: int) -> Optional[Mapping]:
    db = get_db()
    row = db.execute(
        """
        SELECT id, name, telephone, mobile, other, group_name, company, comment
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
    comment: str = "",
) -> None:
    db = get_db()
    db.execute(
        """
        INSERT INTO contacts (name, telephone, mobile, other, group_name, company, comment)
        VALUES (?, ?, ?, ?, ?, ?, ?)
        """,
        (
            name,
            telephone or None,
            mobile or None,
            other or None,
            group_name,
            company or None,
            comment or None,
        ),
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
    comment: str = "",
) -> bool:
    db = get_db()
    cursor = db.execute(
        """
        UPDATE contacts
        SET name = ?, telephone = ?, mobile = ?, other = ?, group_name = ?, company = ?, comment = ?
        WHERE id = ?
        """,
        (
            name,
            telephone or None,
            mobile or None,
            other or None,
            group_name,
            company or None,
            comment or None,
            contact_id,
        ),
    )
    db.commit()
    return cursor.rowcount > 0


def delete_contact(contact_id: int) -> None:
    db = get_db()
    db.execute("DELETE FROM contacts WHERE id = ?", (contact_id,))
    db.commit()


def _contact_columns(db: sqlite3.Connection) -> Set[str]:
    return {row["name"] for row in db.execute("PRAGMA table_info(contacts)")}


def _get_schema_version(db: sqlite3.Connection) -> int:
    has_versions = db.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'schema_migrations'"
    ).fetchone()
    if has_versions:
        rows = db.execute("SELECT version FROM schema_migrations").fetchall()
        if rows:
            if (
                len(rows) != 1
                or not isinstance(rows[0]["version"], int)
                or rows[0]["version"] < 0
            ):
                raise RuntimeError("Invalid database schema version; refusing to modify the database.")
            return rows[0]["version"]

    # Releases before schema tracking must be upgraded without recreating their data.
    columns = _contact_columns(db)
    if not columns:
        return 0
    return 2 if "company" in columns else 1


def _set_schema_version(db: sqlite3.Connection, version: int) -> None:
    db.execute("CREATE TABLE IF NOT EXISTS schema_migrations (version INTEGER NOT NULL)")
    updated = db.execute("UPDATE schema_migrations SET version = ?", (version,))
    if updated.rowcount == 0:
        db.execute("INSERT INTO schema_migrations (version) VALUES (?)", (version,))


def _migrate_to_v1_create_contacts(db: sqlite3.Connection) -> None:
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


def _migrate_to_v2_add_company(db: sqlite3.Connection) -> None:
    if "company" not in _contact_columns(db):
        db.execute("ALTER TABLE contacts ADD COLUMN company TEXT")


def _migrate_to_v3_add_comment(db: sqlite3.Connection) -> None:
    if "comment" not in _contact_columns(db):
        db.execute("ALTER TABLE contacts ADD COLUMN comment TEXT")


MIGRATIONS = {
    1: _migrate_to_v1_create_contacts,
    2: _migrate_to_v2_add_company,
    3: _migrate_to_v3_add_comment,
}


def write_database_backup(output_path: Path) -> None:
    """Write a consistent, standalone SQLite snapshot, including committed WAL data."""
    db_path = Path(current_app.config["DATABASE"]).resolve()
    # A separate reader also works while a migration holds BEGIN IMMEDIATE.
    # Backing up that writer connection would hang.
    with closing(sqlite3.connect(f"{db_path.as_uri()}?mode=ro", uri=True)) as source:
        with closing(sqlite3.connect(output_path)) as destination:
            source.backup(destination, pages=256)


def _backup_database(version: int) -> Path:
    db_path = Path(current_app.config["DATABASE"]).resolve()
    backup_dir = db_path.parent / "backups"
    backup_dir.mkdir(parents=True, exist_ok=True)
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
    backup_path = backup_dir / f"{db_path.stem}-v{version}-{timestamp}-{uuid4().hex}.db"
    try:
        write_database_backup(backup_path)
    except BaseException:
        backup_path.unlink(missing_ok=True)
        raise
    current_app.logger.info("Database backup saved to %s", backup_path)
    return backup_path


def prepare_database_restore(backup_path: Path) -> None:
    """Validate and migrate a private uploaded copy before touching live data."""
    with backup_path.open("rb") as uploaded:
        if uploaded.read(16) != b"SQLite format 3\x00":
            raise BackupValidationError("restore_invalid_backup")
    try:
        with closing(sqlite3.connect(backup_path)) as backup:
            backup.row_factory = sqlite3.Row
            backup.execute("PRAGMA trusted_schema = OFF")
            if [row[0] for row in backup.execute("PRAGMA integrity_check")] != ["ok"]:
                raise BackupValidationError("restore_invalid_backup")
            allowed_tables = {"contacts", "schema_migrations", "sqlite_sequence", "sqlite_stat1", "sqlite_stat4"}
            for row in backup.execute("SELECT type, name FROM sqlite_master"):
                if row["type"] in {"view", "trigger"} or (
                    row["type"] == "table" and row["name"] not in allowed_tables
                ):
                    raise BackupValidationError("restore_invalid_backup")
            version = _get_schema_version(backup)
            if version > TARGET_SCHEMA_VERSION:
                raise BackupValidationError("restore_newer_schema")
            columns = {row["name"]: row for row in backup.execute("PRAGMA table_info(contacts)")}
            required = set(CONTACT_FIELDS[:6])
            if version >= 2:
                required.add("company")
            if version >= 3:
                required.add("comment")
            if version < 1 or not required.issubset(columns) or not set(columns).issubset(CONTACT_FIELDS):
                raise BackupValidationError("restore_invalid_backup")
            if columns["id"]["type"].upper() != "INTEGER" or columns["id"]["pk"] != 1:
                raise BackupValidationError("restore_invalid_backup")
            for field, column in columns.items():
                if field != "id" and column["type"].upper() != "TEXT":
                    raise BackupValidationError("restore_invalid_backup")
            _apply_migrations(backup, backup_existing=False)
            invalid_types = ["typeof(id) != 'integer'", "id <= 0", "typeof(name) != 'text'", "typeof(group_name) != 'text'"]
            invalid_types.extend(
                f"({field} IS NOT NULL AND typeof({field}) != 'text')"
                for field in ("telephone", "mobile", "other", "company", "comment")
            )
            if backup.execute("SELECT 1 FROM contacts WHERE " + " OR ".join(invalid_types) + " LIMIT 1").fetchone():
                raise BackupValidationError("restore_invalid_backup")
    except (sqlite3.Error, RuntimeError, UnicodeError) as error:
        raise BackupValidationError("restore_invalid_backup") from error


def restore_database(backup_path: Path, publish_phonebook: Callable[[], str]) -> int:
    """Replace contact data atomically while keeping workers on the same database file."""
    db = get_db()
    with closing(sqlite3.connect(f"{backup_path.resolve().as_uri()}?mode=ro", uri=True)) as backup:
        has_sequence = backup.execute(
            "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'sqlite_sequence'"
        ).fetchone()
        sequence = backup.execute("SELECT seq FROM sqlite_sequence WHERE name = 'contacts'").fetchone() if has_sequence else None
        if sequence and (not isinstance(sequence[0], int) or sequence[0] < 0):
            raise BackupValidationError("restore_invalid_backup")
        fields = ", ".join(CONTACT_FIELDS)
        rows = backup.execute(f"SELECT {fields} FROM contacts")
        db.execute("BEGIN IMMEDIATE")
        published = False
        try:
            _backup_database(TARGET_SCHEMA_VERSION)
            db.execute("DELETE FROM contacts")
            db.executemany(f"INSERT INTO contacts ({fields}) VALUES (?, ?, ?, ?, ?, ?, ?, ?)", rows)
            db.execute("DELETE FROM sqlite_sequence WHERE name = 'contacts'")
            if sequence is not None:
                db.execute("INSERT INTO sqlite_sequence (name, seq) VALUES ('contacts', ?)", sequence)
            count = db.execute("SELECT COUNT(*) FROM contacts").fetchone()[0]
            publish_phonebook()
            published = True
            db.commit()
        except BaseException:
            db.rollback()
            if published:
                # If COMMIT failed, put the XML back in sync with the rolled-back data.
                publish_phonebook()
            raise
    current_app.logger.info("Database restored from uploaded backup (%s contacts)", count)
    return count


def _apply_migrations(db: sqlite3.Connection, *, backup_existing: bool = True) -> None:
    # Serialize startup across Gunicorn workers. DDL and version updates must
    # commit together so an interrupted migration can safely be retried.
    db.execute("BEGIN IMMEDIATE")
    try:
        original_version = _get_schema_version(db)
        if original_version > TARGET_SCHEMA_VERSION:
            raise RuntimeError(
                f"Database schema v{original_version} is newer than supported v{TARGET_SCHEMA_VERSION}; "
                "start a compatible YeaBook release."
            )
        if backup_existing and original_version < TARGET_SCHEMA_VERSION and _contact_columns(db):
            _backup_database(original_version)
        for version in range(original_version + 1, TARGET_SCHEMA_VERSION + 1):
            MIGRATIONS[version](db)
            _set_schema_version(db, version)
        required_columns = {
            "id", "name", "telephone", "mobile", "other", "group_name", "company", "comment"
        }
        if not required_columns.issubset(_contact_columns(db)):
            raise RuntimeError("Database schema does not match its version; refusing to start.")
        db.commit()
    except BaseException:
        db.rollback()
        raise

    if original_version != TARGET_SCHEMA_VERSION:
        current_app.logger.info(
            "Database schema upgraded from v%s to v%s", original_version, TARGET_SCHEMA_VERSION
        )
    else:
        current_app.logger.info("Database schema is up to date (v%s)", TARGET_SCHEMA_VERSION)
