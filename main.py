"""
NAS 助手 - AstrBot 私聊文件自动归档插件 v2.3.0
文件系统 = 真相源，SQLite = 索引缓存
"""

import asyncio
import json
import math
import os
import shlex
import shutil
import time
import zipfile
from collections import deque
from pathlib import Path

from astrbot.api import AstrBotConfig, logger
from astrbot.api.event import filter, AstrMessageEvent
from astrbot.api.message_components import File, Image, Video
from astrbot.api.star import Context, Star, register

from .utils import file_hash, file_fingerprint, format_size, FileClassifier
from .index import FileIndex


@register("NAS 助手", "pakhozako", "私聊文件自动归档到本地磁盘/NAS", "v2.3.0")
class NASPlugin(Star):

    def __init__(self, context: Context, config: AstrBotConfig):
        super().__init__(context)
        cfg = config or {}
        save_root = cfg.get("save_root") or str(Path("data/plugin_data/astrbot_plugin_nas"))
        self.root = Path(save_root).resolve()
        self.admins = set(str(u) for u in cfg.get("admin_users", []))
        self.allow_all_users = bool(cfg.get("allow_all_users", False))
        self.allow_group_commands = bool(cfg.get("allow_group_commands", False))
        self.max_size = int(cfg.get("max_file_size", 2048)) * 1024 * 1024
        self.auto_save = bool(cfg.get("auto_save_enabled", True))
        self.dedup = bool(cfg.get("dedup_enabled", True))
        self.confirm_ttl = int(cfg.get("delete_confirm_ttl", 120))
        self.log_enabled = bool(cfg.get("log_enabled", True))
        self.preview_text_chars = int(cfg.get("preview_text_chars", 1200))
        self.path_import_max_files = int(cfg.get("path_import_max_files", 2000))
        self.auto_repair_interval = int(cfg.get("auto_repair_interval_minutes", 0))
        self.watch_interval = int(cfg.get("watch_interval_minutes", 0))
        self.export_max_files = int(cfg.get("export_max_files", 100))
        self.batch_max_files = int(cfg.get("batch_max_files", 100))
        self.public_rate_limit = int(cfg.get("public_rate_limit_per_minute", 10))
        self.rebuild_busy_timeout = int(cfg.get("rebuild_busy_timeout_seconds", 600))
        self.public_read_dir = str(cfg.get("public_read_dir") or "Public")
        self.public_read_root = self._resolve_public_root(self.public_read_dir)

        self._load_categories(str(cfg.get("categories", "") or ""))
        self._delete_pending = {}
        self._public_rate_hits: dict[str, deque] = {}
        self._rebuilding = False
        self._rebuild_started_at = 0.0
        self._rebuild_reason = ""
        self._rebuild_token = 0
        self._file_lock = asyncio.Lock()
        self._maintenance_task = None
        self._last_repair_run = 0.0
        self._last_watch_run = 0.0

        self._init_dirs()
        self.index = FileIndex(str(self.root / "files.db"))
        logger.info(f"[NAS] 根目录: {self.root} | 自动保存: {self.auto_save}")
        if not self.admins:
            logger.warning("[NAS] 未配置管理员")

    def _log_info(self, message: str):
        if self.log_enabled:
            logger.info(message)

    def _load_categories(self, raw: str):
        FileClassifier.reset_categories()
        if not raw.strip():
            return
        try:
            categories = json.loads(raw)
            if not isinstance(categories, dict):
                raise ValueError("配置必须是 JSON 对象")
            normalized = {}
            for category, extensions in categories.items():
                if not isinstance(category, str) or not isinstance(extensions, list):
                    raise ValueError("分类名必须是字符串，扩展名必须是列表")
                if not self._safe_dir_name(category):
                    raise ValueError(f"分类名不合法: {category}")
                normalized[category] = {str(ext).lower().lstrip(".") for ext in extensions if str(ext).strip()}
            FileClassifier.CATEGORIES = normalized
        except Exception as e:
            FileClassifier.reset_categories()
            logger.warning(f"[NAS] 自定义分类规则无效，使用默认分类: {e}")

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

    def _init_dirs(self):
        for cat in FileClassifier.get_all_categories():
            (self.root / cat).mkdir(parents=True, exist_ok=True)
        (self.root / ".previews").mkdir(parents=True, exist_ok=True)
        (self.root / ".exports").mkdir(parents=True, exist_ok=True)
        self.public_read_root.mkdir(parents=True, exist_ok=True)

    # ---------- 安全与权限 ----------

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
        if self.public_rate_limit <= 0:
            return 0
        now = time.time()
        window = 60.0
        hits = self._public_rate_hits.setdefault(uid, deque())
        while hits and now - hits[0] >= window:
            hits.popleft()
        if len(hits) >= self.public_rate_limit:
            return max(1, math.ceil(window - (now - hits[0])))
        hits.append(now)
        return 0

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
                return path.name

    def _filter_event_scope(self, event: AstrMessageEvent | None, rows: list[dict]) -> list[dict]:
        visible = []
        for row in rows:
            if self._path_in_event_scope(event, Path(row["path"])):
                visible.append(row)
        return visible

    def _cleanup_pending(self):
        now = time.time()
        expired = [uid for uid, info in self._delete_pending.items()
                   if now - info["time"] > self.confirm_ttl]
        for uid in expired:
            self._delete_pending.pop(uid, None)

    def _begin_rebuild(self, reason: str, allow_stale: bool = False) -> int | None:
        now = time.time()
        if self._rebuilding:
            elapsed = int(now - self._rebuild_started_at) if self._rebuild_started_at else 0
            stale = elapsed >= max(60, self.rebuild_busy_timeout)
            if not allow_stale or not stale:
                return None
            logger.warning(f"[NAS] 索引任务状态超时，接管新任务: {self._rebuild_reason} 已运行 {elapsed} 秒")
        self._rebuild_token += 1
        self._rebuilding = True
        self._rebuild_started_at = now
        self._rebuild_reason = reason
        return self._rebuild_token

    def _finish_rebuild(self, token: int | None):
        if token is not None and token == self._rebuild_token:
            self._rebuilding = False
            self._rebuild_started_at = 0.0
            self._rebuild_reason = ""

    def _rebuild_busy_message(self) -> str | None:
        if not self._rebuilding:
            return None
        elapsed = int(time.time() - self._rebuild_started_at) if self._rebuild_started_at else 0
        if elapsed >= max(60, self.rebuild_busy_timeout):
            logger.warning(f"[NAS] 索引任务状态超时，自动释放: {self._rebuild_reason} 已运行 {elapsed} 秒")
            self._finish_rebuild(self._rebuild_token)
            return None
        reason = self._rebuild_reason or "重建"
        return f"NAS索引{reason}中，已运行 {elapsed} 秒，请稍后再试"

    def _rebuild_status_text(self) -> str:
        if not self._rebuilding:
            return "正常"
        elapsed = int(time.time() - self._rebuild_started_at) if self._rebuild_started_at else 0
        reason = self._rebuild_reason or "重建"
        return f"{reason}中 ({elapsed}秒)"

    # ---------- 启动、后台维护 ----------

    @filter.on_astrbot_loaded()
    async def on_loaded(self):
        token = self._begin_rebuild("启动重建")
        if token is None:
            return
        try:
            count = await asyncio.to_thread(self.index.rebuild_from_fs, self.root)
            logger.info(f"[NAS] 索引重建完成: {count} 个文件")
        except Exception as e:
            logger.error(f"[NAS] 索引重建失败: {e}，将从空索引开始")
        finally:
            self._finish_rebuild(token)
        self._start_maintenance()

    def _start_maintenance(self):
        if (self.auto_repair_interval <= 0 and self.watch_interval <= 0) or self._maintenance_task:
            return
        self._maintenance_task = asyncio.create_task(self._maintenance_loop())
        parts = []
        if self.auto_repair_interval > 0:
            parts.append(f"一致性检查 {self.auto_repair_interval} 分钟")
        if self.watch_interval > 0:
            parts.append(f"监控导入 {self.watch_interval} 分钟")
        logger.info("[NAS] 后台维护已启用: " + "，".join(parts))

    async def _maintenance_loop(self):
        active = [v for v in (self.auto_repair_interval, self.watch_interval) if v > 0]
        interval = max(1, min(active or [1])) * 60
        while True:
            await asyncio.sleep(interval)
            if self._rebuild_busy_message():
                continue
            now = time.time()
            try:
                if self.auto_repair_interval > 0 and now - self._last_repair_run >= self.auto_repair_interval * 60:
                    token = self._begin_rebuild("后台修复")
                    if token is None:
                        continue
                    try:
                        result = await asyncio.to_thread(self.index.repair_from_fs, self.root)
                        self._last_repair_run = now
                        self._log_info(
                            "[NAS] 后台一致性检查完成 | "
                            f"新增 {result['added']} 更新 {result['updated']} 清理 {result['removed']}"
                        )
                    finally:
                        self._finish_rebuild(token)
                if self.watch_interval > 0 and now - self._last_watch_run >= self.watch_interval * 60:
                    result = await self._run_watch_scan()
                    self._last_watch_run = now
                    self._log_info(
                        "[NAS] 后台监控导入完成 | "
                        f"新增 {result['saved']} 重复 {result['duplicate']} 跳过 {result['skipped']} 失败 {result['error']}"
                    )
            except asyncio.CancelledError:
                raise
            except Exception as e:
                logger.warning(f"[NAS] 后台维护失败: {e}")

    async def terminate(self):
        if self._maintenance_task:
            self._maintenance_task.cancel()
            try:
                await self._maintenance_task
            except asyncio.CancelledError:
                pass

    # ---------- 路径与文件工具 ----------

    def _parse_args(self, text: str) -> list[str]:
        try:
            return shlex.split(text, posix=False)
        except ValueError:
            return text.strip().split()

    def _message_text(self, event: AstrMessageEvent) -> str:
        getter = getattr(event, "get_message_str", None)
        if callable(getter):
            try:
                return str(getter() or "").strip()
            except Exception:
                pass
        return str(getattr(event, "message_str", "") or "").strip()

    def _command_payload(self, event: AstrMessageEvent, commands: set[str]) -> str:
        text = self._message_text(event)
        if not text:
            return ""
        parts = text.split(maxsplit=1)
        head = parts[0].lstrip("/")
        if head in commands:
            return parts[1].strip() if len(parts) > 1 else ""
        return text

    def _split_command_args(self, event: AstrMessageEvent, commands: set[str], maxsplit: int = -1) -> list[str]:
        payload = self._command_payload(event, commands)
        return payload.split(maxsplit=maxsplit) if payload else []

    def _parse_command_args(self, event: AstrMessageEvent, commands: set[str]) -> list[str]:
        return self._parse_args(self._command_payload(event, commands))

    @staticmethod
    def _strip_quotes(text: str) -> str:
        text = text.strip()
        if len(text) >= 2 and text[0] == text[-1] and text[0] in {"'", '"'}:
            return text[1:-1]
        return text

    def _info_from_path(self, path: Path) -> dict:
        st = path.stat()
        return {
            "path": str(path),
            "name": path.name,
            "size": st.st_size,
            "category": FileClassifier.get_category(path.name),
            "created_at": int(st.st_mtime),
            "owner": "",
            "source_path": "",
            "note": "",
        }

    def _multiple_match_message(self, results: list[dict], command: str) -> str:
        locations = "\n".join(f"  [{r['category']}] {r['name']}" for r in results[:8])
        suffix = "\n..." if len(results) > 8 else ""
        return f"找到多个文件:\n{locations}{suffix}\n请使用 {command} 分类/文件名 或完整相对路径 指定"

    async def _resolve_indexed_file(
        self,
        query: str,
        command: str,
        allow_search: bool = True,
        event: AstrMessageEvent | None = None,
    ) -> tuple[dict | None, str | None]:
        name = self._strip_quotes(query)
        if not name:
            return None, "文件名不能为空"

        base_root = self._scope_root_for_event(event)
        if os.path.isabs(name):
            file_path = Path(name).resolve()
            if not self._safe_path(file_path):
                return None, "路径不在允许范围内"
            if not self._path_in_event_scope(event, file_path):
                return None, "文件不在可访问目录内"
            if not file_path.exists():
                return None, f"文件不存在: {name}"
            if not file_path.is_file():
                return None, f"不是文件: {name}"
            info = await asyncio.to_thread(self.index.find_by_path, str(file_path))
            return info or self._info_from_path(file_path), None

        rel_path = (base_root / name).resolve()
        if ("/" in name or "\\" in name) and self._safe_path(rel_path) and self._path_in_event_scope(event, rel_path) and rel_path.exists():
            if not rel_path.is_file():
                return None, f"不是文件: {name}"
            info = await asyncio.to_thread(self.index.find_by_path, str(rel_path))
            return info or self._info_from_path(rel_path), None

        normalized = name.replace("\\", "/")
        if "/" in normalized:
            cat_part, file_part = normalized.split("/", 1)
            results = await asyncio.to_thread(self.index.find_by_name, file_part.strip())
            results = [
                r for r in results
                if r["category"] == cat_part.strip()
                or Path(r["path"]).resolve() == (base_root / normalized).resolve()
            ]
        else:
            results = await asyncio.to_thread(self.index.find_by_name, name)
            if not results and allow_search:
                results = await asyncio.to_thread(self.index.search, name)
        results = self._filter_event_scope(event, results)

        valid = []
        stale = []
        for r in results:
            path = Path(r["path"])
            if not self._safe_path(path) or not path.exists() or not path.is_file():
                stale.append(r)
            else:
                valid.append(r)
        for r in stale:
            await asyncio.to_thread(self.index.remove, r["path"])

        if not valid:
            return None, f"未找到文件: {name}"
        if len(valid) > 1:
            return None, self._multiple_match_message(valid, command)
        return valid[0], None

    async def _valid_existing_rows(self, rows: list[dict], event: AstrMessageEvent | None = None) -> list[dict]:
        visible = self._filter_event_scope(event, rows)
        valid = []
        stale = []
        for row in visible:
            path = Path(row["path"])
            if not self._safe_path(path) or not path.exists() or not path.is_file():
                stale.append(row)
            else:
                valid.append(row)
        for row in stale:
            await asyncio.to_thread(self.index.remove, row["path"])
        if stale:
            self._log_info(f"[NAS] 懒清理: {len(stale)} 条脏记录")
        return valid

    async def _select_files(self, selector: str, event: AstrMessageEvent | None = None) -> tuple[list[dict], str | None]:
        selector = self._strip_quotes(selector.strip())
        if not selector:
            return [], "选择器不能为空"

        key, value = "", selector
        if ":" in selector:
            key, value = selector.split(":", 1)
            key = key.strip().lower()
            value = value.strip()
        if not value:
            return [], "选择器内容不能为空"

        if key in {"tag", "标签"}:
            rows = await asyncio.to_thread(self.index.search_by_tag, value.lstrip("#").lower())
        elif key in {"category", "cat", "分类"}:
            rows = await asyncio.to_thread(self.index.find_by_category, value)
        elif key in {"search", "s", "搜索"}:
            rows = await asyncio.to_thread(self.index.search, value)
        elif key in {"path", "dir", "目录"}:
            root = self._scope_root_for_event(event)
            p = Path(value).expanduser()
            target = p.resolve() if p.is_absolute() else (root / p).resolve()
            if not self._path_in_event_scope(event, target):
                return [], "目录不在可访问范围内"
            rows = await asyncio.to_thread(self.index.find_under_path, str(target))
        else:
            categories = set(FileClassifier.get_all_categories())
            if selector in categories:
                rows = await asyncio.to_thread(self.index.find_by_category, selector)
            else:
                rows = await asyncio.to_thread(self.index.search, selector)

        return await self._valid_existing_rows(rows, event), None

    async def _run_watch_scan(self) -> dict:
        watches = await asyncio.to_thread(self.index.list_watches)
        counts = {"saved": 0, "duplicate": 0, "skipped": 0, "error": 0, "missing": 0, "total": 0}
        for item in watches:
            source = Path(item["path"])
            if not source.exists():
                counts["missing"] += 1
                continue
            if source.is_symlink():
                counts["skipped"] += 1
                continue
            files, truncated = await asyncio.to_thread(self._collect_import_files, source)
            if truncated:
                self._log_info(f"[NAS] 监控目录已按上限截断: {source}")
            for file_path in files:
                result = await self._archive_file(file_path, "watch", item["category"] or None)
                status = result["status"]
                counts[status if status in counts else "error"] += 1
                counts["total"] += 1
        return counts

    async def _move_info_to_dir(self, info: dict, target_dir: Path) -> tuple[bool, str]:
        src = Path(info["path"]).resolve()
        if not self._safe_path(src) or not src.exists() or not src.is_file():
            await asyncio.to_thread(self.index.remove, str(src))
            return False, f"{info['name']}: 文件不存在"
        if not self._safe_path(target_dir):
            return False, "目标目录不合法"
        await asyncio.to_thread(target_dir.mkdir, parents=True, exist_ok=True)
        dst = self._next_available_path(target_dir, src.name).resolve()
        if not self._safe_path(dst):
            return False, "目标路径不合法"
        try:
            await asyncio.to_thread(shutil.move, str(src), str(dst))
            fp = await asyncio.to_thread(file_fingerprint, str(dst))
            h = await asyncio.to_thread(file_hash, str(dst))
            new_cat = FileClassifier.get_category(dst.name)
            await asyncio.to_thread(self.index.move, str(src), h, str(dst), dst.name, fp[0], fp[1], new_cat)
            return True, str(dst.relative_to(self.root))
        except Exception as e:
            return False, f"{info['name']}: {e}"

    async def _create_export_zip(self, rows: list[dict], name: str | None = None) -> Path:
        export_dir = self.root / ".exports"
        await asyncio.to_thread(export_dir.mkdir, parents=True, exist_ok=True)
        if name:
            clean = Path(self._strip_quotes(name)).name
            if not clean.lower().endswith(".zip"):
                clean += ".zip"
        else:
            clean = f"nas_export_{time.strftime('%Y%m%d_%H%M%S')}.zip"
        zip_path = self._next_available_path(export_dir, clean)

        def write_zip():
            with zipfile.ZipFile(zip_path, "w", compression=zipfile.ZIP_DEFLATED) as zf:
                for row in rows:
                    path = Path(row["path"])
                    if path.is_symlink() or not path.is_file():
                        continue
                    arcname = str(path.resolve().relative_to(self.root))
                    zf.write(path, arcname)

        await asyncio.to_thread(write_zip)
        return zip_path

    @staticmethod
    def _next_available_path(save_dir: Path, name: str) -> Path:
        save_path = save_dir / name
        stem, suffix = save_path.stem, save_path.suffix
        idx = 1
        while save_path.exists():
            save_path = save_dir / f"{stem}({idx}){suffix}"
            idx += 1
        return save_path

    async def _archive_file(self, source: Path, owner: str, forced_category: str | None = None) -> dict:
        source = source.expanduser()
        if source.is_symlink():
            return {"status": "skipped", "reason": "跳过软链接", "source": str(source)}
        source = source.resolve()
        if not await asyncio.to_thread(source.is_file):
            return {"status": "skipped", "reason": "不是文件", "source": str(source)}

        try:
            file_size = (await asyncio.to_thread(source.stat)).st_size
        except OSError as e:
            return {"status": "error", "reason": f"读取文件信息失败: {e}", "source": str(source)}

        if file_size > self.max_size:
            return {
                "status": "skipped",
                "reason": f"文件超过限制: {format_size(file_size)}",
                "source": str(source),
            }

        try:
            src_hash = await asyncio.to_thread(file_hash, str(source))
        except OSError as e:
            return {"status": "error", "reason": f"计算哈希失败: {e}", "source": str(source)}

        existing_source = await asyncio.to_thread(self.index.has_source_path, str(source))
        if existing_source:
            existing_path = Path(existing_source)
            if self._safe_path(existing_path) and existing_path.exists():
                try:
                    existing_hash = await asyncio.to_thread(file_hash, str(existing_path))
                    if existing_hash == src_hash:
                        return {
                            "status": "duplicate",
                            "reason": f"源文件已导入: {existing_path.name}",
                            "source": str(source),
                        }
                except OSError:
                    pass
            else:
                await asyncio.to_thread(self.index.remove, existing_source)

        if self.dedup:
            existing = await asyncio.to_thread(self.index.has_hash, src_hash)
            if existing:
                existing_path = Path(existing)
                if self._safe_path(existing_path) and existing_path.exists():
                    return {
                        "status": "duplicate",
                        "reason": f"文件已存在: {Path(existing).name}",
                        "source": str(source),
                    }
                await asyncio.to_thread(self.index.remove, existing)

        name = Path(str(source.name)).name or f"file_{int(time.time())}"
        category = forced_category or FileClassifier.get_category(name)
        if not self._safe_dir_name(category):
            category = "Others"

        async with self._file_lock:
            save_dir = self.root / category
            await asyncio.to_thread(save_dir.mkdir, parents=True, exist_ok=True)
            save_path = self._next_available_path(save_dir, name)
            if not self._safe_path(save_path):
                return {"status": "error", "reason": "目标路径不合法", "source": str(source)}
            try:
                if source != save_path:
                    await asyncio.to_thread(shutil.copy2, str(source), str(save_path))
                fp = await asyncio.to_thread(file_fingerprint, str(save_path))
                await asyncio.to_thread(
                    self.index.add,
                    src_hash,
                    str(save_path),
                    save_path.name,
                    fp[0],
                    fp[1],
                    category,
                    str(owner),
                    str(source),
                )
            except Exception as e:
                return {"status": "error", "reason": f"保存失败: {e}", "source": str(source)}

        return {
            "status": "saved",
            "path": str(save_path),
            "name": save_path.name,
            "category": category,
            "size": file_size,
            "source": str(source),
        }

    def _collect_import_files(self, source: Path) -> tuple[list[Path], bool]:
        if source.is_file():
            return ([] if self._skip_internal_file(source) else [source]), False
        files = []
        truncated = False
        stack = [source]
        while stack:
            current = stack.pop()
            try:
                entries = list(current.iterdir())
            except OSError:
                continue
            for entry in entries:
                if entry.is_symlink():
                    continue
                if entry.is_dir():
                    if self._skip_internal_dir(entry):
                        continue
                    stack.append(entry)
                elif entry.is_file():
                    if self._skip_internal_file(entry):
                        continue
                    files.append(entry)
                    if len(files) >= self.path_import_max_files:
                        return files, True
        return files, truncated

    def _skip_internal_dir(self, path: Path) -> bool:
        try:
            rel = path.resolve().relative_to(self.root)
        except ValueError:
            return False
        return bool(rel.parts) and rel.parts[0] in {".previews", ".exports"}

    def _skip_internal_file(self, path: Path) -> bool:
        try:
            rel = path.resolve().relative_to(self.root)
        except ValueError:
            return False
        if bool(rel.parts) and rel.parts[0] in {".previews", ".exports"}:
            return True
        return rel.parent == Path(".") and rel.name in {"files.db", "files.db-wal", "files.db-shm"}

    def _is_text_file(self, path: Path) -> bool:
        text_exts = {
            "txt", "md", "csv", "json", "xml", "yaml", "yml", "log", "ini", "conf",
            "py", "js", "ts", "css", "html", "htm", "sh", "bat", "ps1",
        }
        return path.suffix.lower().lstrip(".") in text_exts

    @staticmethod
    def _is_image_file(path: Path) -> bool:
        return path.suffix.lower().lstrip(".") in {"jpg", "jpeg", "png", "gif", "bmp", "webp"}

    def _read_text_preview(self, path: Path) -> str:
        raw = path.read_bytes()[: max(1, self.preview_text_chars) * 4]
        for encoding in ("utf-8", "gb18030", "latin-1"):
            try:
                text = raw.decode(encoding)
                break
            except UnicodeDecodeError:
                continue
        else:
            text = raw.decode("utf-8", errors="replace")
        text = text.replace("\r\n", "\n").replace("\r", "\n")
        if len(text) > self.preview_text_chars:
            text = text[: self.preview_text_chars] + "\n..."
        return text

    def _image_preview_path(self, path: Path) -> Path:
        try:
            from PIL import Image as PILImage
            preview = self.root / ".previews" / f"{path.stem}_{path.stat().st_mtime_ns}.jpg"
            if preview.exists():
                return preview
            with PILImage.open(path) as img:
                img.thumbnail((1024, 1024))
                if img.mode not in {"RGB", "L"}:
                    img = img.convert("RGB")
                img.save(preview, "JPEG", quality=85)
            return preview
        except Exception:
            return path

    async def _send_file(self, event: AstrMessageEvent, info: dict):
        file_path = Path(info["path"])
        file_size = file_path.stat().st_size
        if file_size > self.max_size:
            yield event.plain_result(f"文件过大: {format_size(file_size)}")
            return
        self._log_info(f"[NAS] SEND | {event.get_sender_id()} | {info['category']}/{info['name']}")
        try:
            yield event.chain_result([File(name=info["name"], file=str(file_path))])
        except (asyncio.TimeoutError, Exception) as e:
            logger.warning(f"[NAS] 文件发送失败: {e}")
            yield event.plain_result("文件发送失败，可能文件较大或网络波动，请重试")
            return
        yield event.plain_result(f"已发送: {info['name']} ({format_size(file_size)})")

    # ---------- 自动接收 ----------

    @filter.event_message_type(filter.EventMessageType.PRIVATE_MESSAGE, priority=100)
    async def on_file_received(self, event: AstrMessageEvent):
        if not self.auto_save:
            return
        uid = str(event.get_sender_id())
        if not self._is_allowed(uid):
            return

        for comp in event.get_messages():
            if not isinstance(comp, (File, Image, Video)):
                continue

            self._stop_event(event)
            source = None
            if hasattr(comp, "get_file"):
                try:
                    source = await comp.get_file()
                except Exception as e:
                    logger.warning(f"[NAS] get_file 失败: {e}")
            elif hasattr(comp, "convert_to_file_path"):
                try:
                    source = await comp.convert_to_file_path()
                except Exception as e:
                    logger.warning(f"[NAS] convert_to_file_path 失败: {e}")

            if not source or not os.path.exists(source):
                continue

            result = await self._archive_file(Path(source), uid)
            if result["status"] == "saved":
                self._log_info(
                    f"[NAS] SAVE | {uid} | {result['category']}/{result['name']} | {format_size(result['size'])}"
                )
                yield event.plain_result(f"已保存到 {result['category']}/{result['name']}")
            elif result["status"] == "duplicate":
                yield event.plain_result(result["reason"])
            elif result["status"] in {"skipped", "error"}:
                yield event.plain_result(result["reason"])
            return

    # ---------- ls ----------

    @filter.command("ls", alias={"列表", "查看"}, priority=100)
    async def cmd_ls(self, event: AstrMessageEvent):
        self._stop_event(event)
        err = self._access_error(event)
        if err:
            yield event.plain_result(err)
            return
        busy = self._rebuild_busy_message()
        if busy:
            yield event.plain_result(busy)
            return

        base_root = self._scope_root_for_event(event)
        args = self._split_command_args(event, {"ls", "列表", "查看"}, maxsplit=1)
        if args:
            p = Path(self._strip_quotes(args[0]))
            target = p.resolve() if p.is_absolute() else (base_root / p).resolve()
        else:
            target = base_root

        if not self._path_in_event_scope(event, target):
            yield event.plain_result("路径不在允许范围内")
            return
        if not target.is_dir():
            yield event.plain_result(f"目录不存在: {target}")
            return

        try:
            entries = await asyncio.to_thread(lambda: sorted(target.iterdir(), key=lambda x: (x.is_file(), x.name.lower())))
        except OSError as e:
            yield event.plain_result(f"读取目录失败: {e}")
            return
        if not entries:
            yield event.plain_result(f"{self._display_path_for_event(event, target)} 是空目录")
            return

        lines = [f"{self._display_path_for_event(event, target)}\n"]
        for e in entries[:30]:
            if e.is_symlink():
                continue
            if e.is_dir():
                if self._skip_internal_dir(e):
                    continue
                lines.append(f"  {e.name}/")
            else:
                if self._skip_internal_file(e):
                    continue
                try:
                    size = await asyncio.to_thread(lambda p=e: p.stat().st_size)
                    lines.append(f"  {e.name} ({format_size(size)})")
                except OSError:
                    continue
        if len(entries) > 30:
            lines.append(f"\n... 共 {len(entries)} 项")

        yield event.plain_result("\n".join(lines))

    # ---------- get ----------

    @filter.command("get", alias={"获取", "下载", "发送文件"}, priority=100)
    async def cmd_get(self, event: AstrMessageEvent):
        self._stop_event(event)
        err = self._access_error(event)
        if err:
            yield event.plain_result(err)
            return
        busy = self._rebuild_busy_message()
        if busy:
            yield event.plain_result(busy)
            return

        args = self._split_command_args(event, {"get", "获取", "下载", "发送文件"}, maxsplit=1)
        if len(args) < 1:
            yield event.plain_result("用法: /get 文件名 或 /获取 文件名")
            return

        info, err = await self._resolve_indexed_file(args[0], "/get", event=event)
        if err:
            yield event.plain_result(err)
            return

        async for result in self._send_file(event, info):
            yield result

    # ---------- search ----------

    @filter.command("search", alias={"搜索", "搜索文件"}, priority=100)
    async def cmd_search(self, event: AstrMessageEvent):
        self._stop_event(event)
        err = self._access_error(event)
        if err:
            yield event.plain_result(err)
            return
        busy = self._rebuild_busy_message()
        if busy:
            yield event.plain_result(busy)
            return

        args = self._split_command_args(event, {"search", "搜索", "搜索文件"}, maxsplit=1)
        if len(args) < 1:
            yield event.plain_result("用法: /search 关键词 或 /搜索 关键词；标签搜索可用 tag:标签")
            return

        keyword = args[0].strip()
        if keyword.lower().startswith("tag:"):
            tag = keyword[4:].strip().lower()
            if not tag:
                yield event.plain_result("用法: /search tag:标签 或 /搜索 tag:标签")
                return
            results = await asyncio.to_thread(self.index.search_by_tag, tag)
        else:
            results = await asyncio.to_thread(self.index.search, keyword)

        valid = await self._valid_existing_rows(results, event)

        if not valid:
            yield event.plain_result(f"未找到包含「{keyword}」的文件")
            return

        lines = [f"搜索结果 ({len(valid)}个):\n"]
        for r in valid[:20]:
            tags = await asyncio.to_thread(self.index.list_tags, r["path"])
            tag_text = f" #{' #'.join(tags)}" if tags else ""
            note = (r.get("note") or "").strip()
            note_text = f" - {note[:40]}" if note else ""
            lines.append(f"  [{r['category']}] {r['name']} ({format_size(r['size'])}){tag_text}{note_text}")
        yield event.plain_result("\n".join(lines))

    # ---------- recent ----------

    @filter.command("recent", alias={"最近", "最近文件"}, priority=100)
    async def cmd_recent(self, event: AstrMessageEvent):
        self._stop_event(event)
        err = self._access_error(event)
        if err:
            yield event.plain_result(err)
            return
        busy = self._rebuild_busy_message()
        if busy:
            yield event.plain_result(busy)
            return

        args = self._split_command_args(event, {"recent", "最近", "最近文件"}, maxsplit=1)
        limit = 10
        if args:
            try:
                limit = max(1, min(30, int(args[0].strip())))
            except ValueError:
                yield event.plain_result("用法: /recent [数量]")
                return

        results = await asyncio.to_thread(self.index.recent, limit)
        valid = await self._valid_existing_rows(results, event)

        if not valid:
            yield event.plain_result("暂无文件记录")
            return

        lines = [f"最近文件 ({len(valid)}个):\n"]
        for r in valid:
            lines.append(f"  [{r['category']}] {r['name']} ({format_size(r['size'])})")
        yield event.plain_result("\n".join(lines))

    # ---------- tree ----------

    @filter.command("tree", alias={"目录树"}, priority=100)
    async def cmd_tree(self, event: AstrMessageEvent):
        self._stop_event(event)
        err = self._access_error(event)
        if err:
            yield event.plain_result(err)
            return

        args = self._parse_command_args(event, {"tree", "目录树"})
        base_root = self._scope_root_for_event(event)
        target = base_root
        max_depth = 2
        if len(args) > 0:
            p = Path(self._strip_quotes(args[0]))
            target = p.resolve() if p.is_absolute() else (base_root / p).resolve()
        if len(args) > 1:
            try:
                max_depth = max(1, min(5, int(args[1].strip())))
            except ValueError:
                yield event.plain_result("用法: /tree [路径] [深度] 或 /目录树 [路径] [深度]")
                return

        if not self._path_in_event_scope(event, target):
            yield event.plain_result("路径不在允许范围内")
            return
        if not target.is_dir():
            yield event.plain_result(f"目录不存在: {target}")
            return

        def build_tree():
            root_name = self._display_path_for_event(event, target)
            lines = [root_name]
            limit = 80

            def walk_dir(path: Path, depth: int):
                if len(lines) >= limit or depth > max_depth:
                    return
                try:
                    entries = sorted(path.iterdir(), key=lambda x: (x.is_file(), x.name.lower()))
                except OSError:
                    return
                for entry in entries:
                    if len(lines) >= limit:
                        return
                    if entry.is_symlink():
                        continue
                    prefix = "  " * depth
                    if entry.is_dir():
                        if self._skip_internal_dir(entry):
                            continue
                        lines.append(f"{prefix}{entry.name}/")
                        walk_dir(entry, depth + 1)
                    else:
                        if self._skip_internal_file(entry):
                            continue
                        try:
                            size = entry.stat().st_size
                        except OSError:
                            continue
                        lines.append(f"{prefix}{entry.name} ({format_size(size)})")

            walk_dir(target, 1)
            if len(lines) >= limit:
                lines.append("... 输出已截断")
            return "\n".join(lines)

        yield event.plain_result(await asyncio.to_thread(build_tree))

    # ---------- rm ----------

    @filter.command("rm", alias={"删除", "删除文件"}, priority=100)
    async def cmd_rm(self, event: AstrMessageEvent):
        self._stop_event(event)
        err = self._access_error(event, admin=True, action="删除文件")
        if err:
            yield event.plain_result(err)
            return

        args = self._split_command_args(event, {"rm", "删除", "删除文件"}, maxsplit=1)
        if len(args) < 1:
            yield event.plain_result("用法: /rm 文件名 或 /删除 文件名")
            return

        self._cleanup_pending()
        info, err = await self._resolve_indexed_file(args[0], "/rm", event=event)
        if err:
            yield event.plain_result(err)
            return

        target = Path(info["path"])
        sig = await asyncio.to_thread(lambda: (target.stat().st_size, target.stat().st_mtime_ns))
        uid = str(event.get_sender_id())
        self._delete_pending[uid] = {
            "path": target,
            "name": target.name,
            "sig": sig,
            "time": time.time(),
            "category": info["category"],
        }
        yield event.plain_result(
            f"确认删除 [{info['category']}] {target.name} ({format_size(info['size'])})？\n"
            f"{self.confirm_ttl}秒内回复「/确认删除」执行，「/取消」放弃"
        )

    @filter.command("确认删除", priority=100)
    async def cmd_confirm_delete(self, event: AstrMessageEvent):
        self._stop_event(event)
        err = self._access_error(event, admin=True, action="删除文件")
        if err:
            yield event.plain_result(err)
            return

        uid = str(event.get_sender_id())
        waiting = self._delete_pending.pop(uid, None)
        if not waiting:
            yield event.plain_result("没有待确认的删除")
            return

        if time.time() - waiting["time"] > self.confirm_ttl:
            yield event.plain_result("删除确认已超时")
            return

        target: Path = waiting["path"]
        if not self._safe_path(target):
            await asyncio.to_thread(self.index.remove, str(target))
            yield event.plain_result("索引路径不在允许范围内，已清理")
            return
        if not target.exists():
            await asyncio.to_thread(self.index.remove, str(target))
            yield event.plain_result("文件已被外部删除，已清理索引")
            return
        if not target.is_file():
            yield event.plain_result("目标不是文件，已取消")
            return
        if (target.stat().st_size, target.stat().st_mtime_ns) != waiting["sig"]:
            yield event.plain_result("文件已变化，请重新发起删除")
            return

        await asyncio.to_thread(target.unlink)
        await asyncio.to_thread(self.index.remove, str(target))
        self._log_info(f"[NAS] DELETE | {uid} | {waiting['category']}/{waiting['name']}")
        yield event.plain_result(f"已删除: {waiting['name']}")

    @filter.command("取消", priority=100)
    async def cmd_cancel(self, event: AstrMessageEvent):
        self._stop_event(event)
        if self._delete_pending.pop(str(event.get_sender_id()), None):
            yield event.plain_result("已取消删除")

    # ---------- mv ----------

    @filter.command("mv", alias={"移动", "移动文件"}, priority=100)
    async def cmd_mv(self, event: AstrMessageEvent):
        self._stop_event(event)
        err = self._access_error(event, admin=True, action="移动文件")
        if err:
            yield event.plain_result(err)
            return

        args = self._split_command_args(event, {"mv", "移动", "移动文件"}, maxsplit=1)
        if len(args) < 2:
            yield event.plain_result("用法: /mv 源文件 目标路径 或 /移动 源文件 目标路径")
            return

        info, err = await self._resolve_indexed_file(args[0], "/mv", event=event)
        if err:
            yield event.plain_result(err)
            return
        src = Path(info["path"]).resolve()
        dst_arg = self._strip_quotes(args[1])
        dst = Path(dst_arg).resolve() if Path(dst_arg).is_absolute() else (self.root / dst_arg).resolve()

        if not self._safe_path(src) or not self._safe_path(dst):
            yield event.plain_result("路径不合法")
            return
        if not src.exists() or not src.is_file():
            yield event.plain_result(f"源文件不存在或不是文件: {args[0]}")
            return
        if dst.exists() and dst.is_dir():
            dst = dst / src.name
        if dst.exists():
            yield event.plain_result(f"目标已存在: {dst.relative_to(self.root)}")
            return
        if not self._safe_path(dst):
            yield event.plain_result("目标路径不合法")
            return
        try:
            await asyncio.to_thread(dst.parent.mkdir, parents=True, exist_ok=True)
            await asyncio.to_thread(shutil.move, str(src), str(dst))
            fp = await asyncio.to_thread(file_fingerprint, str(dst))
            h = await asyncio.to_thread(file_hash, str(dst))
            new_cat = FileClassifier.get_category(dst.name)
            await asyncio.to_thread(
                self.index.move, str(src), h, str(dst), dst.name,
                fp[0], fp[1], new_cat
            )
            self._log_info(f"[NAS] MOVE | {event.get_sender_id()} | {src.name} -> {dst}")
            yield event.plain_result(f"已移动到 {dst.relative_to(self.root)}")
        except Exception as e:
            logger.error(f"[NAS] 移动失败: {e}")
            yield event.plain_result(f"移动失败: {e}")

    # ---------- rename ----------

    @filter.command("rename", alias={"重命名"}, priority=100)
    async def cmd_rename(self, event: AstrMessageEvent):
        self._stop_event(event)
        err = self._access_error(event, admin=True, action="重命名文件")
        if err:
            yield event.plain_result(err)
            return

        args = self._split_command_args(event, {"rename", "重命名"}, maxsplit=1)
        if len(args) < 2:
            yield event.plain_result("用法: /rename 源文件 新名称 或 /重命名 源文件 新名称")
            return

        raw_name = self._strip_quotes(args[1])
        if not raw_name or "/" in raw_name or "\\" in raw_name or Path(raw_name).name != raw_name:
            yield event.plain_result("新名称不能包含路径")
            return

        info, err = await self._resolve_indexed_file(args[0], "/rename", event=event)
        if err:
            yield event.plain_result(err)
            return
        src = Path(info["path"]).resolve()
        dst = src.with_name(raw_name).resolve()
        if not self._safe_path(dst):
            yield event.plain_result("新名称不合法")
            return
        if dst.exists():
            yield event.plain_result(f"目标已存在: {raw_name}")
            return

        try:
            await asyncio.to_thread(src.rename, dst)
            fp = await asyncio.to_thread(file_fingerprint, str(dst))
            h = await asyncio.to_thread(file_hash, str(dst))
            new_cat = FileClassifier.get_category(dst.name)
            await asyncio.to_thread(
                self.index.move, str(src), h, str(dst), dst.name,
                fp[0], fp[1], new_cat
            )
            self._log_info(f"[NAS] RENAME | {event.get_sender_id()} | {src.name} -> {dst.name}")
            yield event.plain_result(f"已重命名为 {dst.name}")
        except Exception as e:
            logger.error(f"[NAS] 重命名失败: {e}")
            yield event.plain_result(f"重命名失败: {e}")

    # ---------- path import ----------

    @filter.command("add", alias={"addpath", "添加", "路径添加"}, priority=100)
    async def cmd_add_path(self, event: AstrMessageEvent):
        self._stop_event(event)
        err = self._access_error(event, admin=True, action="从路径添加文件")
        if err:
            yield event.plain_result(err)
            return

        args = self._parse_command_args(event, {"add", "addpath", "添加", "路径添加"})
        if len(args) < 1:
            yield event.plain_result("用法: /add 源路径 [分类] 或 /添加 源路径 [分类]\n源路径可以是任意本机路径或 NAS 挂载路径")
            return
        raw_source = Path(self._strip_quotes(args[0])).expanduser()
        forced_category = self._strip_quotes(args[1]) if len(args) > 1 else None
        if forced_category and not self._safe_dir_name(forced_category):
            yield event.plain_result("分类名不合法")
            return
        if raw_source.is_symlink():
            yield event.plain_result("为避免目录逃逸，不能直接导入软链接路径")
            return
        source = raw_source.resolve()
        if not source.exists():
            yield event.plain_result(f"源路径不存在: {source}")
            return

        files, truncated = await asyncio.to_thread(self._collect_import_files, source)
        if not files:
            yield event.plain_result("没有可导入的文件")
            return

        yield event.plain_result(f"开始导入 {len(files)} 个文件" + ("，已按上限截断" if truncated else ""))
        counts = {"saved": 0, "duplicate": 0, "skipped": 0, "error": 0}
        samples = []
        uid = str(event.get_sender_id())
        for file_path in files:
            result = await self._archive_file(file_path, uid, forced_category)
            status = result["status"]
            counts[status if status in counts else "error"] += 1
            if status == "saved" and len(samples) < 8:
                samples.append(f"  [{result['category']}] {result['name']}")

        lines = [
            "路径导入完成",
            f"  新增: {counts['saved']}",
            f"  重复: {counts['duplicate']}",
            f"  跳过: {counts['skipped']}",
            f"  失败: {counts['error']}",
        ]
        if samples:
            lines.append("")
            lines.extend(samples)
        yield event.plain_result("\n".join(lines))

    # ---------- watch ----------

    @filter.command("watch", alias={"监控"}, priority=100)
    async def cmd_watch(self, event: AstrMessageEvent):
        self._stop_event(event)
        err = self._access_error(event, admin=True, action="管理监控目录")
        if err:
            yield event.plain_result(err)
            return

        args = self._parse_command_args(event, {"watch", "监控"})
        sub = args[0].lower() if args else "list"
        if sub in {"list", "ls", "列表"}:
            watches = await asyncio.to_thread(self.index.list_watches)
            if not watches:
                yield event.plain_result("暂无监控目录")
                return
            lines = ["监控目录:"]
            for item in watches[:30]:
                cat = item["category"] or "自动分类"
                exists = "OK" if Path(item["path"]).exists() else "缺失"
                lines.append(f"  [{exists}] {item['path']} -> {cat}")
            yield event.plain_result("\n".join(lines))
            return

        if sub in {"add", "添加"}:
            if len(args) < 2:
                yield event.plain_result("用法: /watch add 目录 [分类] 或 /监控 add 目录 [分类]")
                return
            raw_source = Path(self._strip_quotes(args[1])).expanduser()
            if raw_source.is_symlink():
                yield event.plain_result("不能监控软链接目录")
                return
            source = raw_source.resolve()
            if not source.exists() or not source.is_dir():
                yield event.plain_result(f"目录不存在: {source}")
                return
            category = self._strip_quotes(args[2]) if len(args) > 2 else ""
            if category and not self._safe_dir_name(category):
                yield event.plain_result("分类名不合法")
                return
            await asyncio.to_thread(self.index.add_watch, str(source), category)
            yield event.plain_result(f"已添加监控目录: {source}" + (f" -> {category}" if category else ""))
            return

        if sub in {"rm", "remove", "del", "删除"}:
            if len(args) < 2:
                yield event.plain_result("用法: /watch rm 目录 或 /监控 删除 目录")
                return
            target = Path(self._strip_quotes(args[1])).expanduser()
            path = target.resolve()
            removed = await asyncio.to_thread(self.index.remove_watch, str(path))
            yield event.plain_result("已移除监控目录" if removed else "未找到该监控目录")
            return

        if sub in {"run", "scan", "扫描", "执行"}:
            yield event.plain_result("正在扫描监控目录...")
            result = await self._run_watch_scan()
            self._last_watch_run = time.time()
            yield event.plain_result(
                "监控扫描完成\n"
                f"  扫描: {result['total']}\n"
                f"  新增: {result['saved']}\n"
                f"  重复: {result['duplicate']}\n"
                f"  跳过: {result['skipped']}\n"
                f"  缺失目录: {result['missing']}\n"
                f"  失败: {result['error']}"
            )
            return

        yield event.plain_result("用法: /watch list|add|rm|run 或 /监控 列表|添加|删除|扫描")

    # ---------- tag ----------

    @filter.command("tag", alias={"标签", "打标签"}, priority=100)
    async def cmd_tag(self, event: AstrMessageEvent):
        self._stop_event(event)
        err = self._access_error(event, admin=True, action="修改标签")
        if err:
            yield event.plain_result(err)
            return
        args = self._split_command_args(event, {"tag", "标签", "打标签"}, maxsplit=1)
        if len(args) < 1:
            yield event.plain_result("用法: /tag 文件名 [标签...] 或 /标签 文件名 [标签...]\n标签前加 - 表示移除，例如 /标签 a.txt 工作 -临时")
            return
        info, err = await self._resolve_indexed_file(args[0], "/tag", event=event)
        if err:
            yield event.plain_result(err)
            return

        if len(args) < 2:
            tags = await asyncio.to_thread(self.index.list_tags, info["path"])
            yield event.plain_result(f"{info['name']} 标签: " + (" ".join(f"#{t}" for t in tags) if tags else "暂无"))
            return

        raw_tags = [t.strip().lstrip("#") for t in args[1].replace("，", " ").split()]
        add_tags = []
        remove_tags = []
        for tag in raw_tags:
            if not tag or "/" in tag or "\\" in tag:
                continue
            if tag.startswith("-") and len(tag) > 1:
                remove_tags.append(tag[1:])
            else:
                add_tags.append(tag[1:] if tag.startswith("+") else tag)
        if not add_tags and not remove_tags:
            yield event.plain_result("没有有效标签")
            return

        parts = []
        if add_tags:
            saved = await asyncio.to_thread(self.index.add_tags, info["path"], add_tags)
            parts.append("添加 " + " ".join(f"#{t}" for t in saved))
        if remove_tags:
            removed = await asyncio.to_thread(self.index.remove_tags, info["path"], remove_tags)
            parts.append("移除 " + " ".join(f"#{t}" for t in removed))
        yield event.plain_result(f"{info['name']} 标签已更新: " + "；".join(parts))

    @filter.command("untag", alias={"移除标签"}, priority=100)
    async def cmd_untag(self, event: AstrMessageEvent):
        self._stop_event(event)
        err = self._access_error(event, admin=True, action="修改标签")
        if err:
            yield event.plain_result(err)
            return
        args = self._split_command_args(event, {"untag", "移除标签"}, maxsplit=1)
        if len(args) < 2:
            yield event.plain_result("用法: /untag 文件名 标签1 标签2 ... 或 /移除标签 文件名 标签1")
            return
        info, err = await self._resolve_indexed_file(args[0], "/untag", event=event)
        if err:
            yield event.plain_result(err)
            return
        tags = [t.strip().lstrip("#") for t in args[1].replace("，", " ").split()]
        removed = await asyncio.to_thread(self.index.remove_tags, info["path"], tags)
        yield event.plain_result(f"已从 {info['name']} 移除标签: " + " ".join(f"#{t}" for t in removed))

    @filter.command("tags", alias={"查看标签"}, priority=100)
    async def cmd_tags(self, event: AstrMessageEvent):
        self._stop_event(event)
        err = self._access_error(event)
        if err:
            yield event.plain_result(err)
            return
        args = self._split_command_args(event, {"tags", "查看标签"}, maxsplit=1)
        if len(args) < 1:
            yield event.plain_result("用法: /tags 文件名 或 /查看标签 文件名")
            return
        info, err = await self._resolve_indexed_file(args[0], "/tags", event=event)
        if err:
            yield event.plain_result(err)
            return
        tags = await asyncio.to_thread(self.index.list_tags, info["path"])
        yield event.plain_result(f"{info['name']} 标签: " + (" ".join(f"#{t}" for t in tags) if tags else "暂无"))

    # ---------- note ----------

    @filter.command("note", alias={"备注"}, priority=100)
    async def cmd_note(self, event: AstrMessageEvent):
        self._stop_event(event)
        args = self._split_command_args(event, {"note", "备注"}, maxsplit=1)
        if len(args) < 1:
            yield event.plain_result("用法: /note 文件 [备注内容] 或 /备注 文件 [备注内容]；内容为 - 表示清空")
            return

        if len(args) >= 2:
            err = self._access_error(event, admin=True, action="修改备注")
            if err:
                yield event.plain_result(err)
                return
        else:
            err = self._access_error(event)
            if err:
                yield event.plain_result(err)
                return

        info, err = await self._resolve_indexed_file(args[0], "/note", event=event)
        if err:
            yield event.plain_result(err)
            return

        if len(args) < 2:
            note = (info.get("note") or "").strip()
            yield event.plain_result(f"{info['name']} 备注: {note if note else '暂无'}")
            return

        note = "" if args[1].strip() == "-" else args[1].strip()
        saved = await asyncio.to_thread(self.index.set_note, info["path"], note)
        if not saved:
            yield event.plain_result("备注保存失败，索引中未找到该文件")
            return
        yield event.plain_result(f"{info['name']} 备注已" + ("清空" if not note else "更新"))

    # ---------- preview ----------

    @filter.command("preview", alias={"预览"}, priority=100)
    async def cmd_preview(self, event: AstrMessageEvent):
        self._stop_event(event)
        err = self._access_error(event)
        if err:
            yield event.plain_result(err)
            return
        args = self._split_command_args(event, {"preview", "预览"}, maxsplit=1)
        if len(args) < 1:
            yield event.plain_result("用法: /preview 文件名 或 /预览 文件名")
            return
        info, err = await self._resolve_indexed_file(args[0], "/preview", event=event)
        if err:
            yield event.plain_result(err)
            return
        path = Path(info["path"])
        tags = await asyncio.to_thread(self.index.list_tags, info["path"])
        header = (
            f"[{info['category']}] {info['name']}\n"
            f"大小: {format_size(path.stat().st_size)}\n"
            f"标签: {' '.join('#' + t for t in tags) if tags else '暂无'}"
        )
        note = (info.get("note") or "").strip()
        if note:
            header += f"\n备注: {note}"
        if self._is_image_file(path):
            preview = await asyncio.to_thread(self._image_preview_path, path)
            yield event.plain_result(header)
            yield event.chain_result([Image.fromFileSystem(str(preview))])
            return
        if self._is_text_file(path):
            try:
                text = await asyncio.to_thread(self._read_text_preview, path)
            except Exception as e:
                yield event.plain_result(f"{header}\n\n读取预览失败: {e}")
                return
            yield event.plain_result(f"{header}\n\n{text}")
            return
        yield event.plain_result(f"{header}\n\n该类型暂不支持内容预览，可用 /get 下载。")

    # ---------- dups / batch / export ----------

    @filter.command("dups", alias={"重复"}, priority=100)
    async def cmd_dups(self, event: AstrMessageEvent):
        self._stop_event(event)
        err = self._access_error(event, admin=True, action="查看重复文件")
        if err:
            yield event.plain_result(err)
            return
        args = self._split_command_args(event, {"dups", "重复"}, maxsplit=1)
        limit = 10
        if args:
            try:
                limit = max(1, min(50, int(args[0].strip())))
            except ValueError:
                yield event.plain_result("用法: /dups [数量] 或 /重复 [数量]")
                return

        groups = await asyncio.to_thread(self.index.duplicate_groups, limit)
        lines = []
        group_no = 0
        for group in groups:
            files = await self._valid_existing_rows(group["files"])
            if len(files) < 2:
                continue
            group_no += 1
            lines.append(f"重复组 {group_no}: {len(files)} 个，合计 {format_size(sum(f['size'] for f in files))}")
            for row in files[:6]:
                lines.append(f"  [{row['category']}] {row['name']} ({format_size(row['size'])})")
            if len(files) > 6:
                lines.append("  ...")
            if len(lines) > 80:
                lines.append("... 输出已截断")
                break

        if not lines:
            yield event.plain_result("未发现重复文件")
            return
        yield event.plain_result("\n".join(lines))

    @filter.command("batch", alias={"批量"}, priority=100)
    async def cmd_batch(self, event: AstrMessageEvent):
        self._stop_event(event)
        err = self._access_error(event, admin=True, action="执行批量操作")
        if err:
            yield event.plain_result(err)
            return

        args = self._parse_command_args(event, {"batch", "批量"})
        if len(args) < 2:
            yield event.plain_result(
                "用法:\n"
                "/batch 选择器 tag 标签...\n"
                "/batch 选择器 untag 标签...\n"
                "/batch 选择器 move 目标目录\n"
                "选择器: tag:标签 / category:分类 / search:关键词 / path:目录"
            )
            return

        selector = args[0]
        op = args[1].lower()
        rows, err = await self._select_files(selector)
        if err:
            yield event.plain_result(err)
            return
        if not rows:
            yield event.plain_result("没有匹配文件")
            return

        truncated = len(rows) > self.batch_max_files
        rows = rows[: self.batch_max_files]

        if op in {"tag", "addtag", "标签"}:
            tags = [
                t.strip().lstrip("#")
                for t in args[2:]
                if t.strip() and "/" not in t and "\\" not in t
            ]
            if not tags:
                yield event.plain_result("请提供要添加的标签")
                return
            for row in rows:
                await asyncio.to_thread(self.index.add_tags, row["path"], tags)
            yield event.plain_result(
                f"已为 {len(rows)} 个文件添加标签: " + " ".join(f"#{t}" for t in tags)
                + ("；结果已按上限截断" if truncated else "")
            )
            return

        if op in {"untag", "rmtag", "移除标签"}:
            tags = [
                t.strip().lstrip("#")
                for t in args[2:]
                if t.strip() and "/" not in t and "\\" not in t
            ]
            if not tags:
                yield event.plain_result("请提供要移除的标签")
                return
            for row in rows:
                await asyncio.to_thread(self.index.remove_tags, row["path"], tags)
            yield event.plain_result(
                f"已从 {len(rows)} 个文件移除标签: " + " ".join(f"#{t}" for t in tags)
                + ("；结果已按上限截断" if truncated else "")
            )
            return

        if op in {"move", "mv", "移动"}:
            if len(args) < 3:
                yield event.plain_result("用法: /batch 选择器 move 目标目录 或 /批量 选择器 移动 目标目录")
                return
            raw_target = Path(self._strip_quotes(args[2])).expanduser()
            target_dir = raw_target.resolve() if raw_target.is_absolute() else (self.root / raw_target).resolve()
            if not self._safe_path(target_dir):
                yield event.plain_result("目标目录不合法")
                return
            moved = 0
            failed = []
            for row in rows:
                ok, message = await self._move_info_to_dir(row, target_dir)
                if ok:
                    moved += 1
                elif len(failed) < 5:
                    failed.append(message)
            lines = [f"批量移动完成: {moved}/{len(rows)}"]
            if truncated:
                lines.append("结果已按上限截断")
            if failed:
                lines.append("失败示例:")
                lines.extend(f"  {item}" for item in failed)
            yield event.plain_result("\n".join(lines))
            return

        yield event.plain_result("不支持的批量操作，可用: tag / untag / move")

    @filter.command("export", alias={"导出"}, priority=100)
    async def cmd_export(self, event: AstrMessageEvent):
        self._stop_event(event)
        err = self._access_error(event, admin=True, action="导出文件")
        if err:
            yield event.plain_result(err)
            return

        args = self._parse_command_args(event, {"export", "导出"})
        if len(args) < 1:
            yield event.plain_result("用法: /export 选择器 [文件名.zip] 或 /导出 选择器 [文件名.zip]")
            return
        rows, err = await self._select_files(args[0])
        if err:
            yield event.plain_result(err)
            return
        if not rows:
            yield event.plain_result("没有匹配文件")
            return
        truncated = len(rows) > self.export_max_files
        rows = rows[: self.export_max_files]
        yield event.plain_result(f"正在打包 {len(rows)} 个文件" + ("，已按上限截断" if truncated else ""))
        try:
            zip_path = await self._create_export_zip(rows, args[1] if len(args) > 1 else None)
        except Exception as e:
            logger.error(f"[NAS] 导出失败: {e}")
            yield event.plain_result(f"导出失败: {e}")
            return
        size = zip_path.stat().st_size
        if size > self.max_size:
            yield event.plain_result(f"导出包过大: {format_size(size)}，已保存在 {zip_path}")
            return
        yield event.chain_result([File(name=zip_path.name, file=str(zip_path))])
        yield event.plain_result(f"已导出: {zip_path.name} ({format_size(size)})")

    # ---------- du / health / repair ----------

    @filter.command("status", alias={"du", "状态", "空间"}, priority=100)
    async def cmd_du(self, event: AstrMessageEvent):
        self._stop_event(event)
        err = self._access_error(event, public_read=False)
        if err:
            yield event.plain_result(err)
            return
        busy = self._rebuild_busy_message()
        if busy:
            yield event.plain_result(busy)
            return

        usage = await asyncio.to_thread(shutil.disk_usage, self.root)
        stats = await asyncio.to_thread(self.index.get_stats)
        db_size = await asyncio.to_thread(self.index.get_db_size)
        status = self._rebuild_status_text()

        lines = [
            "空间与状态",
            f"  总空间: {format_size(usage.total)}",
            f"  已用: {format_size(usage.used)}",
            f"  剩余: {format_size(usage.free)}",
            f"  数据库: {format_size(db_size)}",
            f"  索引状态: {status}",
            f"  后台检查: {self.auto_repair_interval} 分钟" if self.auto_repair_interval > 0 else "  后台检查: 关闭",
            f"  监控导入: {self.watch_interval} 分钟" if self.watch_interval > 0 else "  监控导入: 关闭",
            f"  公开目录: {self.public_read_root.relative_to(self.root)}" if self.allow_all_users else "  公开目录: 关闭",
            "",
            f"文件统计 (共 {stats['total_count']} 个, {format_size(stats['total_size'])})",
        ]
        for cat, (count, size) in stats["categories"].items():
            if count > 0:
                lines.append(f"  {cat}: {count}个 ({format_size(size)})")

        yield event.plain_result("\n".join(lines))

    @filter.command("health", priority=100)
    async def cmd_health(self, event: AstrMessageEvent):
        self._stop_event(event)
        err = self._access_error(event, public_read=False)
        if err:
            yield event.plain_result(err)
            return

        stats = await asyncio.to_thread(self.index.get_stats)
        db_size = await asyncio.to_thread(self.index.get_db_size)
        status = self._rebuild_status_text()

        yield event.plain_result(
            f"NAS 状态\n\n"
            f"文件数: {stats['total_count']}\n"
            f"数据库大小: {format_size(db_size)}\n"
            f"NAS占用: {format_size(stats['total_size'])}\n"
            f"重建状态: {status}\n"
            f"版本: v2.3.0"
        )

    @filter.command("repair", alias={"修复"}, priority=100)
    async def cmd_repair(self, event: AstrMessageEvent):
        self._stop_event(event)
        err = self._access_error(event, admin=True, action="修复索引")
        if err:
            yield event.plain_result(err)
            return
        token = self._begin_rebuild("手动修复", allow_stale=True)
        if token is None:
            busy = self._rebuild_busy_message()
            yield event.plain_result(busy or "NAS索引任务正在运行，请稍后再试")
            return

        yield event.plain_result("正在修复索引...")
        try:
            result = await asyncio.to_thread(self.index.repair_from_fs, self.root)
            yield event.plain_result(
                "索引修复完成\n"
                f"  新增: {result['added']}\n"
                f"  更新: {result['updated']}\n"
                f"  清理: {result['removed']}\n"
                f"  当前文件: {result['total']}"
            )
        except Exception as e:
            logger.error(f"[NAS] 索引修复失败: {e}")
            yield event.plain_result(f"索引修复失败: {e}")
        finally:
            self._finish_rebuild(token)

    # ---------- nas ----------

    @filter.command("nashelp", alias={"nas帮助"}, priority=100)
    async def cmd_nas(self, event: AstrMessageEvent):
        self._stop_event(event)
        yield event.plain_result(self._nas_help_text())

    def _nas_help_text(self) -> str:
        return (
            "NAS 助手 v2.3\n\n"
            "常用:\n"
            "/ls | /列表 [路径]              - 查看目录\n"
            "/get | /获取 文件               - 发送文件\n"
            "/preview | /预览 文件           - 预览图片/文本\n"
            "/search | /搜索 关键词|tag:标签  - 搜索文件/备注\n"
            "/recent | /最近 [数量]          - 最近文件\n"
            "/tags | /查看标签 文件          - 查看标签\n"
            "/note | /备注 文件 [内容]       - 查看/设置备注\n"
            "/status | /状态                 - 空间与状态\n\n"
            "管理:\n"
            "/add | /添加 源路径 [分类]     - 从任意本机/NAS路径导入\n"
            "/watch | /监控 list|add|rm|run  - 监控目录\n"
            "/dups | /重复 [数量]            - 重复文件审计\n"
            "/batch | /批量 选择器 操作      - 批量标签/移动\n"
            "/export | /导出 选择器 [zip]    - 导出ZIP\n"
            "/tag | /标签 文件 [标签...]    - 查看/添加/移除标签，-标签 表示移除\n"
            "/rm | /删除 文件               - 删除文件，需确认\n"
            "/mv | /移动 源 目标            - 移动文件\n"
            "/rename | /重命名 源 新名称    - 重命名文件\n"
            "/repair | /修复                - 修复索引"
        )

    # ---------- vacuum ----------

    @filter.command("vacuum", alias={"整理"}, priority=100)
    async def cmd_vacuum(self, event: AstrMessageEvent):
        self._stop_event(event)
        err = self._access_error(event, admin=True, action="整理数据库")
        if err:
            yield event.plain_result(err)
            return
        yield event.plain_result("正在整理数据库...")
        await asyncio.to_thread(self.index.vacuum)
        yield event.plain_result("数据库整理完成 (VACUUM + ANALYZE)")
