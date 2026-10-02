"""Run with: python tests/docker_smoke.py <locally built image>.

Uses an isolated temporary Docker volume and removes only its own resources.
"""

import os
import subprocess
import sys
import time
from uuid import uuid4


def docker(*args):
    return subprocess.run(["docker", *args], check=True, text=True, capture_output=True).stdout.strip()


def main():
    image = sys.argv[1]
    platform = os.environ.get("TEST_PLATFORM")
    platform_options = ["--platform", platform] if platform else []
    suffix = uuid4().hex
    volume = f"yeabook-test-{suffix}"
    container = f"yeabook-test-{suffix}"
    created_container = False
    docker("volume", "create", volume)
    try:
        def run_python(code, *options):
            output = docker(
                "run", "--rm", "--network", "none", "-v", f"{volume}:/data",
                *platform_options,
                "-e", f"TEST_PLATFORM={platform or ''}",
                "-e", f"EXPECTED_APP_VERSION={os.environ.get('EXPECTED_APP_VERSION', '0.1.0')}",
                *options, image, "python", "-c", code,
            )
            if output:
                print(output, flush=True)

        run_python('''
import os
import sqlite3
import struct
from pathlib import Path

assert os.getuid() == 1000
if os.environ['TEST_PLATFORM'] in ('linux/386', 'linux/arm/v7'):
    assert struct.calcsize('P') == 4, 'Expected a 32-bit Python runtime'
elif os.environ['TEST_PLATFORM'] in ('linux/amd64', 'linux/arm64'):
    assert struct.calcsize('P') == 8, 'Expected a 64-bit Python runtime'
assert not Path('/app/data').exists(), 'Runtime data was included in the image'
with sqlite3.connect('/data/contacts.db') as db:
    db.execute("CREATE TABLE contacts (id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT NOT NULL, telephone TEXT, mobile TEXT, other TEXT, group_name TEXT NOT NULL DEFAULT 'Contacts')")
    db.execute("INSERT INTO contacts VALUES (42, 'Legacy contact', '+48123', NULL, NULL, 'Office')")
    db.execute('CREATE TABLE schema_migrations (version INTEGER NOT NULL)')
    db.execute('INSERT INTO schema_migrations VALUES (1)')

from app import app
assert app.config['APP_VERSION'] == os.environ['EXPECTED_APP_VERSION']
client = app.test_client()
response = client.post('/contacts/42/update', data={'name': 'Legacy contact', 'telephone': '+48123', 'group_name': 'Office', 'comment': 'Persistent comment'})
assert response.status_code == 302
with sqlite3.connect('/data/contacts.db') as db:
    assert db.execute('SELECT id, name, telephone, comment FROM contacts').fetchall() == [(42, 'Legacy contact', '+48123', 'Persistent comment')]
    assert db.execute('SELECT version FROM schema_migrations').fetchall() == [(3,)]
backups = list(Path('/data/backups').glob('*.db'))
assert len(backups) == 1
with sqlite3.connect(backups[0]) as db:
    assert db.execute('SELECT version FROM schema_migrations').fetchall() == [(1,)]
assert Path('/data/contacts.db').stat().st_uid == 1000
print('PASS: legacy migration, backup, comments, unprivileged database creation')
''')

        check_persistence = '''
import os
from pathlib import Path
from app import app
html = app.test_client().get('/').get_data(as_text=True)
assert 'Persistent comment' in html and 'Legacy contact' in html
assert len(list(Path('/data/backups').glob('*.db'))) == 1
assert Path('/data/contacts.db').stat().st_uid == os.getuid()
print('PASS: replacement container preserved data; uid=' + str(os.getuid()))
'''
        run_python(check_persistence, "--user", "1000:1000", "-e", "APP_VERSION=99.0.0")
        run_python(check_persistence, "-e", "PUID=2345", "-e", "PGID=3456")
        run_python(check_persistence, "-e", "APP_UID=2346", "-e", "APP_GID=3457", "-e", "PUID=2345")

        docker(
            "run", "-d", "--network", "none", "--name", container, "-v", f"{volume}:/data",
            *platform_options,
            image, "gunicorn", "--bind", "0.0.0.0:8000", "--workers", "3", "app:app",
        )
        created_container = True
        probe = "import urllib.request; r = urllib.request.urlopen('http://127.0.0.1:8000/'); assert b'Persistent comment' in r.read()"
        deadline = time.monotonic() + 30
        while True:
            try:
                docker("exec", container, "python", "-c", probe)
                break
            except subprocess.CalledProcessError:
                if time.monotonic() >= deadline:
                    print(docker("logs", container))
                    raise
                time.sleep(0.2)
        print("PASS: HTTP response from Gunicorn with three workers", flush=True)
        docker("exec", container, "python", "-c", '''
import sqlite3
import tempfile
import urllib.request
from contextlib import closing
from pathlib import Path

with urllib.request.urlopen('http://127.0.0.1:8000/backup') as response:
    assert response.headers['Content-Disposition'].startswith('attachment;')
    assert response.headers['Cache-Control'] == 'no-store'
    payload = response.read()
with tempfile.TemporaryDirectory() as directory:
    path = Path(directory) / 'downloaded.db'
    path.write_bytes(payload)
    with closing(sqlite3.connect(path)) as backup:
        assert backup.execute('PRAGMA integrity_check').fetchone()[0] == 'ok'
        assert backup.execute('SELECT comment FROM contacts WHERE id = 42').fetchone()[0] == 'Persistent comment'
''')
        print("PASS: database backup downloaded over HTTP is complete and readable", flush=True)
        docker("exec", container, "python", "-c", '''
import http.cookiejar
import re
import sqlite3
import urllib.parse
import urllib.request
from pathlib import Path
from uuid import uuid4

base = 'http://127.0.0.1:8000'
opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(http.cookiejar.CookieJar()))
with opener.open(base + '/') as response:
    html = response.read().decode()
token = re.search(r'name="restore_token" value="([^"]+)"', html).group(1)
with opener.open(base + '/backup') as response:
    payload = response.read()

def post_contact(path, fields):
    with opener.open(base + path, urllib.parse.urlencode(fields).encode()) as response:
        assert response.status == 200

post_contact('/contacts/42/update', {'name': 'Changed contact', 'comment': 'Changed comment'})
post_contact('/contacts', {'name': 'New contact'})
with sqlite3.connect('/data/contacts.db') as db:
    previous = list(db.iterdump())
inode = Path('/data/contacts.db').stat().st_ino

def restore(content):
    boundary = uuid4().hex
    body = bytearray()
    for name, value in [('restore_token', token), ('confirm_restore', 'yes')]:
        body.extend(('--' + boundary + '\\r\\nContent-Disposition: form-data; name="' + name + '"\\r\\n\\r\\n' + value + '\\r\\n').encode())
    body.extend(('--' + boundary + '\\r\\nContent-Disposition: form-data; name="backup_file"; filename="downloaded.db"\\r\\nContent-Type: application/vnd.sqlite3\\r\\n\\r\\n').encode())
    body.extend(content)
    body.extend(('\\r\\n--' + boundary + '--\\r\\n').encode())
    request = urllib.request.Request(base + '/backup/restore', data=bytes(body), headers={'Content-Type': 'multipart/form-data; boundary=' + boundary})
    with opener.open(request) as response:
        assert response.status == 200
        return response.read().decode()

assert 'Database restored: 1 contacts' in restore(payload)
assert Path('/data/contacts.db').stat().st_ino == inode
with sqlite3.connect('/data/contacts.db') as db:
    assert db.execute('PRAGMA integrity_check').fetchone()[0] == 'ok'
    assert db.execute('SELECT id, name, comment FROM contacts').fetchall() == [(42, 'Legacy contact', 'Persistent comment')]
backups = [path for path in Path('/data/backups').glob('*.db') if '-v3-' in path.name]
assert len(backups) == 1
with sqlite3.connect(backups[0]) as backup:
    assert list(backup.iterdump()) == previous
for _ in range(12):
    with opener.open(base + '/') as response:
        html = response.read().decode()
        assert 'Persistent comment' in html and 'Legacy contact' in html
        assert 'Changed contact' not in html and 'New contact' not in html
with opener.open(base + '/phonebook.xml') as response:
    xml = response.read().decode()
    assert 'Legacy contact' in xml and 'Changed contact' not in xml
assert 'not a valid, compatible YeaBook' in restore(b'not a database')
with sqlite3.connect('/data/contacts.db') as db:
    assert db.execute('SELECT comment FROM contacts WHERE id = 42').fetchone()[0] == 'Persistent comment'
''')
        print("PASS: HTTP backup restoration across three workers, safety copy, XML refresh, invalid upload rejection", flush=True)
    finally:
        if created_container:
            docker("rm", "-f", container)
        docker("volume", "rm", volume)


if __name__ == "__main__":
    try:
        main()
    except subprocess.CalledProcessError as error:
        print(error.stderr, file=sys.stderr)
        raise
