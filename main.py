"""
NAS 助手 - AstrBot 私聊文件自动归档插件 v2.2.0
文件系统 = 真相源，SQLite = 索引缓存
"""

import asyncio
import json
import os
import shlex
import shutil
import time
from pathlib import Path

from astrbot.api import AstrBotConfig, logger
from astrbot.api.event import filter, AstrMessageEvent
from astrbot.api.message_components import File, Image, Video
from astrbot.api.star import Context, Star, register

from .utils import file_hash, file_fingerprint, format_size, FileClassifier
from .index import FileIndex


@register("NAS 助手", "pakhozako", "私聊文件自动归档到本地磁盘/NAS", "v2.2.0")
class NASPlugin(Star):

    def __init__(self, context: Context, config: AstrBotConfig):
        super().__init__(context)
        cfg = config or {}
        save_root = cfg.get("save_root") or str(Path("data/plugin_data/astrbot_plugin_nas"))
        self.root = Path(save_root).resolve()
        self.admins = set(str(u) for u in cfg.get("admin_users", []))
        self.allow_group_commands = bool(cfg.get("allow_group_commands", False))
        self.max_size = int(cfg.get("max_file_size", 2048)) * 1024 * 1024
        self.auto_save = bool(cfg.get("auto_save_enabled", True))
        self.dedup = bool(cfg.get("dedup_enabled", True))
        self.confirm_ttl = int(cfg.get("delete_confirm_ttl", 120))
        self.log_enabled = bool(cfg.get("log_enabled", True))
        self.preview_text_chars = int(cfg.get("preview_text_chars", 1200))
        self.path_import_max_files = int(cfg.get("path_import_max_files", 2000))
        self.auto_repair_interval = int(cfg.get("auto_repair_interval_minutes", 0))

        self._load_categories(str(cfg.get("categories", "") or ""))
        self._delete_pending = {}
        self._rebuilding = False
        self._file_lock = asyncio.Lock()
        self._maintenance_task = None

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

    # ---------- 安全与权限 ----------

    def _is_allowed(self, uid: str) -> bool:
        return self._is_admin(uid)

    def _is_admin(self, uid: str) -> bool:
        return str(uid) in self.admins

    def _is_group_message(self, event: AstrMessageEvent) -> bool:
        obj = getattr(event, "message_obj", None)
        group_id = getattr(obj, "group_id", None)
        if group_id:
            return True
        msg_type = getattr(obj, "type", None)
        return "group" in str(msg_type).lower()

    def _access_error(self, event: AstrMessageEvent, admin: bool = False, action: str = "执行此命令") -> str | None:
        if self._is_group_message(event) and not self.allow_group_commands:
            return "群聊命令未启用，请在私聊中使用 NAS 助手"
        uid = str(event.get_sender_id())
        if admin:
            if not self._is_admin(uid):
                return f"仅管理员可{action}"
            return None
        if not self._is_allowed(uid):
            return "没有权限使用 NAS 助手"
        return None

    def _safe_path(self, path: Path) -> bool:
        try:
            path.resolve().relative_to(self.root)
            return True
        except ValueError:
            return False

    def _cleanup_pending(self):
        now = time.time()
        expired = [uid for uid, info in self._delete_pending.items()
                   if now - info["time"] > self.confirm_ttl]
        for uid in expired:
            self._delete_pending.pop(uid, None)

    # ---------- 启动、后台维护 ----------

    @filter.on_astrbot_loaded()
    async def on_loaded(self):
        if self._rebuilding:
            return
        self._rebuilding = True
        try:
            count = await asyncio.to_thread(self.index.rebuild_from_fs, self.root)
            logger.info(f"[NAS] 索引重建完成: {count} 个文件")
        except Exception as e:
            logger.error(f"[NAS] 索引重建失败: {e}，将从空索引开始")
        finally:
            self._rebuilding = False
        self._start_maintenance()

    def _start_maintenance(self):
        if self.auto_repair_interval <= 0 or self._maintenance_task:
            return
        self._maintenance_task = asyncio.create_task(self._maintenance_loop())
        logger.info(f"[NAS] 后台一致性检查已启用: 每 {self.auto_repair_interval} 分钟")

    async def _maintenance_loop(self):
        interval = max(1, self.auto_repair_interval) * 60
        while True:
            await asyncio.sleep(interval)
            if self._rebuilding:
                continue
            self._rebuilding = True
            try:
                result = await asyncio.to_thread(self.index.repair_from_fs, self.root)
                self._log_info(
                    "[NAS] 后台一致性检查完成 | "
                    f"新增 {result['added']} 更新 {result['updated']} 清理 {result['removed']}"
                )
            except asyncio.CancelledError:
                raise
            except Exception as e:
                logger.warning(f"[NAS] 后台一致性检查失败: {e}")
            finally:
                self._rebuilding = False

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
        }

    def _multiple_match_message(self, results: list[dict], command: str) -> str:
        locations = "\n".join(f"  [{r['category']}] {r['name']}" for r in results[:8])
        suffix = "\n..." if len(results) > 8 else ""
        return f"找到多个文件:\n{locations}{suffix}\n请使用 {command} 分类/文件名 或完整相对路径 指定"

    async def _resolve_indexed_file(self, query: str, command: str, allow_search: bool = True) -> tuple[dict | None, str | None]:
        name = self._strip_quotes(query)
        if not name:
            return None, "文件名不能为空"

        if os.path.isabs(name):
            file_path = Path(name).resolve()
            if not self._safe_path(file_path):
                return None, "路径不在允许范围内"
            if not file_path.exists():
                return None, f"文件不存在: {name}"
            if not file_path.is_file():
                return None, f"不是文件: {name}"
            info = await asyncio.to_thread(self.index.find_by_path, str(file_path))
            return info or self._info_from_path(file_path), None

        rel_path = (self.root / name).resolve()
        if ("/" in name or "\\" in name) and self._safe_path(rel_path) and rel_path.exists():
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
                or Path(r["path"]).resolve() == (self.root / normalized).resolve()
            ]
        else:
            results = await asyncio.to_thread(self.index.find_by_name, name)
            if not results and allow_search:
                results = await asyncio.to_thread(self.index.search, name)

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
        source = source.resolve()
        if source.is_symlink():
            return {"status": "skipped", "reason": "跳过软链接", "source": str(source)}
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
        return bool(rel.parts) and rel.parts[0] == ".previews"

    def _skip_internal_file(self, path: Path) -> bool:
        try:
            rel = path.resolve().relative_to(self.root)
        except ValueError:
            return False
        if bool(rel.parts) and rel.parts[0] == ".previews":
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

    @filter.regex(r"^/ls(\s|$)|^/列表(\s|$)|^/查看(\s|$)")
    async def cmd_ls(self, event: AstrMessageEvent):
        err = self._access_error(event)
        if err:
            yield event.plain_result(err)
            return
        if self._rebuilding:
            yield event.plain_result("NAS索引重建中，请稍后再试")
            return

        args = event.message_str.strip().split(maxsplit=1)
        if len(args) > 1:
            p = Path(self._strip_quotes(args[1]))
            target = p.resolve() if p.is_absolute() else (self.root / p).resolve()
        else:
            target = self.root

        if not self._safe_path(target):
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
            yield event.plain_result(f"{target.relative_to(self.root) or '/'} 是空目录")
            return

        lines = [f"{target.relative_to(self.root) or '/'}\n"]
        for e in entries[:30]:
            if e.is_symlink():
                continue
            if e.is_dir():
                lines.append(f"  {e.name}/")
            else:
                try:
                    size = await asyncio.to_thread(lambda p=e: p.stat().st_size)
                    lines.append(f"  {e.name} ({format_size(size)})")
                except OSError:
                    continue
        if len(entries) > 30:
            lines.append(f"\n... 共 {len(entries)} 项")

        yield event.plain_result("\n".join(lines))

    # ---------- get ----------

    @filter.regex(r"^/get(\s|$)|^/获取(\s|$)|^/下载(\s|$)|^/发送文件(\s|$)")
    async def cmd_get(self, event: AstrMessageEvent):
        err = self._access_error(event)
        if err:
            yield event.plain_result(err)
            return
        if self._rebuilding:
            yield event.plain_result("NAS索引重建中，请稍后再试")
            return

        args = event.message_str.strip().split(maxsplit=1)
        if len(args) < 2:
            yield event.plain_result("用法: /get 文件名 或 /获取 文件名")
            return

        info, err = await self._resolve_indexed_file(args[1], "/get")
        if err:
            yield event.plain_result(err)
            return

        async for result in self._send_file(event, info):
            yield result

    # ---------- search ----------

    @filter.regex(r"^/search(\s|$)|^/搜索(\s|$)|^/搜索文件(\s|$)")
    async def cmd_search(self, event: AstrMessageEvent):
        err = self._access_error(event)
        if err:
            yield event.plain_result(err)
            return
        if self._rebuilding:
            yield event.plain_result("NAS索引重建中，请稍后再试")
            return

        args = event.message_str.strip().split(maxsplit=1)
        if len(args) < 2:
            yield event.plain_result("用法: /search 关键词 或 /搜索 关键词；标签搜索可用 tag:标签")
            return

        keyword = args[1].strip()
        if keyword.lower().startswith("tag:"):
            tag = keyword[4:].strip().lower()
            if not tag:
                yield event.plain_result("用法: /search tag:标签 或 /搜索 tag:标签")
                return
            results = await asyncio.to_thread(self.index.search_by_tag, tag)
        else:
            results = await asyncio.to_thread(self.index.search, keyword)

        valid = []
        stale = []
        for r in results:
            path = Path(r["path"])
            if not self._safe_path(path) or not path.exists() or not path.is_file():
                stale.append(r)
            else:
                valid.append(r)

        for s in stale:
            await asyncio.to_thread(self.index.remove, s["path"])
        if stale:
            self._log_info(f"[NAS] 搜索懒清理: {len(stale)} 条脏记录")

        if not valid:
            yield event.plain_result(f"未找到包含「{keyword}」的文件")
            return

        lines = [f"搜索结果 ({len(valid)}个):\n"]
        for r in valid[:20]:
            tags = await asyncio.to_thread(self.index.list_tags, r["path"])
            tag_text = f" #{' #'.join(tags)}" if tags else ""
            lines.append(f"  [{r['category']}] {r['name']} ({format_size(r['size'])}){tag_text}")
        yield event.plain_result("\n".join(lines))

    # ---------- recent ----------

    @filter.regex(r"^/recent(\s|$)|^/最近(\s|$)|^/最近文件(\s|$)")
    async def cmd_recent(self, event: AstrMessageEvent):
        err = self._access_error(event)
        if err:
            yield event.plain_result(err)
            return
        if self._rebuilding:
            yield event.plain_result("NAS索引重建中，请稍后再试")
            return

        args = event.message_str.strip().split(maxsplit=1)
        limit = 10
        if len(args) > 1:
            try:
                limit = max(1, min(30, int(args[1].strip())))
            except ValueError:
                yield event.plain_result("用法: /recent [数量]")
                return

        results = await asyncio.to_thread(self.index.recent, limit)
        valid = []
        for r in results:
            path = Path(r["path"])
            if not self._safe_path(path) or not path.exists() or not path.is_file():
                await asyncio.to_thread(self.index.remove, r["path"])
            else:
                valid.append(r)

        if not valid:
            yield event.plain_result("暂无文件记录")
            return

        lines = [f"最近文件 ({len(valid)}个):\n"]
        for r in valid:
            lines.append(f"  [{r['category']}] {r['name']} ({format_size(r['size'])})")
        yield event.plain_result("\n".join(lines))

    # ---------- tree ----------

    @filter.regex(r"^/tree(\s|$)|^/目录树(\s|$)")
    async def cmd_tree(self, event: AstrMessageEvent):
        err = self._access_error(event)
        if err:
            yield event.plain_result(err)
            return

        args = event.message_str.strip().split(maxsplit=2)
        target = self.root
        max_depth = 2
        if len(args) > 1:
            p = Path(self._strip_quotes(args[1]))
            target = p.resolve() if p.is_absolute() else (self.root / p).resolve()
        if len(args) > 2:
            try:
                max_depth = max(1, min(5, int(args[2].strip())))
            except ValueError:
                yield event.plain_result("用法: /tree [路径] [深度] 或 /目录树 [路径] [深度]")
                return

        if not self._safe_path(target):
            yield event.plain_result("路径不在允许范围内")
            return
        if not target.is_dir():
            yield event.plain_result(f"目录不存在: {target}")
            return

        def build_tree():
            root_name = str(target.relative_to(self.root) or "/")
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
                        lines.append(f"{prefix}{entry.name}/")
                        walk_dir(entry, depth + 1)
                    else:
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

    @filter.regex(r"^/rm(\s|$)|^/删除(\s|$)|^/删除文件(\s|$)")
    async def cmd_rm(self, event: AstrMessageEvent):
        err = self._access_error(event, admin=True, action="删除文件")
        if err:
            yield event.plain_result(err)
            return

        args = event.message_str.strip().split(maxsplit=1)
        if len(args) < 2:
            yield event.plain_result("用法: /rm 文件名 或 /删除 文件名")
            return

        self._cleanup_pending()
        info, err = await self._resolve_indexed_file(args[1], "/rm")
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

    @filter.regex(r"^/确认删除$")
    async def cmd_confirm_delete(self, event: AstrMessageEvent):
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

    @filter.regex(r"^/取消$")
    async def cmd_cancel(self, event: AstrMessageEvent):
        if self._delete_pending.pop(str(event.get_sender_id()), None):
            yield event.plain_result("已取消删除")

    # ---------- mv ----------

    @filter.regex(r"^/mv(\s|$)|^/移动(\s|$)|^/移动文件(\s|$)")
    async def cmd_mv(self, event: AstrMessageEvent):
        err = self._access_error(event, admin=True, action="移动文件")
        if err:
            yield event.plain_result(err)
            return

        args = event.message_str.strip().split(maxsplit=2)
        if len(args) < 3:
            yield event.plain_result("用法: /mv 源文件 目标路径 或 /移动 源文件 目标路径")
            return

        info, err = await self._resolve_indexed_file(args[1], "/mv")
        if err:
            yield event.plain_result(err)
            return
        src = Path(info["path"]).resolve()
        dst_arg = self._strip_quotes(args[2])
        dst = Path(dst_arg).resolve() if Path(dst_arg).is_absolute() else (self.root / dst_arg).resolve()

        if not self._safe_path(src) or not self._safe_path(dst):
            yield event.plain_result("路径不合法")
            return
        if not src.exists() or not src.is_file():
            yield event.plain_result(f"源文件不存在或不是文件: {args[1]}")
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

    @filter.regex(r"^/rename(\s|$)|^/重命名(\s|$)")
    async def cmd_rename(self, event: AstrMessageEvent):
        err = self._access_error(event, admin=True, action="重命名文件")
        if err:
            yield event.plain_result(err)
            return

        args = event.message_str.strip().split(maxsplit=2)
        if len(args) < 3:
            yield event.plain_result("用法: /rename 源文件 新名称 或 /重命名 源文件 新名称")
            return

        raw_name = self._strip_quotes(args[2])
        if not raw_name or "/" in raw_name or "\\" in raw_name or Path(raw_name).name != raw_name:
            yield event.plain_result("新名称不能包含路径")
            return

        info, err = await self._resolve_indexed_file(args[1], "/rename")
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

    @filter.regex(r"^/add(\s|$)|^/addpath(\s|$)|^/添加(\s|$)|^/路径添加(\s|$)")
    async def cmd_add_path(self, event: AstrMessageEvent):
        err = self._access_error(event, admin=True, action="从路径添加文件")
        if err:
            yield event.plain_result(err)
            return

        args = self._parse_args(event.message_str.strip())
        if len(args) < 2:
            yield event.plain_result("用法: /add 源路径 [分类] 或 /添加 源路径 [分类]\n源路径可以是任意本机路径或 NAS 挂载路径")
            return
        source = Path(self._strip_quotes(args[1])).expanduser().resolve()
        forced_category = self._strip_quotes(args[2]) if len(args) > 2 else None
        if forced_category and not self._safe_dir_name(forced_category):
            yield event.plain_result("分类名不合法")
            return
        if not source.exists():
            yield event.plain_result(f"源路径不存在: {source}")
            return
        if source.is_symlink():
            yield event.plain_result("为避免目录逃逸，不能直接导入软链接路径")
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

    # ---------- tag ----------

    @filter.regex(r"^/tag(\s|$)|^/标签(\s|$)|^/打标签(\s|$)")
    async def cmd_tag(self, event: AstrMessageEvent):
        err = self._access_error(event, admin=True, action="修改标签")
        if err:
            yield event.plain_result(err)
            return
        args = event.message_str.strip().split(maxsplit=2)
        if len(args) < 2:
            yield event.plain_result("用法: /tag 文件名 [标签...] 或 /标签 文件名 [标签...]\n标签前加 - 表示移除，例如 /标签 a.txt 工作 -临时")
            return
        info, err = await self._resolve_indexed_file(args[1], "/tag")
        if err:
            yield event.plain_result(err)
            return

        if len(args) < 3:
            tags = await asyncio.to_thread(self.index.list_tags, info["path"])
            yield event.plain_result(f"{info['name']} 标签: " + (" ".join(f"#{t}" for t in tags) if tags else "暂无"))
            return

        raw_tags = [t.strip().lstrip("#") for t in args[2].replace("，", " ").split()]
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

    @filter.regex(r"^/untag(\s|$)|^/移除标签(\s|$)")
    async def cmd_untag(self, event: AstrMessageEvent):
        err = self._access_error(event, admin=True, action="修改标签")
        if err:
            yield event.plain_result(err)
            return
        args = event.message_str.strip().split(maxsplit=2)
        if len(args) < 3:
            yield event.plain_result("用法: /untag 文件名 标签1 标签2 ... 或 /移除标签 文件名 标签1")
            return
        info, err = await self._resolve_indexed_file(args[1], "/untag")
        if err:
            yield event.plain_result(err)
            return
        tags = [t.strip().lstrip("#") for t in args[2].replace("，", " ").split()]
        removed = await asyncio.to_thread(self.index.remove_tags, info["path"], tags)
        yield event.plain_result(f"已从 {info['name']} 移除标签: " + " ".join(f"#{t}" for t in removed))

    @filter.regex(r"^/tags(\s|$)|^/查看标签(\s|$)")
    async def cmd_tags(self, event: AstrMessageEvent):
        err = self._access_error(event)
        if err:
            yield event.plain_result(err)
            return
        args = event.message_str.strip().split(maxsplit=1)
        if len(args) < 2:
            yield event.plain_result("用法: /tags 文件名 或 /查看标签 文件名")
            return
        info, err = await self._resolve_indexed_file(args[1], "/tags")
        if err:
            yield event.plain_result(err)
            return
        tags = await asyncio.to_thread(self.index.list_tags, info["path"])
        yield event.plain_result(f"{info['name']} 标签: " + (" ".join(f"#{t}" for t in tags) if tags else "暂无"))

    # ---------- preview ----------

    @filter.regex(r"^/preview(\s|$)|^/预览(\s|$)")
    async def cmd_preview(self, event: AstrMessageEvent):
        err = self._access_error(event)
        if err:
            yield event.plain_result(err)
            return
        args = event.message_str.strip().split(maxsplit=1)
        if len(args) < 2:
            yield event.plain_result("用法: /preview 文件名 或 /预览 文件名")
            return
        info, err = await self._resolve_indexed_file(args[1], "/preview")
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

    # ---------- du / health / repair ----------

    @filter.regex(r"^/status(\s|$)|^/du(\s|$)|^/状态(\s|$)|^/空间(\s|$)")
    async def cmd_du(self, event: AstrMessageEvent):
        err = self._access_error(event)
        if err:
            yield event.plain_result(err)
            return
        if self._rebuilding:
            yield event.plain_result("NAS索引重建中，请稍后再试")
            return

        usage = await asyncio.to_thread(shutil.disk_usage, self.root)
        stats = await asyncio.to_thread(self.index.get_stats)
        db_size = await asyncio.to_thread(self.index.get_db_size)
        status = "重建中" if self._rebuilding else "正常"

        lines = [
            "空间与状态",
            f"  总空间: {format_size(usage.total)}",
            f"  已用: {format_size(usage.used)}",
            f"  剩余: {format_size(usage.free)}",
            f"  数据库: {format_size(db_size)}",
            f"  索引状态: {status}",
            f"  后台检查: {self.auto_repair_interval} 分钟" if self.auto_repair_interval > 0 else "  后台检查: 关闭",
            "",
            f"文件统计 (共 {stats['total_count']} 个, {format_size(stats['total_size'])})",
        ]
        for cat, (count, size) in stats["categories"].items():
            if count > 0:
                lines.append(f"  {cat}: {count}个 ({format_size(size)})")

        yield event.plain_result("\n".join(lines))

    @filter.regex(r"^/health$")
    async def cmd_health(self, event: AstrMessageEvent):
        err = self._access_error(event)
        if err:
            yield event.plain_result(err)
            return

        stats = await asyncio.to_thread(self.index.get_stats)
        db_size = await asyncio.to_thread(self.index.get_db_size)
        status = "重建中" if self._rebuilding else "正常"

        yield event.plain_result(
            f"NAS 状态\n\n"
            f"文件数: {stats['total_count']}\n"
            f"数据库大小: {format_size(db_size)}\n"
            f"NAS占用: {format_size(stats['total_size'])}\n"
            f"重建状态: {status}\n"
            f"版本: v2.2.0"
        )

    @filter.regex(r"^/repair$|^/修复$")
    async def cmd_repair(self, event: AstrMessageEvent):
        err = self._access_error(event, admin=True, action="修复索引")
        if err:
            yield event.plain_result(err)
            return
        if self._rebuilding:
            yield event.plain_result("NAS索引重建中，请稍后再试")
            return

        self._rebuilding = True
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
            self._rebuilding = False

    # ---------- nas ----------

    @filter.regex(r"^/nashelp(\s|$)|^/nas帮助(\s|$)")
    async def cmd_nas(self, event: AstrMessageEvent):
        yield event.plain_result(self._nas_help_text())

    def _nas_help_text(self) -> str:
        return (
            "NAS 助手 v2.2\n\n"
            "常用:\n"
            "/ls | /列表 [路径]              - 查看目录\n"
            "/get | /获取 文件               - 发送文件\n"
            "/preview | /预览 文件           - 预览图片/文本\n"
            "/search | /搜索 关键词|tag:标签  - 搜索文件\n"
            "/recent | /最近 [数量]          - 最近文件\n"
            "/status | /状态                 - 空间与状态\n\n"
            "管理:\n"
            "/add | /添加 源路径 [分类]     - 从任意本机/NAS路径导入\n"
            "/tag | /标签 文件 [标签...]    - 查看/添加/移除标签，-标签 表示移除\n"
            "/rm | /删除 文件               - 删除文件，需确认\n"
            "/mv | /移动 源 目标            - 移动文件\n"
            "/rename | /重命名 源 新名称    - 重命名文件\n"
            "/repair | /修复                - 修复索引"
        )

    # ---------- vacuum ----------

    @filter.regex(r"^/vacuum$|^/整理$")
    async def cmd_vacuum(self, event: AstrMessageEvent):
        err = self._access_error(event, admin=True, action="整理数据库")
        if err:
            yield event.plain_result(err)
            return
        yield event.plain_result("正在整理数据库...")
        await asyncio.to_thread(self.index.vacuum)
        yield event.plain_result("数据库整理完成 (VACUUM + ANALYZE)")
