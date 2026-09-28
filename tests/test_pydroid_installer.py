from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from zipfile import ZipFile

import pydroid_installer as installer


class PydroidInstallerPathTest(unittest.TestCase):
    def test_main_starts_install_directly_without_input(self):
        target = Path("/tmp/automatic-install").resolve()
        with patch.object(installer, "install", return_value=target) as install, \
                patch("builtins.input", side_effect=AssertionError("input must not be called")):
            self.assertEqual(installer.main(), target)
        install.assert_called_once_with()

    def test_download_is_selected_automatically_when_writable(self):
        working = Path("/tmp/elsewhere")
        with patch("pydroid_installer.Path.cwd", return_value=working), \
                patch("pydroid_installer.Path.is_dir", return_value=True), \
                patch("pydroid_installer.os.access", return_value=True):
            selected = installer.default_install_dir()
        self.assertEqual(selected, Path("/storage/emulated/0/Download") / installer.PROJECT_NAME)

    def test_current_directory_fallback_when_download_is_unavailable(self):
        working = Path("/tmp/writable-place")
        with patch("pydroid_installer.Path.cwd", return_value=working), \
                patch("pydroid_installer.Path.is_dir", return_value=False):
            selected = installer.default_install_dir()
        self.assertEqual(selected, working / installer.PROJECT_NAME)

    def test_running_inside_project_suggests_current_project_not_nested_copy(self):
        with tempfile.TemporaryDirectory() as directory:
            project = Path(directory) / installer.PROJECT_NAME
            project.mkdir(); (project / "main.py").write_text("", encoding="utf-8")
            with patch("pydroid_installer.Path.cwd", return_value=project), \
                    patch("pydroid_installer.Path.is_dir", return_value=False):
                self.assertEqual(installer.default_install_dir(), project.resolve())

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
