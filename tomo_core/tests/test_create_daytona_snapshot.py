import contextlib
import io
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from scripts import create_daytona_snapshot


class CreateDaytonaSnapshotTests(unittest.TestCase):
    def test_name_is_required(self):
        with self.assertRaises(SystemExit) as raised, contextlib.redirect_stderr(io.StringIO()):
            create_daytona_snapshot.main([])

        self.assertEqual(raised.exception.code, 2)

    def test_creates_the_named_snapshot_from_the_daytona_dockerfile_and_prints_active(self):
        daytona = SimpleNamespace(snapshot=Mock())
        daytona.snapshot.get.side_effect = create_daytona_snapshot.DaytonaNotFoundError("missing")
        created = SimpleNamespace(state="active")
        daytona.snapshot.create.return_value = created
        image = create_daytona_snapshot.Image()

        with (
            patch.object(create_daytona_snapshot, "Daytona", return_value=daytona),
            patch.object(create_daytona_snapshot.Image, "from_dockerfile", return_value=image) as from_dockerfile,
            patch("sys.stdout", io.StringIO()) as stdout,
        ):
            code = create_daytona_snapshot.main(["--name", "tomo-core-20260710"])

        self.assertEqual(code, 0)
        from_dockerfile.assert_called_once_with("Dockerfile.daytona")
        params = daytona.snapshot.create.call_args.args[0]
        self.assertEqual(params.name, "tomo-core-20260710")
        self.assertIs(params.image, image)
        self.assertEqual(stdout.getvalue(), "active\n")

    def test_existing_snapshot_requires_replace_without_exposing_sdk_error_text(self):
        daytona = SimpleNamespace(snapshot=Mock())
        daytona.snapshot.get.return_value = SimpleNamespace(state="active")
        secret = "daytona-api-key"

        with (
            patch.object(create_daytona_snapshot, "Daytona", return_value=daytona),
            patch("sys.stderr", io.StringIO()) as stderr,
        ):
            code = create_daytona_snapshot.main(["--name", "tomo-core-20260710"])

        self.assertEqual(code, 1)
        daytona.snapshot.create.assert_not_called()
        self.assertNotIn(secret, stderr.getvalue())

    def test_replace_deletes_existing_snapshot_before_creating_a_new_one(self):
        existing = SimpleNamespace(state="active")
        daytona = SimpleNamespace(snapshot=Mock())
        daytona.snapshot.get.return_value = existing
        daytona.snapshot.create.return_value = SimpleNamespace(state="active")

        with (
            patch.object(create_daytona_snapshot, "Daytona", return_value=daytona),
            patch.object(create_daytona_snapshot.Image, "from_dockerfile", return_value=create_daytona_snapshot.Image()),
            patch("sys.stdout", io.StringIO()),
        ):
            code = create_daytona_snapshot.main(["--name", "tomo-core-20260710", "--replace"])

        self.assertEqual(code, 0)
        daytona.snapshot.delete.assert_called_once_with(existing)
        daytona.snapshot.create.assert_called_once()

    def test_non_active_snapshot_fails_without_printing_sdk_error_text(self):
        daytona = SimpleNamespace(snapshot=Mock())
        daytona.snapshot.get.side_effect = create_daytona_snapshot.DaytonaNotFoundError("missing")
        daytona.snapshot.create.return_value = SimpleNamespace(state="build_failed", error_reason="daytona-api-key")

        with (
            patch.object(create_daytona_snapshot, "Daytona", return_value=daytona),
            patch.object(create_daytona_snapshot.Image, "from_dockerfile", return_value=create_daytona_snapshot.Image()),
            patch("sys.stdout", io.StringIO()) as stdout,
            patch("sys.stderr", io.StringIO()) as stderr,
        ):
            code = create_daytona_snapshot.main(["--name", "tomo-core-20260710"])

        self.assertEqual(code, 1)
        self.assertEqual(stdout.getvalue(), "")
        self.assertNotIn("daytona-api-key", stderr.getvalue())

    def test_docker_artifacts_include_only_the_snapshot_contract_inputs(self):
        root = Path(__file__).resolve().parents[1]
        dockerfile = (root / "Dockerfile.daytona").read_text(encoding="utf-8")

        self.assertIn("COPY pyproject.toml uv.lock ./", dockerfile)
        self.assertIn("COPY src ./src", dockerfile)
        self.assertIn("COPY SOUL.md ./SOUL.md", dockerfile)
        self.assertIn("uv sync --frozen --no-dev", dockerfile)
        self.assertIn("tomo-core --help", dockerfile)
        self.assertTrue((root / ".dockerignore").is_file())
