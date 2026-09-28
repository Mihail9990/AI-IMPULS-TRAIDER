from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from zipfile import ZipFile

import pydroid_installer as installer


class PydroidInstallerPathTest(unittest.TestCase):
    def choose(self, answers):
        iterator = iter(answers)
        output = []
        selected = installer.choose_install_dir(
            input_fn=lambda _prompt: next(iterator), output_fn=output.append
        )
        return selected, output

    def test_standard_path_can_be_selected_and_confirmed(self):
        standard = Path("/tmp/Download/AI-IMPULS-TRAIDER").resolve()
        with patch.object(installer, "default_install_dir", return_value=standard):
            selected, output = self.choose(["", "yes"])
        self.assertEqual(selected, standard)
        self.assertTrue(any(str(standard) in line for line in output))

    def test_explicit_existing_path_is_not_modified_or_nested(self):
        with tempfile.TemporaryDirectory() as directory:
            existing = Path(directory) / "live-bot"
            selected, _ = self.choose([str(existing), "y"])
        self.assertEqual(selected, existing.resolve())

    def test_explicit_path_wins_even_when_download_is_available(self):
        explicit = Path("/tmp/my-existing-bot").resolve()
        with patch.object(installer, "default_install_dir",
                          return_value=Path("/storage/emulated/0/Download") / installer.PROJECT_NAME):
            selected, _ = self.choose([str(explicit), "yes"])
        self.assertEqual(selected, explicit)

    def test_running_inside_project_suggests_current_project_not_nested_copy(self):
        with tempfile.TemporaryDirectory() as directory:
            project = Path(directory) / installer.PROJECT_NAME
            project.mkdir(); (project / "main.py").write_text("", encoding="utf-8")
            with patch("pydroid_installer.Path.cwd", return_value=project), \
                    patch("pydroid_installer.Path.is_dir", return_value=False):
                self.assertEqual(installer.default_install_dir(), project.resolve())

    def test_cancel_happens_before_installation(self):
        selected, output = self.choose(["q"])
        self.assertIsNone(selected)
        self.assertTrue(any("cancelled" in line for line in output))


class PydroidInstallerUpdateTest(unittest.TestCase):
    def make_archive(self, path: Path) -> None:
        with ZipFile(path, "w") as archive:
            archive.writestr("AI-IMPULS-TRAIDER-work/main.py", "print('new')\n")
            archive.writestr("AI-IMPULS-TRAIDER-work/requirements.txt", "")
            archive.writestr("AI-IMPULS-TRAIDER-work/bot_config.example.json", "{}\n")
            archive.writestr("AI-IMPULS-TRAIDER-work/trader/new.py", "NEW = True\n")

    def test_update_preserves_runtime_state_and_records_archive_sha(self):
        sha = "a" * 40
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory); target = root / "existing"; target.mkdir()
            preserved = {
                "bot_config.json": "private config",
                "bot_state.json": "legacy",
                "bot_state.sqlite3": "database",
                "bot_state.sqlite3-wal": "wal",
                "bot_state.sqlite3-shm": "shm",
                "bot_state.sqlite3.backup-1": "backup",
                "bot_diagnostics.log": "history",
            }
            for name, content in preserved.items():
                (target / name).write_text(content, encoding="utf-8")
            archive = root / "source.zip"; self.make_archive(archive)

            def local_download(_url, destination):
                destination.write_bytes(archive.read_bytes())

            with patch.object(installer, "installed_version", return_value=sha), \
                    patch.object(installer, "download", side_effect=local_download), \
                    patch.object(installer, "install_requirements") as requirements:
                result = installer.install(archive_url="local-test", install_dir=target)

            self.assertEqual(result, target.resolve())
            self.assertEqual((target / "installed_version.txt").read_text().strip(), sha)
            self.assertEqual((target / "main.py").read_text(), "print('new')\n")
            for name, content in preserved.items():
                self.assertEqual((target / name).read_text(), content)
            requirements.assert_called_once_with(target.resolve())


if __name__ == "__main__":
    unittest.main()
