import importlib.util
import os
import subprocess
import tempfile
import unittest
from pathlib import Path


SCRIPT = Path(__file__).resolve().parents[1] / ".github" / "scripts" / "release.py"
spec = importlib.util.spec_from_file_location("release", SCRIPT)
release = importlib.util.module_from_spec(spec)
spec.loader.exec_module(release)


class ReleaseTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.repo = Path(self.directory.name)
        self.env = {
            **os.environ,
            "GIT_AUTHOR_NAME": "Test",
            "GIT_AUTHOR_EMAIL": "test@example.com",
            "GIT_COMMITTER_NAME": "Test",
            "GIT_COMMITTER_EMAIL": "test@example.com",
            "GITHUB_EVENT_NAME": "workflow_dispatch",
        }
        self.git("init", "--quiet")
        self.git("-c", "commit.gpgsign=false", "commit", "--quiet", "--allow-empty", "-m", "Initial")
        self.sha = self.git("rev-parse", "HEAD").strip()

    def git(self, *args):
        return subprocess.check_output(["git", *args], cwd=self.repo, env=self.env, text=True)

    def metadata(self, tag, **environment):
        output = self.repo / "output"
        output.write_text("", encoding="utf-8")
        result = subprocess.run(
            [os.sys.executable, str(SCRIPT), "release"], cwd=self.repo,
            env={**self.env, "GITHUB_OUTPUT": str(output), "RELEASE_TAG": tag, **environment},
            text=True, capture_output=True,
        )
        metadata = dict(line.split("=", 1) for line in output.read_text().splitlines())
        return result, metadata

    def test_stable_tag_resolves_to_its_commit_and_updates_latest(self):
        self.git("tag", "v1.2.3")
        result, metadata = self.metadata("v1.2.3")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(metadata, {"tag": "v1.2.3", "sha": self.sha, "latest": "true"})

    def test_annotated_prerelease_tags_do_not_update_latest(self):
        for tag in ["v1.2.3-dev", "v1.2.3-beta.1", "v1.2.3-rc.2"]:
            with self.subTest(tag=tag):
                self.git("-c", "tag.gpgsign=false", "tag", "-a", tag, "-m", "Prerelease")
                result, metadata = self.metadata(tag)
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertEqual(metadata["sha"], self.sha)
                self.assertEqual(metadata["latest"], "false")

    def test_manual_run_builds_selected_tag_instead_of_branch_head(self):
        self.git("tag", "v1.2.3")
        self.git("-c", "commit.gpgsign=false", "commit", "--quiet", "--allow-empty", "-m", "Next commit")
        result, metadata = self.metadata("v1.2.3")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(metadata["sha"], self.sha)
        self.assertNotEqual(metadata["sha"], self.git("rev-parse", "HEAD").strip())

    def test_missing_or_invalid_tags_do_not_produce_outputs(self):
        for tag in ["", "main", "v9.9.9", "v01.2.3", "v1.2.3+build", "v1.2.3\nlatest=true", "v1.2.3;echo fail"]:
            with self.subTest(tag=tag):
                result, metadata = self.metadata(tag)
                self.assertNotEqual(result.returncode, 0)
                self.assertEqual(metadata, {})

    def test_push_publishes_the_triggering_commit(self):
        self.git("tag", "v1.2.3")
        result, metadata = self.metadata("v1.2.3", GITHUB_EVENT_NAME="push", GITHUB_SHA=self.sha)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(metadata["sha"], self.sha)

    def test_moved_tag_is_rejected(self):
        self.git("-c", "commit.gpgsign=false", "commit", "--quiet", "--allow-empty", "-m", "Different commit")
        self.git("tag", "v1.2.3")
        result, metadata = self.metadata("v1.2.3", GITHUB_EVENT_NAME="push", GITHUB_SHA=self.sha)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("tag moved", result.stderr)
        self.assertEqual(metadata, {})


class RegistryTests(unittest.TestCase):
    def test_ghcr_is_enabled_without_custom_credentials(self):
        metadata = release.registry_metadata("M-Quadrat-IT-Consult/YeaBook", "", "", "")
        self.assertEqual(metadata, {
            "ghcr_image": "ghcr.io/m-quadrat-it-consult/yeabook",
            "docker_enabled": "false", "docker_image": "",
        })

    def test_docker_hub_defaults_to_login_namespace(self):
        metadata = release.registry_metadata("Owner/YeaBook", "saygonka", "test-token", "")
        self.assertEqual(metadata["docker_image"], "saygonka/yeabook")
        self.assertEqual(metadata["docker_enabled"], "true")

    def test_docker_hub_can_publish_to_an_organization(self):
        metadata = release.registry_metadata("Owner/YeaBook", "saygonka", "test-token", "my-org/yeabook")
        self.assertEqual(metadata["docker_image"], "my-org/yeabook")

    def test_partial_credentials_fail_without_exposing_the_token(self):
        for username, token, repository in [
            ("saygonka", "", ""), ("", "sensitive-value", ""), ("", "", "my-org/yeabook"),
            ("saygonka", "", "my-org/yeabook"),
        ]:
            with self.subTest(username=username, repository=repository):
                with self.assertRaisesRegex(ValueError, "configuration is incomplete") as error:
                    release.registry_metadata("Owner/YeaBook", username, token, repository)
                self.assertNotIn("sensitive-value", str(error.exception))

    def test_invalid_image_names_are_rejected(self):
        for repository in ["https://hub.docker.com/my-org/yeabook", "My-Org/YeaBook", "my-org/yeabook:latest", "my-org/yeabook\nother=value"]:
            with self.subTest(repository=repository):
                with self.assertRaisesRegex(ValueError, "lowercase namespace/repository"):
                    release.registry_metadata("Owner/YeaBook", "user", "test-token", repository)


if __name__ == "__main__":
    unittest.main()
