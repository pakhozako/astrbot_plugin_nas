"""Permission and path-scope helpers for command handlers."""

from pathlib import Path

from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent


class AccessControlMixin:
    @staticmethod
    def _safe_dir_name(name: str) -> bool:
        clean = name.strip()
        return (
            bool(clean)
            and clean == name
            and Path(clean).name == clean
            and "/" not in clean
            and "\\" not in clean
            and clean not in {".", ".."}
        )

    def _is_allowed(self, uid: str) -> bool:
        return self._is_admin(uid)

    def _is_admin(self, uid: str) -> bool:
        return str(uid) in self.admins

    def _is_public_user(self, uid: str) -> bool:
        return self.allow_all_users and not self._is_admin(uid)

    @staticmethod
    def _stop_event(event: AstrMessageEvent):
        try:
            event.stop_event()
        except Exception:
            pass

    def _is_group_message(self, event: AstrMessageEvent) -> bool:
        obj = getattr(event, "message_obj", None)
        group_id = getattr(obj, "group_id", None)
        if group_id:
            return True
        msg_type = getattr(obj, "type", None)
        return "group" in str(msg_type).lower()

    def _access_error(
        self,
        event: AstrMessageEvent,
        admin: bool = False,
        action: str = "执行此命令",
        public_read: bool = True,
    ) -> str | None:
        if self._is_group_message(event) and not self.allow_group_commands:
            return "群聊命令未启用，请在私聊中使用 NAS 助手"
        uid = str(event.get_sender_id())
        if admin:
            if not self._is_admin(uid):
                return f"没有权限{action}"
            return None
        if self._is_admin(uid):
            return None
        if public_read and self._is_public_user(uid):
            wait = self._public_rate_wait(uid)
            if wait > 0:
                return f"请求过快，请 {wait} 秒后再试"
            return None
        return "没有权限使用 NAS 助手"

    def _public_rate_wait(self, uid: str) -> int:
        return self._public_rate_limiter.wait_seconds(uid)

    def _resolve_public_root(self, raw: str) -> Path:
        raw = self._strip_quotes(str(raw or "Public")).strip() or "Public"
        candidate = Path(raw).expanduser()
        path = candidate.resolve() if candidate.is_absolute() else (self.root / candidate).resolve()
        try:
            path.relative_to(self.root)
            return path
        except ValueError:
            logger.warning("[NAS] public_read_dir 不在 save_root 内，已回退到 Public")
            return (self.root / "Public").resolve()

    def _safe_path(self, path: Path) -> bool:
        try:
            path.resolve().relative_to(self.root)
            return True
        except ValueError:
            return False

    @staticmethod
    def _path_under(path: Path, root: Path) -> bool:
        try:
            path.resolve().relative_to(root.resolve())
            return True
        except ValueError:
            return False

    def _scope_root_for_event(self, event: AstrMessageEvent | None) -> Path:
        if event and self._is_public_user(str(event.get_sender_id())):
            return self.public_read_root
        return self.root

    def _path_in_event_scope(self, event: AstrMessageEvent | None, path: Path) -> bool:
        if (
            event
            and self.admin_external_paths
            and self._is_admin(str(event.get_sender_id()))
        ):
            return True
        if not self._safe_path(path):
            return False
        if event and self._is_public_user(str(event.get_sender_id())):
            return self._path_under(path, self.public_read_root)
        return True

    def _display_path_for_event(self, event: AstrMessageEvent | None, path: Path) -> str:
        root = self._scope_root_for_event(event)
        try:
            rel = path.resolve().relative_to(root)
            return str(rel) if str(rel) != "." else "/"
        except ValueError:
            try:
                rel = path.resolve().relative_to(self.root)
                return str(rel) if str(rel) != "." else "/"
            except ValueError:
                if (
                    event
                    and self.admin_external_paths
                    and self._is_admin(str(event.get_sender_id()))
                ):
                    return str(path.resolve())
                return path.name

    def _filter_event_scope(self, event: AstrMessageEvent | None, rows: list[dict]) -> list[dict]:
        visible = []
        for row in rows:
            path = Path(row["path"])
            if self._path_in_event_scope(event, path):
                visible.append(row)
        return visible
