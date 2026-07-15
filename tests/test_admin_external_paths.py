import sys
import tempfile
import unittest
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT.parent))

from astrbot_plugin_nas.access_control import AccessControlMixin  # noqa: E402
from astrbot_plugin_nas.config import NASSettings  # noqa: E402
from astrbot_plugin_nas.file_services import FileServiceMixin  # noqa: E402
from astrbot_plugin_nas.help_text import nas_help_text  # noqa: E402


class FakeEvent:
    def __init__(self, sender_id: str):
        self.sender_id = sender_id

    def get_sender_id(self):
        return self.sender_id


class FakeIndex:
    @staticmethod
    def find_by_path(_path: str):
        return None


class FileHarness(AccessControlMixin, FileServiceMixin):
    def __init__(self, root: Path, allow_external: bool = True):
        self.root = root.resolve()
        self.admins = {"2413474391"}
        self.admin_external_paths = allow_external
        self.allow_all_users = False
        self.public_read_root = self.root / "Public"
        self.index = FakeIndex()

    @staticmethod
    def _strip_quotes(text: str) -> str:
        return text.strip().strip('"').strip("'")

    @staticmethod
    def _log_info(_message: str):
        return None


class AdminExternalPathTests(unittest.IsolatedAsyncioTestCase):
    async def test_admin_can_resolve_external_absolute_file(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            base = Path(temp_dir)
            root = base / "nas"
            root.mkdir()
            external = base / "outside.txt"
            external.write_text("outside", encoding="utf-8")
            harness = FileHarness(root)

            info, error = await harness._resolve_indexed_file(
                str(external),
                "/get",
                event=FakeEvent("2413474391"),
            )

            self.assertIsNone(error)
            self.assertEqual(Path(info["path"]), external)

    async def test_non_admin_remains_inside_save_root(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            base = Path(temp_dir)
            root = base / "nas"
            root.mkdir()
            external = base / "outside.txt"
            external.write_text("outside", encoding="utf-8")
            harness = FileHarness(root)

            info, error = await harness._resolve_indexed_file(
                str(external),
                "/get",
                event=FakeEvent("10000"),
            )

            self.assertIsNone(info)
            self.assertEqual(error, "文件不在可访问目录内")

    def test_external_access_can_be_disabled(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            base = Path(temp_dir)
            root = base / "nas"
            root.mkdir()
            harness = FileHarness(root, allow_external=False)
            self.assertFalse(
                harness._path_in_event_scope(
                    FakeEvent("2413474391"),
                    base / "outside.txt",
                )
            )


class SimpleModeTests(unittest.TestCase):
    def test_simple_mode_is_enabled_by_default(self):
        settings = NASSettings.from_config({})
        self.assertTrue(settings.simple_mode)
        self.assertTrue(settings.admin_external_paths)

    def test_simple_help_hides_advanced_commands(self):
        help_text = nas_help_text(simple_mode=True)
        self.assertIn("/get 文件或绝对路径", help_text)
        self.assertNotIn("/batch", help_text)
        self.assertNotIn("/export", help_text)

    def test_full_help_keeps_advanced_commands(self):
        help_text = nas_help_text(simple_mode=False)
        self.assertIn("/batch", help_text)
        self.assertIn("/export", help_text)


if __name__ == "__main__":
    unittest.main()
