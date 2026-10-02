import io
import os
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from pathlib import Path
from threading import Barrier
from unittest.mock import patch
from urllib.parse import parse_qs, urlparse

from flask import Flask

# Importing app also creates the Gunicorn application. Keep that initialization
# isolated from the developer's real database, just like each individual test.
with tempfile.TemporaryDirectory() as bootstrap_dir:
    with patch.dict(os.environ, {"DATA_DIR": bootstrap_dir}):
        from app import create_app
        from app.db import TARGET_SCHEMA_VERSION, close_db, get_db, init_db


class DatabaseTestCase(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.data_dir = Path(self.directory.name)
        self.db_path = self.data_dir / "contacts.db"
        self.config = {
            "TESTING": True,
            "SECRET_KEY": "test-secret",
            "DATABASE": str(self.db_path),
            "XML_FILE": str(self.data_dir / "phonebook.xml"),
        }
        self.app = Flask(__name__)
        self.app.config.update(self.config)
        self.app.teardown_appcontext(close_db)

    def seed_legacy(self, version=1, tracked=True):
        with closing(sqlite3.connect(self.db_path)) as db:
            db.execute(
                """CREATE TABLE contacts (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    name TEXT NOT NULL, telephone TEXT, mobile TEXT, other TEXT,
                    group_name TEXT NOT NULL DEFAULT 'Contacts'
                )"""
            )
            db.execute(
                "INSERT INTO contacts VALUES (42, 'Żaneta', '+48123', '456', NULL, 'Biuro')"
            )
            if version >= 2:
                db.execute("ALTER TABLE contacts ADD COLUMN company TEXT")
                db.execute("UPDATE contacts SET company = 'Firma'")
            if tracked:
                db.execute("CREATE TABLE schema_migrations (version INTEGER NOT NULL)")
                db.execute("INSERT INTO schema_migrations VALUES (?)", (version,))
            db.commit()

    def migrate(self):
        with self.app.app_context():
            init_db()

    def dump(self):
        with closing(sqlite3.connect(self.db_path)) as db:
            return list(db.iterdump())


class MigrationTests(DatabaseTestCase):
    def test_new_database_and_repeated_startup(self):
        self.migrate()
        original = self.dump()
        self.migrate()
        self.assertEqual(self.dump(), original)
        with closing(sqlite3.connect(self.db_path)) as db:
            self.assertEqual(db.execute("SELECT version FROM schema_migrations").fetchall(), [(3,)])
            self.assertIn("comment", {row[1] for row in db.execute("PRAGMA table_info(contacts)")})
        self.assertFalse((self.data_dir / "backups").exists())

    def assert_legacy_upgrade(self, version, tracked):
        self.seed_legacy(version, tracked)
        original = self.dump()
        self.migrate()
        with closing(sqlite3.connect(self.db_path)) as db:
            self.assertEqual(
                db.execute("SELECT * FROM contacts").fetchall(),
                [(42, "Żaneta", "+48123", "456", None, "Biuro", "Firma" if version == 2 else None, None)],
            )
            self.assertEqual(db.execute("SELECT version FROM schema_migrations").fetchall(), [(3,)])
        backups = list((self.data_dir / "backups").glob("*.db"))
        self.assertEqual(len(backups), 1)
        with closing(sqlite3.connect(backups[0])) as backup:
            self.assertEqual(list(backup.iterdump()), original)
            self.assertEqual(backup.execute("PRAGMA integrity_check").fetchone()[0], "ok")
        migrated = self.dump()
        self.migrate()
        self.assertEqual(self.dump(), migrated)
        self.assertEqual(list((self.data_dir / "backups").glob("*.db")), backups)

    def test_v1_upgrade(self):
        self.assert_legacy_upgrade(1, True)

    def test_v2_upgrade(self):
        self.assert_legacy_upgrade(2, True)

    def test_unversioned_v1_upgrade(self):
        self.assert_legacy_upgrade(1, False)

    def test_unversioned_v2_upgrade(self):
        self.assert_legacy_upgrade(2, False)

    def test_failure_rolls_back_all_migrations_and_can_be_retried(self):
        self.seed_legacy()
        original = self.dump()

        def failing_migration(db):
            db.execute("ALTER TABLE contacts ADD COLUMN comment TEXT")
            db.execute("UPDATE contacts SET name = 'should roll back'")
            raise RuntimeError("simulated migration failure")

        with patch.dict("app.db.MIGRATIONS", {3: failing_migration}):
            with self.assertRaisesRegex(RuntimeError, "simulated migration failure"):
                self.migrate()
        self.assertEqual(self.dump(), original)
        self.migrate()
        with self.app.app_context():
            self.assertEqual(get_db().execute("SELECT name FROM contacts").fetchone()[0], "Żaneta")

    def test_backup_failure_prevents_schema_changes(self):
        self.seed_legacy()
        original = self.dump()
        with patch("app.db._backup_database", side_effect=OSError("backup unavailable")):
            with self.assertRaisesRegex(OSError, "backup unavailable"):
                self.migrate()
        self.assertEqual(self.dump(), original)

    def test_newer_schema_is_rejected_without_changes(self):
        self.seed_legacy(2)
        with closing(sqlite3.connect(self.db_path)) as db:
            db.execute("UPDATE schema_migrations SET version = ?", (TARGET_SCHEMA_VERSION + 1,))
            db.commit()
        original = self.dump()
        with self.assertRaisesRegex(RuntimeError, "newer than supported"):
            self.migrate()
        self.assertEqual(self.dump(), original)
        self.assertFalse((self.data_dir / "backups").exists())

    def test_invalid_version_metadata_is_rejected(self):
        self.seed_legacy()
        for versions in [[-1], ["broken"], [1, 2]]:
            with self.subTest(versions=versions):
                with closing(sqlite3.connect(self.db_path)) as db:
                    db.execute("DELETE FROM schema_migrations")
                    db.executemany("INSERT INTO schema_migrations VALUES (?)", [(v,) for v in versions])
                    db.commit()
                original = self.dump()
                with self.assertRaisesRegex(RuntimeError, "Invalid database schema version"):
                    self.migrate()
                self.assertEqual(self.dump(), original)

    def test_empty_version_table_is_inferred(self):
        self.seed_legacy()
        with closing(sqlite3.connect(self.db_path)) as db:
            db.execute("DELETE FROM schema_migrations")
            db.commit()
        self.migrate()
        with self.app.app_context():
            self.assertEqual(get_db().execute("SELECT version FROM schema_migrations").fetchone()[0], 3)

    def test_backup_includes_committed_wal_data(self):
        self.seed_legacy(2)
        with closing(sqlite3.connect(self.db_path)) as writer:
            self.assertEqual(writer.execute("PRAGMA journal_mode=WAL").fetchone()[0], "wal")
            writer.execute("UPDATE contacts SET telephone = '+48999'")
            writer.commit()
            self.migrate()
            backup_path = next((self.data_dir / "backups").glob("*.db"))
            with closing(sqlite3.connect(backup_path)) as backup:
                self.assertEqual(backup.execute("SELECT telephone FROM contacts").fetchone()[0], "+48999")

    def test_inconsistent_schema_is_rejected(self):
        self.seed_legacy(2)
        with closing(sqlite3.connect(self.db_path)) as db:
            db.execute("UPDATE schema_migrations SET version = 3")
            db.commit()
        original = self.dump()
        with self.assertRaisesRegex(RuntimeError, "does not match its version"):
            self.migrate()
        self.assertEqual(self.dump(), original)

    def test_concurrent_workers_migrate_once(self):
        self.seed_legacy()
        barrier = Barrier(4)

        def start_worker(_):
            barrier.wait(timeout=10)
            self.migrate()

        with ThreadPoolExecutor(max_workers=4) as workers:
            list(workers.map(start_worker, range(4)))
        self.assertEqual(len(list((self.data_dir / "backups").glob("*.db"))), 1)
        with self.app.app_context():
            self.assertEqual(get_db().execute("SELECT COUNT(*) FROM contacts").fetchone()[0], 1)


class ContactTests(DatabaseTestCase):
    def setUp(self):
        super().setUp()
        self.app = create_app(self.config)
        self.client = self.app.test_client()

    def contact(self):
        with self.app.app_context():
            return dict(get_db().execute("SELECT * FROM contacts").fetchone())

    def test_add_edit_clear_and_delete_comment(self):
        comment = "Zadzwoń po 15:00\nZapytaj o zamówienie."
        response = self.client.post("/contacts", data={"name": "Jan", "comment": comment}, follow_redirects=True)
        self.assertEqual(response.status_code, 200)
        self.assertIn(comment, response.get_data(as_text=True))
        self.assertEqual(self.contact()["comment"], comment)
        contact_id = self.contact()["id"]
        form = self.client.get(f"/?edit={contact_id}").get_data(as_text=True)
        self.assertIn(comment, form)
        self.client.post(f"/contacts/{contact_id}/update", data={"name": "Jan", "comment": "Nowa treść"})
        self.assertEqual(self.contact()["comment"], "Nowa treść")
        self.client.post(f"/contacts/{contact_id}/update", data={"name": "Jan", "comment": "   "})
        self.assertIsNone(self.contact()["comment"])
        self.client.post(f"/contacts/{contact_id}/delete")
        with self.app.app_context():
            self.assertEqual(get_db().execute("SELECT COUNT(*) FROM contacts").fetchone()[0], 0)

    def test_optional_comment_and_older_form_submission(self):
        self.client.post("/contacts", data={"name": "Jan"})
        self.assertIsNone(self.contact()["comment"])
        contact_id = self.contact()["id"]
        self.client.post(f"/contacts/{contact_id}/update", data={"name": "Jan", "comment": "Zachowaj"})
        self.client.post(f"/contacts/{contact_id}/update", data={"name": "Jan", "telephone": "123"})
        self.assertEqual(self.contact()["comment"], "Zachowaj")

    def test_comment_is_escaped_in_list_and_edit_form(self):
        comment = '</textarea><script>alert("x")</script> & tekst'
        self.client.post("/contacts", data={"name": "Jan", "comment": comment})
        for path in ["/", f"/?edit={self.contact()['id']}"]:
            html = self.client.get(path).get_data(as_text=True)
            self.assertNotIn(comment, html)
            self.assertIn("&lt;/textarea&gt;&lt;script&gt;", html)
        self.assertEqual(self.contact()["comment"], comment)

    def test_comment_is_not_exported_to_phonebook_xml(self):
        self.client.post("/contacts", data={"name": "Jan", "telephone": "+48123", "comment": "Internal note"})
        response = self.client.get("/phonebook.xml")
        self.assertEqual(response.status_code, 200)
        self.assertNotIn("Internal note", response.get_data(as_text=True))
        self.assertIn('Phone1="+48123"', response.get_data(as_text=True))

    def test_comments_survive_app_recreation(self):
        self.client.post("/contacts", data={"name": "Jan", "comment": "Trwały komentarz"})
        original = self.dump()
        with patch.dict(os.environ, {"APP_VERSION": "99.0.0"}):
            next_release = create_app(self.config)
        self.assertIn("Trwały komentarz", next_release.test_client().get("/").get_data(as_text=True))
        self.assertEqual(self.dump(), original)

    def test_default_database_path_is_independent_of_working_directory(self):
        release_dir = self.data_dir / "release"
        shutil.copytree(Path(__file__).resolve().parents[1] / "app", release_dir / "app")
        env = {**os.environ, "PYTHONPATH": str(release_dir)}
        env.pop("DATA_DIR", None)
        subprocess.run(
            [sys.executable, "-c", "from app import app; app.test_client().post('/contacts', data={'name': 'Persistent', 'comment': 'Across working directories'})"],
            cwd=self.data_dir, env=env, check=True, capture_output=True,
        )
        subprocess.run(
            [sys.executable, "-c", "from app import app; assert 'Across working directories' in app.test_client().get('/').get_data(as_text=True)"],
            cwd=release_dir, env=env, check=True, capture_output=True,
        )
        self.assertTrue((release_dir / "data" / "contacts.db").is_file())
        self.assertFalse((self.data_dir / "data").exists())

    def test_invalid_contact_does_not_write_comment(self):
        for data in [{"name": "", "comment": "test"}, {"name": "Jan", "telephone": "abc", "comment": "test"}]:
            self.client.post("/contacts", data=data)
        with self.app.app_context():
            self.assertEqual(get_db().execute("SELECT COUNT(*) FROM contacts").fetchone()[0], 0)

    def test_comment_translations(self):
        for language, label in [("en", "Comment"), ("de", "Kommentar"), ("pl", "Komentarz")]:
            with self.client.session_transaction() as session:
                session["language"] = language
            html = self.client.get("/").get_data(as_text=True)
            self.assertIn(f'<label for="comment">{label}</label>', html)

    def test_csv_import_with_multiline_comment(self):
        csv_content = 'Name,Phone,Comment\nAnna,123,"Pierwsza linia\nDruga linia"\n'
        response = self.client.post(
            "/import/preview",
            data={"file": (io.BytesIO(csv_content.encode("utf-8")), "contacts.csv")},
            content_type="multipart/form-data",
        )
        import_id = parse_qs(urlparse(response.location).query)["import_id"][0]
        preview = self.client.get(response.location)
        self.assertEqual(preview.status_code, 200)
        self.assertRegex(preview.get_data(as_text=True), r'value="comment"\s+selected')
        self.client.post("/import/apply", data={
            "import_id": import_id, "map_0": "name", "map_1": "telephone", "map_2": "comment",
        })
        self.assertEqual(self.contact()["comment"], "Pierwsza linia\nDruga linia")


class BackupDownloadTests(DatabaseTestCase):
    def setUp(self):
        super().setUp()
        self.app = create_app(self.config)
        self.client = self.app.test_client()
        self.download_directories = []

        def make_directory(**kwargs):
            directory = tempfile.TemporaryDirectory(dir=self.data_dir, **kwargs)
            self.download_directories.append(Path(directory.name))
            return directory

        directory_patch = patch("app.routes.TemporaryDirectory", side_effect=make_directory)
        directory_patch.start()
        self.addCleanup(directory_patch.stop)

    def downloaded_path(self, response):
        self.assertEqual(response.status_code, 200)
        path = self.data_dir / "downloaded.db"
        path.write_bytes(response.data)
        return path

    def test_download_contains_complete_database_and_can_be_restored(self):
        self.client.post("/contacts", data={
            "name": "Żaneta", "company": "Firma", "telephone": "+48123",
            "mobile": "456", "other": "789", "group_name": "Biuro",
            "comment": "Pierwsza linia\nDruga linia",
        })
        original = self.dump()
        with closing(self.client.get("/backup")) as response:
            self.assertEqual(response.mimetype, "application/vnd.sqlite3")
            self.assertRegex(
                response.headers["Content-Disposition"],
                r"attachment; filename=yeabook-backup-\d{8}T\d{12}Z\.db",
            )
            self.assertEqual(response.headers["Cache-Control"], "no-store")
            self.assertEqual(response.headers["X-Content-Type-Options"], "nosniff")
            path = self.downloaded_path(response)
            self.assertEqual(response.content_length, path.stat().st_size)
        with closing(sqlite3.connect(path)) as backup:
            self.assertEqual(backup.execute("PRAGMA integrity_check").fetchone()[0], "ok")
            self.assertEqual(list(backup.iterdump()), original)
        restored = create_app({**self.config, "DATABASE": str(path), "XML_FILE": str(self.data_dir / "restored.xml")})
        html = restored.test_client().get("/").get_data(as_text=True)
        self.assertIn("Żaneta", html)
        self.assertIn("Pierwsza linia\nDruga linia", html)
        self.assertIn('+48123', (self.data_dir / "restored.xml").read_text())
        self.assertEqual(self.dump(), original)
        self.assertTrue(all(not path.exists() for path in self.download_directories))
        self.assertFalse((self.data_dir / "backups").exists())

    def test_empty_database_can_be_downloaded(self):
        with closing(self.client.get("/backup")) as response:
            path = self.downloaded_path(response)
        with closing(sqlite3.connect(path)) as backup:
            self.assertEqual(backup.execute("SELECT COUNT(*) FROM contacts").fetchone()[0], 0)
            self.assertEqual(backup.execute("SELECT version FROM schema_migrations").fetchone()[0], 3)

    def test_snapshot_includes_committed_wal_changes_only(self):
        self.client.post("/contacts", data={"name": "Jan", "comment": "Original"})
        with closing(sqlite3.connect(self.db_path)) as writer:
            writer.execute("PRAGMA journal_mode=WAL")
            writer.execute("UPDATE contacts SET comment = 'Committed'")
            writer.commit()
            writer.execute("UPDATE contacts SET comment = 'Uncommitted'")
            with closing(self.client.get("/backup")) as response:
                path = self.downloaded_path(response)
            writer.rollback()
        with closing(sqlite3.connect(path)) as backup:
            self.assertEqual(backup.execute("SELECT comment FROM contacts").fetchone()[0], "Committed")
            self.assertEqual(backup.execute("PRAGMA integrity_check").fetchone()[0], "ok")

    def test_each_download_is_fresh(self):
        with closing(self.client.get("/backup")) as response:
            first = response.data
        self.client.post("/contacts", data={"name": "New contact"})
        with closing(self.client.get("/backup", headers={"If-Modified-Since": "Fri, 01 Jan 2100 00:00:00 GMT"})) as response:
            self.assertNotEqual(response.data, first)
            path = self.downloaded_path(response)
        with closing(sqlite3.connect(path)) as backup:
            self.assertEqual(backup.execute("SELECT name FROM contacts").fetchone()[0], "New contact")

    def test_disconnect_removes_temporary_files(self):
        response = self.client.get("/backup", buffered=False)
        self.assertEqual(response.status_code, 200)
        self.assertTrue(self.download_directories[0].exists())
        response.close()
        self.assertFalse(self.download_directories[0].exists())

    def test_failure_returns_translated_error_and_removes_partial_copy(self):
        original = self.dump()
        with self.client.session_transaction() as session:
            session["language"] = "pl"

        def fail(path):
            path.write_bytes(b"partial backup")
            raise sqlite3.OperationalError("backup unavailable")

        with patch("app.routes.write_database_backup", side_effect=fail):
            with self.assertLogs(self.app.logger, level="ERROR"):
                response = self.client.get("/backup", follow_redirects=True)
        self.assertIn("Nie udało się utworzyć kopii bazy", response.get_data(as_text=True))
        self.assertNotIn("Content-Disposition", response.headers)
        self.assertTrue(all(not path.exists() for path in self.download_directories))
        self.assertEqual(self.dump(), original)

    def test_backup_link_is_available_in_all_languages(self):
        for language, label in [
            ("en", "Download database backup"),
            ("de", "Datenbanksicherung herunterladen"),
            ("pl", "Pobierz kopię bazy"),
        ]:
            with self.client.session_transaction() as session:
                session["language"] = language
            html = self.client.get("/").get_data(as_text=True)
            self.assertIn('href="/backup"', html)
            self.assertIn(label, html)


class BackupRestoreTests(DatabaseTestCase):
    def setUp(self):
        super().setUp()
        self.app = create_app(self.config)
        self.client = self.app.test_client()
        self.client.get("/")
        with self.client.session_transaction() as session:
            self.token = session["restore_token"]
        self.upload_directories = []
        temporary_directory = tempfile.TemporaryDirectory

        def temporary_upload(**kwargs):
            directory = temporary_directory(dir=self.data_dir, **kwargs)
            self.upload_directories.append(Path(directory.name))
            return directory

        patcher = patch("app.routes.TemporaryDirectory", side_effect=temporary_upload)
        patcher.start()
        self.addCleanup(patcher.stop)

    def download(self):
        with closing(self.client.get("/backup")) as response:
            self.assertEqual(response.status_code, 200)
            return response.data

    def upload(self, payload, **fields):
        data = {
            "restore_token": self.token,
            "confirm_restore": "yes",
            "backup_file": (io.BytesIO(payload), "backup.db"),
            **fields,
        }
        return self.client.post("/backup/restore", data=data, follow_redirects=True)

    def add_contact(self, name, comment=""):
        self.client.post("/contacts", data={"name": name, "telephone": "+48123", "comment": comment})

    def assert_upload_cleanup(self):
        self.assertTrue(self.upload_directories)
        self.assertTrue(all(not path.exists() for path in self.upload_directories))

    def test_download_restore_roundtrip_and_safety_copy(self):
        self.add_contact("Żaneta", "Pierwsza linia\nDruga linia")
        payload = self.download()
        expected = self.dump()
        self.client.post("/contacts/1/update", data={"name": "Changed", "comment": "Changed"})
        self.add_contact("New contact")
        previous = self.dump()
        inode = self.db_path.stat().st_ino
        response = self.upload(payload)
        self.assertIn("Database restored: 1 contacts", response.get_data(as_text=True))
        self.assertEqual(self.dump(), expected)
        self.assertEqual(self.db_path.stat().st_ino, inode)
        xml = self.client.get("/phonebook.xml").get_data(as_text=True)
        self.assertIn("Żaneta", xml)
        self.assertNotIn("Changed", xml)
        self.assertNotIn("New contact", xml)
        backups = list((self.data_dir / "backups").glob("*.db"))
        self.assertEqual(len(backups), 1)
        with closing(sqlite3.connect(backups[0])) as backup:
            self.assertEqual(list(backup.iterdump()), previous)
            self.assertEqual(backup.execute("PRAGMA integrity_check").fetchone()[0], "ok")
        self.assert_upload_cleanup()

    def test_legacy_backups_are_migrated_without_touching_original(self):
        self.add_contact("Current")
        for version, tracked in [(1, True), (2, True), (1, False), (2, False)]:
            with self.subTest(version=version, tracked=tracked):
                legacy = self.data_dir / f"legacy-{version}-{tracked}.db"
                live_path = self.db_path
                self.db_path = legacy
                try:
                    self.seed_legacy(version, tracked)
                finally:
                    self.db_path = live_path
                payload = legacy.read_bytes()
                self.assertIn("Database restored: 1 contacts", self.upload(payload).get_data(as_text=True))
                self.assertEqual(legacy.read_bytes(), payload)
                with closing(sqlite3.connect(self.db_path)) as db:
                    self.assertEqual(db.execute("SELECT version FROM schema_migrations").fetchone()[0], 3)
                    self.assertEqual(db.execute("SELECT id, name, company, comment FROM contacts").fetchall(),
                                     [(42, "Żaneta", "Firma" if version == 2 else None, None)])
        # Only pre-restore copies of the live v3 database, no staging migration backups.
        backups = list((self.data_dir / "backups").glob("*.db"))
        self.assertEqual(len(backups), 4)
        self.assertTrue(all("-v3-" in path.name for path in backups))
        self.assert_upload_cleanup()

    def test_empty_backup_replaces_all_contacts(self):
        payload = self.download()
        self.add_contact("Current")
        self.assertIn("Database restored: 0 contacts", self.upload(payload).get_data(as_text=True))
        with closing(sqlite3.connect(self.db_path)) as db:
            self.assertEqual(db.execute("SELECT COUNT(*) FROM contacts").fetchone()[0], 0)
        self.assertNotIn("Current", self.client.get("/phonebook.xml").get_data(as_text=True))

    def test_restores_autoincrement_sequence_after_deleted_contacts(self):
        self.add_contact("First")
        self.add_contact("Deleted")
        self.client.post("/contacts/2/delete")
        payload = self.download()
        self.add_contact("Later")
        self.upload(payload)
        self.add_contact("Next")
        with closing(sqlite3.connect(self.db_path)) as db:
            self.assertEqual(db.execute("SELECT id, name FROM contacts ORDER BY id").fetchall(), [(1, "First"), (3, "Next")])

    def test_existing_connections_see_restored_data_with_wal(self):
        self.add_contact("Snapshot")
        with closing(sqlite3.connect(self.db_path)) as worker:
            worker.execute("PRAGMA journal_mode=WAL")
            payload = self.download()
            self.add_contact("Later")
            self.assertEqual(worker.execute("SELECT COUNT(*) FROM contacts").fetchone()[0], 2)
            inode = self.db_path.stat().st_ino
            self.assertIn("Database restored: 1 contacts", self.upload(payload).get_data(as_text=True))
            self.assertEqual(worker.execute("SELECT name FROM contacts").fetchall(), [("Snapshot",)])
            self.assertEqual(worker.execute("PRAGMA journal_mode").fetchone()[0], "wal")
            self.assertEqual(self.db_path.stat().st_ino, inode)
            with closing(sqlite3.connect(next((self.data_dir / "backups").glob("*.db")))) as backup:
                self.assertEqual(backup.execute("SELECT COUNT(*) FROM contacts").fetchone()[0], 2)

    def test_safety_backup_failure_preserves_database_and_xml(self):
        payload = self.download()
        self.add_contact("Current")
        original = self.dump()
        xml = (self.data_dir / "phonebook.xml").read_bytes()
        with patch("app.db.write_database_backup", side_effect=OSError("disk full")):
            with self.assertLogs(self.app.logger, level="ERROR"):
                response = self.upload(payload)
        self.assertIn("Could not restore the backup", response.get_data(as_text=True))
        self.assertEqual(self.dump(), original)
        self.assertEqual((self.data_dir / "phonebook.xml").read_bytes(), xml)
        self.assertEqual(list((self.data_dir / "backups").glob("*.db")), [])
        self.assert_upload_cleanup()

    def test_insert_failure_rolls_back_deletion(self):
        self.add_contact("Snapshot")
        payload = self.download()
        self.add_contact("Current")
        with closing(sqlite3.connect(self.db_path)) as db:
            db.execute("CREATE TRIGGER fail_insert BEFORE INSERT ON contacts BEGIN SELECT RAISE(ABORT, 'simulated failure'); END")
            db.commit()
        original = self.dump()
        with self.assertLogs(self.app.logger, level="ERROR"):
            response = self.upload(payload)
        self.assertIn("Could not restore the backup", response.get_data(as_text=True))
        self.assertEqual(self.dump(), original)
        self.assert_upload_cleanup()

    def test_xml_failure_rolls_back_database_and_keeps_previous_xml(self):
        self.add_contact("Snapshot")
        payload = self.download()
        self.add_contact("Current")
        original = self.dump()
        xml = (self.data_dir / "phonebook.xml").read_bytes()
        with patch("app.xml_utils.ET.ElementTree.write", side_effect=OSError("XML write failed")):
            with self.assertLogs(self.app.logger, level="ERROR"):
                response = self.upload(payload)
        self.assertIn("Could not restore the backup", response.get_data(as_text=True))
        self.assertEqual(self.dump(), original)
        self.assertEqual((self.data_dir / "phonebook.xml").read_bytes(), xml)
        self.assertEqual(list(self.data_dir.glob(".phonebook-*")), [])
        self.assert_upload_cleanup()

    def test_invalid_and_corrupt_files_do_not_change_data(self):
        self.add_contact("Current")
        original = self.dump()
        for payload in [b"", b"not SQLite", self.download()[:100]]:
            with self.subTest(length=len(payload)):
                response = self.upload(payload)
                self.assertIn("not a valid, compatible YeaBook", response.get_data(as_text=True))
                self.assertEqual(self.dump(), original)
        self.assertFalse((self.data_dir / "backups").exists())
        self.assert_upload_cleanup()

    def test_incompatible_schemas_and_blob_data_are_rejected(self):
        self.add_contact("Current")
        original = self.dump()
        payload = self.download()
        changes = [
            ("UPDATE schema_migrations SET version = 99", "newer database version"),
            ("UPDATE schema_migrations SET version = -1", "not a valid, compatible YeaBook"),
            ("ALTER TABLE contacts DROP COLUMN comment", "not a valid, compatible YeaBook"),
            ("CREATE TABLE unrelated (value TEXT)", "not a valid, compatible YeaBook"),
            ("CREATE VIEW contact_view AS SELECT * FROM contacts", "not a valid, compatible YeaBook"),
            ("CREATE TRIGGER contact_trigger AFTER INSERT ON contacts BEGIN DELETE FROM contacts; END", "not a valid, compatible YeaBook"),
            ("UPDATE contacts SET name = X'FF00'", "not a valid, compatible YeaBook"),
            ("UPDATE sqlite_sequence SET seq = 'invalid' WHERE name = 'contacts'", "not a valid, compatible YeaBook"),
        ]
        for statement, message in changes:
            with self.subTest(statement=statement):
                path = self.data_dir / "modified.db"
                path.write_bytes(payload)
                with closing(sqlite3.connect(path)) as backup:
                    backup.execute(statement)
                    backup.commit()
                self.assertIn(message, self.upload(path.read_bytes()).get_data(as_text=True))
                self.assertEqual(self.dump(), original)
        self.assertFalse((self.data_dir / "backups").exists())
        self.assert_upload_cleanup()

    def test_unrelated_sqlite_database_is_rejected(self):
        path = self.data_dir / "unrelated.db"
        with closing(sqlite3.connect(path)) as db:
            db.execute("CREATE TABLE schema_migrations (version INTEGER)")
            db.execute("INSERT INTO schema_migrations VALUES (0)")
            db.commit()
        self.assertIn("not a valid, compatible YeaBook", self.upload(path.read_bytes()).get_data(as_text=True))
        self.assertFalse((self.data_dir / "backups").exists())

    def test_confirmation_and_session_token_required(self):
        self.add_contact("Current")
        original = self.dump()
        payload = self.download()
        for token in ["", "wrong", "błędny"]:
            self.assertIn("form has expired or is invalid", self.upload(payload, restore_token=token).get_data(as_text=True))
        self.assertIn("Confirm that the current contacts", self.upload(payload, confirm_restore="").get_data(as_text=True))
        self.assertEqual(self.dump(), original)
        self.assertFalse((self.data_dir / "backups").exists())

    def test_file_required_and_upload_size_limit(self):
        response = self.client.post("/backup/restore", data={"restore_token": self.token, "confirm_restore": "yes"}, follow_redirects=True)
        self.assertIn("Choose a database backup", response.get_data(as_text=True))
        self.add_contact("Current")
        original = self.dump()
        with patch("app.routes.MAX_BACKUP_UPLOAD_BYTES", 32):
            response = self.upload(self.download())
            self.assertIn("maximum size of 64 MiB", response.get_data(as_text=True))
            response = self.client.post("/backup/restore", data=b"x" * (1024 * 1024 + 33), follow_redirects=True)
            self.assertIn("maximum size of 64 MiB", response.get_data(as_text=True))
        self.assertEqual(self.dump(), original)
        self.assertFalse((self.data_dir / "backups").exists())
        self.assert_upload_cleanup()

    def test_restore_form_and_messages_in_all_languages(self):
        payload = self.download()
        for language, label, success in [
            ("en", "Restore database from backup", "Database restored: 0 contacts"),
            ("de", "Datenbank aus Sicherung wiederherstellen", "Datenbank wiederhergestellt: 0 Kontakte"),
            ("pl", "Przywróć bazę z kopii", "Przywrócono bazę: 0 kontaktów"),
        ]:
            with self.client.session_transaction() as session:
                session["language"] = language
            html = self.client.get("/").get_data(as_text=True)
            self.assertIn(label, html)
            self.assertIn('action="/backup/restore"', html)
            self.assertIn('enctype="multipart/form-data"', html)
            self.assertIn(success, self.upload(payload).get_data(as_text=True))


if __name__ == "__main__":
    unittest.main()
