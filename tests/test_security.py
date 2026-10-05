import io
import sqlite3
from contextlib import closing
from pathlib import Path
from unittest.mock import patch

from werkzeug.security import check_password_hash

from test_app import DatabaseTestCase, create_app


class PanelSecurityTests(DatabaseTestCase):
    password = "A secure panel password 123!"

    def setUp(self):
        super().setUp()
        self.app = create_app(self.config)
        self.client = self.app.test_client()
        self.client.get("/settings")

    def state(self):
        with closing(sqlite3.connect(self.app.config["SECURITY_DATABASE"])) as db:
            db.row_factory = sqlite3.Row
            return dict(db.execute("SELECT * FROM panel_security").fetchone())

    def token(self, client=None):
        client = client or self.client
        with client.session_transaction() as session:
            return session["csrf_token"]

    def save(self, **fields):
        return self.client.post("/settings", data={
            "csrf_token": self.token(),
            "security_revision": self.state()["revision"],
            "username": "admin",
            **fields,
        })

    def enable(self):
        response = self.save(auth_enabled="yes", password=self.password, password_confirm=self.password)
        self.assertEqual(response.status_code, 302)
        self.assertEqual(self.state()["auth_enabled"], 1)

    def login(self, client, password=None, **options):
        client.get("/login")
        return client.post("/login" + options.get("query", ""), data={
            "csrf_token": self.token(client), "username": "admin",
            "password": password if password is not None else self.password,
        })

    def test_disabled_by_default_and_enable_protects_management(self):
        self.assertEqual(self.state()["auth_enabled"], 0)
        self.assertEqual(self.client.get("/").status_code, 200)
        self.enable()
        self.assertTrue(check_password_hash(self.state()["password_hash"], self.password))
        self.assertNotIn(self.password, Path(self.app.config["SECURITY_DATABASE"]).read_bytes().decode("latin1"))
        self.assertEqual(self.client.get("/").status_code, 200)
        anonymous = self.app.test_client()
        for method, path in [
            ("get", "/"), ("get", "/settings"), ("get", "/backup"),
            ("post", "/contacts"), ("post", "/contacts/1/update"),
            ("post", "/contacts/1/delete"), ("post", "/import/preview"),
            ("post", "/import/apply"), ("post", "/import/cancel"),
            ("post", "/backup/restore"), ("post", "/settings"),
        ]:
            with self.subTest(path=path, method=method):
                response = getattr(anonymous, method)(path)
                self.assertEqual(response.status_code, 302)
                self.assertIn("/login", response.location)
        self.assertEqual(anonymous.get("/status.json").status_code, 401)
        self.assertEqual(anonymous.get("/phonebook.xml").status_code, 200)

    def test_login_logout_and_session_rotation(self):
        self.enable()
        second = self.app.test_client()
        second.get("/login")
        original_token = self.token(second)
        response = self.login(second, password="wrong")
        self.assertIn("Incorrect username or password", response.get_data(as_text=True))
        self.assertEqual(second.get("/").status_code, 302)
        self.assertEqual(self.login(second).status_code, 302)
        self.assertNotEqual(self.token(second), original_token)
        self.assertEqual(second.get("/").status_code, 200)
        self.assertEqual(second.post("/logout", data={"csrf_token": self.token(second)}).status_code, 302)
        self.assertEqual(second.get("/").status_code, 302)

    def test_panel_pages_are_not_cached_after_logout(self):
        self.enable()
        for path in ["/", "/settings", "/backup"]:
            with closing(self.client.get(path)) as response:
                self.assertEqual(response.headers["Cache-Control"], "no-store")
        anonymous = self.app.test_client()
        self.assertEqual(anonymous.get("/login").headers["Cache-Control"], "no-store")
        self.assertEqual(anonymous.get("/phonebook.xml").status_code, 200)

    def test_settings_validation_and_current_password_for_disable(self):
        for fields, message in [
            ({"password": "short", "password_confirm": "short"}, "12 to 1024"),
            ({"password": self.password, "password_confirm": "other"}, "do not match"),
            ({"username": "", "password": self.password, "password_confirm": self.password}, "1 to 80"),
        ]:
            with self.subTest(message=message):
                response = self.save(auth_enabled="yes", **fields)
                self.assertEqual(response.status_code, 400)
                self.assertIn(message, response.get_data(as_text=True))
                self.assertEqual(self.state()["auth_enabled"], 0)
        self.enable()
        response = self.save(current_password="wrong")
        self.assertEqual(response.status_code, 400)
        self.assertEqual(self.state()["auth_enabled"], 1)
        self.assertEqual(self.save(current_password=self.password).status_code, 302)
        self.assertEqual(self.state()["auth_enabled"], 0)
        self.assertEqual(self.state()["password_hash"], "")
        self.assertEqual(self.app.test_client().get("/").status_code, 200)

    def test_password_changes_invalidate_other_sessions(self):
        self.enable()
        second = self.app.test_client()
        self.login(second)
        new_password = "Another secure password 456!"
        self.assertEqual(self.save(auth_enabled="yes", current_password=self.password,
                                  password=new_password, password_confirm=new_password).status_code, 302)
        self.assertEqual(second.get("/").status_code, 302)
        self.assertEqual(self.client.get("/").status_code, 200)
        self.login(second, password=new_password)
        self.assertEqual(second.get("/").status_code, 200)

    def test_csrf_blocks_settings_login_and_authenticated_mutations(self):
        for token in ["", "wrong", "błędny"]:
            response = self.save(csrf_token=token, auth_enabled="yes", password=self.password, password_confirm=self.password)
            self.assertEqual(response.status_code, 403)
            self.assertEqual(self.state()["auth_enabled"], 0)
        self.enable()
        for path in ["/contacts", "/contacts/1/delete", "/import/apply", "/backup/restore", "/logout"]:
            self.assertEqual(self.client.post(path, data={"name": "Blocked"}).status_code, 403)
        response = self.client.post("/contacts", data={"name": "Allowed", "csrf_token": self.token()})
        self.assertEqual(response.status_code, 302)
        anonymous = self.app.test_client()
        anonymous.get("/login")
        self.assertEqual(anonymous.post("/login", data={"username": "admin", "password": self.password}).status_code, 403)
        with closing(sqlite3.connect(self.db_path)) as db:
            self.assertEqual(db.execute("SELECT name FROM contacts").fetchall(), [("Allowed",)])

    def test_login_redirect_cannot_leave_application(self):
        self.enable()
        for destination in ["https://example.com", "//example.com", "/\\example.com", "http://["]:
            anonymous = self.app.test_client()
            response = self.login(anonymous, query="?next=" + destination)
            self.assertEqual(response.location, "/")
        anonymous = self.app.test_client()
        response = self.login(anonymous, query="?next=/settings")
        self.assertEqual(response.location, "/settings")

    def test_login_limit_is_shared_between_clients_and_expires(self):
        self.enable()
        with patch("app.security.time.time", return_value=1000):
            for _ in range(5):
                self.assertEqual(self.login(self.app.test_client(), password="wrong").status_code, 200)
            self.assertEqual(self.login(self.app.test_client()).status_code, 429)
        with patch("app.security.time.time", return_value=1301):
            self.assertEqual(self.login(self.app.test_client()).status_code, 302)

    def test_stale_settings_form_cannot_overwrite_changes(self):
        stale_revision = self.state()["revision"]
        self.save()
        response = self.save(security_revision=stale_revision, auth_enabled="yes",
                             password=self.password, password_confirm=self.password)
        self.assertEqual(response.status_code, 302)
        self.assertEqual(self.state()["auth_enabled"], 0)
        self.assertIn("another session", self.client.get("/settings").get_data(as_text=True))

    def test_settings_and_generated_secret_survive_app_recreation(self):
        self.enable()
        first = create_app({**self.config, "SECRET_KEY": None})
        second = create_app({**self.config, "SECRET_KEY": None})
        self.assertEqual(first.config["SECRET_KEY"], second.config["SECRET_KEY"])
        self.assertGreaterEqual(len(first.config["SECRET_KEY"]), 64)
        self.assertNotEqual(first.config["SECRET_KEY"], "change-me")
        self.assertEqual(second.test_client().get("/").status_code, 302)
        with patch.dict("os.environ", {"SECRET_KEY": "change-me"}):
            configured = create_app({**self.config, "SECRET_KEY": "change-me"})
        self.assertEqual(configured.config["SECRET_KEY"], first.config["SECRET_KEY"])
        self.assertEqual(Path(first.config["SECURITY_DATABASE"]).stat().st_mode & 0o777, 0o600)

    def test_contact_restore_keeps_authentication_and_does_not_export_credentials(self):
        with closing(self.client.get("/backup")) as response:
            payload = response.data
        self.enable()
        state = self.state()
        self.client.get("/")
        with self.client.session_transaction() as session:
            restore_token = session["restore_token"]
        response = self.client.post("/backup/restore", data={
            "csrf_token": self.token(), "restore_token": restore_token,
            "confirm_restore": "yes", "backup_file": (io.BytesIO(payload), "backup.db"),
        })
        self.assertEqual(response.status_code, 302)
        self.assertEqual(self.state(), state)
        self.assertEqual(self.app.test_client().get("/").status_code, 302)
        path = self.data_dir / "downloaded.db"
        path.write_bytes(payload)
        with closing(sqlite3.connect(path)) as db:
            self.assertNotIn("panel_security", {row[0] for row in db.execute("SELECT name FROM sqlite_master")})

    def test_recovery_command_preserves_contacts(self):
        self.client.post("/contacts", data={"name": "Keep"})
        original = self.dump()
        self.enable()
        result = self.app.test_cli_runner().invoke(args=["reset-panel-auth", "--yes"])
        self.assertEqual(result.exit_code, 0, result.output)
        self.assertEqual(self.state()["auth_enabled"], 0)
        self.assertEqual(self.dump(), original)

    def test_newer_security_schema_is_rejected_without_changes(self):
        self.enable()
        with closing(sqlite3.connect(self.app.config["SECURITY_DATABASE"])) as db:
            self.assertEqual(db.execute("PRAGMA user_version").fetchone()[0], 1)
            db.execute("PRAGMA user_version = 2")
            db.commit()
            original = list(db.iterdump())
        with self.assertRaisesRegex(RuntimeError, "security schema is newer"):
            create_app(self.config)
        with closing(sqlite3.connect(self.app.config["SECURITY_DATABASE"])) as db:
            self.assertEqual(list(db.iterdump()), original)
            self.assertEqual(db.execute("PRAGMA user_version").fetchone()[0], 2)

    def test_missing_security_record_does_not_silently_disable_login(self):
        with closing(sqlite3.connect(self.app.config["SECURITY_DATABASE"])) as db:
            db.execute("DELETE FROM panel_security")
            db.commit()
        with self.assertRaisesRegex(RuntimeError, "settings are missing"):
            create_app(self.config)
        with closing(sqlite3.connect(self.app.config["SECURITY_DATABASE"])) as db:
            self.assertEqual(db.execute("SELECT COUNT(*) FROM panel_security").fetchone()[0], 0)

    def test_settings_and_login_are_translated(self):
        for language, label in [("en", "Settings"), ("de", "Einstellungen"), ("pl", "Ustawienia")]:
            with self.client.session_transaction() as session:
                session["language"] = language
            self.assertIn(label, self.client.get("/settings").get_data(as_text=True))
        self.enable()
        self.assertIn("Zaloguj się do YeaBook", self.app.test_client().get("/login", headers={"Accept-Language": "pl-PL"}).get_data(as_text=True))
