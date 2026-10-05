import io
import sqlite3
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from threading import Event, current_thread
from unittest.mock import patch
from xml.etree import ElementTree as ET

from test_app import DatabaseTestCase, create_app


class PhonebookConcurrencyTests(DatabaseTestCase):
    def setUp(self):
        super().setUp()
        self.app = create_app(self.config)

    def names(self):
        with closing(sqlite3.connect(self.db_path)) as db:
            database = {row[0] for row in db.execute("SELECT name FROM contacts")}
        xml = {unit.attrib["Name"] for unit in ET.parse(self.data_dir / "phonebook.xml").iter("Unit")}
        return database, xml

    def check_delayed_publisher(self, second_operation, expected):
        from app.routes import write_phonebook_xml
        captured, release, second_started, second_done = Event(), Event(), Event(), Event()

        def delayed_write(contacts, *args, **kwargs):
            if current_thread().name.endswith("_0"):
                captured.set()
                if not release.wait(10):
                    raise TimeoutError("publisher was not released")
            return write_phonebook_xml(contacts, *args, **kwargs)

        def second():
            second_started.set()
            try:
                return second_operation()
            finally:
                second_done.set()

        with patch("app.routes.write_phonebook_xml", side_effect=delayed_write):
            with ThreadPoolExecutor(max_workers=2, thread_name_prefix="publisher") as executor:
                first = executor.submit(lambda: self.app.test_client().post("/contacts", data={"name": "A"}))
                try:
                    self.assertTrue(captured.wait(5))
                    later = executor.submit(second)
                    self.assertTrue(second_started.wait(5))
                    self.assertFalse(second_done.wait(0.3), "a concurrent writer escaped the publication lock")
                finally:
                    release.set()
                self.assertEqual(first.result(timeout=10).status_code, 302)
                self.assertEqual(later.result(timeout=10).status_code, 302)
        self.assertEqual(self.names(), (expected, expected))

    def test_slow_writer_cannot_overwrite_newer_contact_xml(self):
        self.check_delayed_publisher(
            lambda: self.app.test_client().post("/contacts", data={"name": "B"}), {"A", "B"})

    def test_slow_writer_cannot_overwrite_restored_xml(self):
        client = self.app.test_client()
        client.post("/contacts", data={"name": "Snapshot"})
        with closing(client.get("/backup")) as response:
            payload = response.data
        client.post("/contacts/1/delete")
        client.get("/")
        with client.session_transaction() as session:
            token = session["restore_token"]
        self.check_delayed_publisher(
            lambda: client.post("/backup/restore", data={
                "restore_token": token, "confirm_restore": "yes",
                "backup_file": (io.BytesIO(payload), "backup.db"),
            }), {"Snapshot"})

    def test_parallel_contact_updates_finish_with_matching_xml(self):
        with ThreadPoolExecutor(max_workers=8) as executor:
            responses = list(executor.map(
                lambda number: self.app.test_client().post("/contacts", data={"name": f"Contact {number}"}),
                range(24)))
        self.assertTrue(all(response.status_code == 302 for response in responses))
        expected = {f"Contact {number}" for number in range(24)}
        self.assertEqual(self.names(), (expected, expected))
