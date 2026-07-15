import sys
import tempfile
import unittest
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT.parent))

from astrbot_plugin_nas.access_control import AccessControlMixin  # noqa: E402
from astrbot_plugin_nas.file_services import FileServiceMixin  # noqa: E402


class FakeEvent:
    def __init__(self, sender_id: str):
        self.sender_id = sender_id

    def get_sender_id(self):
        return self.sender_id


class AccessHarness(AccessControlMixin):
    def __init__(self, root: Path):
        self.root = root.resolve()
        self.public_read_root = self.root / "Public"
        self.admins = {"2413474391"}
        self.allow_all_users = False


class FakeIndex:
    @staticmethod
    def find_by_path(_path: str):
        return None


class FileHarness(AccessHarness, FileServiceMixin):
    def __init__(self, root: Path):
        super().__init__(root)
        self.index = FakeIndex()

    @staticmethod
    def _strip_quotes(text: str) -> str:
        return text.strip().strip('"').strip("'")


class AdminReadAccessTests(unittest.TestCase):
    def test_admin_can_read_external_path(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            base = Path(temp_dir)
            root = base / "nas"
            external = base / "Github"
            root.mkdir()
            external.mkdir()
            harness = AccessHarness(root)

            self.assertTrue(
                harness._path_in_read_scope(
                    FakeEvent("2413474391"),
                    external / "project" / "README.md",
                )
            )

    def test_external_root_remains_outside_write_scope(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            base = Path(temp_dir)
            root = base / "nas"
            external = base / "Github"
            root.mkdir()
            external.mkdir()
            harness = AccessHarness(root)

            self.assertFalse(
                harness._path_in_event_scope(
                    FakeEvent("2413474391"),
                    external / "project" / "README.md",
                )
            )

    def test_non_admin_cannot_read_external_root(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            base = Path(temp_dir)
            root = base / "nas"
            external = base / "Github"
            root.mkdir()
            external.mkdir()
            harness = AccessHarness(root)

            self.assertFalse(
                harness._path_in_read_scope(
                    FakeEvent("10000"),
                    external / "project" / "README.md",
                )
            )


class ExternalFileResolutionTests(unittest.IsolatedAsyncioTestCase):
    async def test_get_can_resolve_external_absolute_file_for_admin(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            base = Path(temp_dir)
            root = base / "nas"
            external = base / "Github" / "README.md"
            root.mkdir()
            external.parent.mkdir()
            external.write_text("test", encoding="utf-8")
            harness = FileHarness(root)

            info, error = await harness._resolve_indexed_file(
                str(external),
                "/get",
                allow_external_read=True,
                event=FakeEvent("2413474391"),
            )

            self.assertIsNone(error)
            self.assertEqual(Path(info["path"]), external)

    async def test_write_resolution_stays_inside_nas_root(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            base = Path(temp_dir)
            root = base / "nas"
            external = base / "Github" / "README.md"
            root.mkdir()
            external.parent.mkdir()
            external.write_text("test", encoding="utf-8")
            harness = FileHarness(root)

            info, error = await harness._resolve_indexed_file(
                str(external),
                "/mv",
                event=FakeEvent("2413474391"),
            )

            self.assertIsNone(info)
            self.assertEqual(error, "文件不在可访问目录内")

if __name__ == "__main__":
    unittest.main()
