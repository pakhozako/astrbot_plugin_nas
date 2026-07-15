"""
NAS 助手 - AstrBot 私聊文件自动归档插件 v2.4.1
文件系统 = 真相源，SQLite = 索引缓存
"""

import asyncio
import json
import os
import shutil
import time
from pathlib import Path

from astrbot.api import AstrBotConfig, logger
from astrbot.api.event import filter, AstrMessageEvent
from astrbot.api.message_components import File, Image, Video
from astrbot.api.star import Context, Star, register

from .command_args import (
    parse_command_args,
    split_first_command_arg,
    split_command_args,
    strip_quotes,
)
from .access_control import AccessControlMixin
from .constants import INTERNAL_DIRS, PLUGIN_VERSION
from .config import NASSettings
from .file_services import FileServiceMixin
from .help_text import nas_help_text
from .utils import file_hash, file_fingerprint, format_size, FileClassifier
from .index import FileIndex
from .runtime_state import RateLimiter, RebuildState


@register("NAS 助手", "pakhozako", "私聊文件自动归档到本地磁盘/NAS", PLUGIN_VERSION)
class NASPlugin(AccessControlMixin, FileServiceMixin, Star):

    def __init__(self, context: Context, config: AstrBotConfig):
        super().__init__(context)
        settings = NASSettings.from_config(config)
        self.settings = settings
        self.root = settings.root
        self.admins = settings.admin_users
        self.admin_external_paths = settings.admin_external_paths
        self.simple_mode = settings.simple_mode
        self.allow_all_users = settings.allow_all_users
        self.allow_group_commands = settings.allow_group_commands
        self.max_size = settings.max_file_size_bytes
        self.auto_save = settings.auto_save_enabled
        self.dedup = settings.dedup_enabled
        self.confirm_ttl = settings.delete_confirm_ttl
        self.log_enabled = settings.log_enabled
        self.preview_text_chars = settings.preview_text_chars
        self.path_import_max_files = settings.path_import_max_files
        self.auto_repair_interval = settings.auto_repair_interval_minutes
        self.watch_interval = settings.watch_interval_minutes
        self.export_max_files = settings.export_max_files
        self.batch_max_files = settings.batch_max_files
        self.seven_zip_path = settings.seven_zip_path
        self.public_read_dir = settings.public_read_dir
        self.public_file_recall_minutes = settings.public_file_recall_minutes
        self.public_read_root = self._resolve_public_root(self.public_read_dir)

        self._load_categories(settings.categories_raw)
        self._delete_pending = {}
        self._recall_tasks = set()
        self._public_rate_limiter = RateLimiter(settings.public_rate_limit_per_minute)
        self._rebuild_state = RebuildState(settings.rebuild_busy_timeout_seconds, logger)
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

    def _simple_mode_error(self) -> str | None:
        if self.simple_mode:
            return "精简模式已停用此功能"
        return None

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

    def _init_dirs(self):
        for cat in FileClassifier.get_all_categories():
            (self.root / cat).mkdir(parents=True, exist_ok=True)
        for directory in INTERNAL_DIRS:
            (self.root / directory).mkdir(parents=True, exist_ok=True)
        self.public_read_root.mkdir(parents=True, exist_ok=True)

    def _cleanup_pending(self):
        now = time.time()
        expired = [uid for uid, info in self._delete_pending.items()
                   if now - info["time"] > self.confirm_ttl]
        for uid in expired:
            self._delete_pending.pop(uid, None)

    def _begin_rebuild(self, reason: str, allow_stale: bool = False) -> int | None:
        return self._rebuild_state.begin(reason, allow_stale)

    def _finish_rebuild(self, token: int | None):
        self._rebuild_state.finish(token)

    def _rebuild_busy_message(self) -> str | None:
        return self._rebuild_state.busy_message()

    def _rebuild_status_text(self) -> str:
        return self._rebuild_state.status_text()

    # ---------- 启动、后台维护 ----------

    @filter.on_astrbot_loaded()
    async def on_loaded(self):
        token = self._begin_rebuild("启动重建")
        if token is None:
            self._start_maintenance()
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
        for task in list(self._recall_tasks):
            task.cancel()
        if self._recall_tasks:
            await asyncio.gather(*self._recall_tasks, return_exceptions=True)
        self._recall_tasks.clear()

    # ---------- 路径与文件工具 ----------

    def _split_command_args(self, event: AstrMessageEvent, commands: set[str], maxsplit: int = -1) -> list[str]:
        return split_command_args(event, commands, maxsplit)

    def _parse_command_args(self, event: AstrMessageEvent, commands: set[str]) -> list[str]:
        return parse_command_args(event, commands)

    def _split_first_command_arg(
        self,
        event: AstrMessageEvent,
        commands: set[str],
        keep_unquoted: bool = False,
    ) -> list[str]:
        return split_first_command_arg(event, commands, keep_unquoted=keep_unquoted)

    @staticmethod
    def _strip_quotes(text: str) -> str:
        return strip_quotes(text)


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

    @filter.command("ls", priority=100)
    async def cmd_ls(self, event: AstrMessageEvent):
        self._stop_event(event)
        err = self._access_error(event)
        if err:
            yield event.plain_result(err)
            return
        base_root = self._scope_root_for_event(event)
        args = self._split_command_args(event, {"ls"}, maxsplit=1)
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

    @filter.command("get", priority=100)
    async def cmd_get(self, event: AstrMessageEvent):
        self._stop_event(event)
        err = self._access_error(event)
        if err:
            yield event.plain_result(err)
            return
        args = self._split_first_command_arg(event, {"get"}, keep_unquoted=True)
        if len(args) < 1:
            yield event.plain_result("用法: /get 文件名|路径|通配符")
            return

        info, err = await self._resolve_indexed_file(
            args[0],
            "/get",
            allow_glob=True,
            allow_fuzzy=True,
            event=event,
        )
        if err:
            yield event.plain_result(err)
            return

        async for result in self._send_file(event, info):
            yield result

    # ---------- search ----------

    @filter.command("search", priority=100)
    async def cmd_search(self, event: AstrMessageEvent):
        self._stop_event(event)
        err = self._access_error(event)
        if err:
            yield event.plain_result(err)
            return
        args = self._split_command_args(event, {"search"}, maxsplit=1)
        if len(args) < 1:
            yield event.plain_result("用法: /search 关键词 | /search tag:标签 | /search --recent [数量]")
            return

        keyword = args[0].strip()
        parts = keyword.split(maxsplit=1)
        if parts and parts[0].lower() == "--recent":
            limit = 10
            if len(parts) > 1:
                try:
                    limit = max(1, min(30, int(parts[1].strip())))
                except ValueError:
                    yield event.plain_result("用法: /search --recent [数量]")
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
            return

        if keyword.lower().startswith("tag:"):
            tag = keyword[4:].strip().lower()
            if not tag:
                yield event.plain_result("用法: /search tag:标签")
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

    # ---------- tree ----------

    @filter.command("tree", priority=100)
    async def cmd_tree(self, event: AstrMessageEvent):
        self._stop_event(event)
        err = self._access_error(event)
        if err:
            yield event.plain_result(err)
            return

        args = self._parse_command_args(event, {"tree"})
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
                yield event.plain_result("用法: /tree [路径] [深度]")
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

    @filter.command("rm", priority=100)
    async def cmd_rm(self, event: AstrMessageEvent):
        self._stop_event(event)
        err = self._access_error(event, admin=True, action="删除文件")
        if err:
            yield event.plain_result(err)
            return

        args = self._split_first_command_arg(event, {"rm"}, keep_unquoted=True)
        if len(args) < 1:
            yield event.plain_result("用法: /rm 文件名")
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
            f"{self.confirm_ttl}秒内回复「/confirm」执行，「/cancel」放弃"
        )

    @filter.command("confirm", priority=100)
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
        if not self._path_in_event_scope(event, target):
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

    @filter.command("cancel", priority=100)
    async def cmd_cancel(self, event: AstrMessageEvent):
        self._stop_event(event)
        if self._delete_pending.pop(str(event.get_sender_id()), None):
            yield event.plain_result("已取消删除")
            return
        yield event.plain_result("没有待取消的操作")

    # ---------- mv ----------

    @filter.command("mv", priority=100)
    async def cmd_mv(self, event: AstrMessageEvent):
        self._stop_event(event)
        err = self._access_error(event, admin=True, action="移动文件")
        if err:
            yield event.plain_result(err)
            return

        args = self._split_first_command_arg(event, {"mv"})
        if len(args) < 2:
            yield event.plain_result("用法: /mv 源文件 目标路径或新文件名")
            return

        info, err = await self._resolve_indexed_file(args[0], "/mv", event=event)
        if err:
            yield event.plain_result(err)
            return
        src = Path(info["path"]).resolve()
        dst_arg = self._strip_quotes(args[1])
        if not dst_arg:
            yield event.plain_result("目标不能为空")
            return
        raw_dst = Path(dst_arg).expanduser()
        if raw_dst.is_absolute():
            dst = raw_dst.resolve()
        elif "/" not in dst_arg and "\\" not in dst_arg and Path(dst_arg).name == dst_arg:
            if dst_arg in {".", ".."}:
                yield event.plain_result("目标文件名不合法")
                return
            root_candidate = (self.root / dst_arg).resolve()
            if root_candidate.exists() and root_candidate.is_dir():
                dst = root_candidate
            else:
                dst = src.with_name(dst_arg).resolve()
        else:
            dst = (self.root / dst_arg).resolve()

        if not self._path_in_event_scope(event, src) or not self._path_in_event_scope(
            event, dst
        ):
            yield event.plain_result("路径不合法")
            return
        if not src.exists() or not src.is_file():
            yield event.plain_result(f"源文件不存在或不是文件: {args[0]}")
            return
        if dst.exists() and dst.is_dir():
            dst = dst / src.name
        if dst.exists():
            yield event.plain_result(
                f"目标已存在: {self._display_path_for_event(event, dst)}"
            )
            return
        if not self._path_in_event_scope(event, dst):
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
            yield event.plain_result(
                f"已移动到 {self._display_path_for_event(event, dst)}"
            )
        except Exception as e:
            logger.error(f"[NAS] 移动失败: {e}")
            yield event.plain_result(f"移动失败: {e}")

    # ---------- path import ----------

    @filter.command("add", priority=100)
    async def cmd_add_path(self, event: AstrMessageEvent):
        self._stop_event(event)
        err = self._access_error(event, admin=True, action="从路径添加文件")
        if err:
            yield event.plain_result(err)
            return

        args = self._parse_command_args(event, {"add"})
        if len(args) < 1:
            yield event.plain_result("用法: /add 源路径 [分类]\n源路径可以是任意本机路径或 NAS 挂载路径")
            return
        raw_source = Path(self._strip_quotes(args[0])).expanduser()
        forced_category = self._strip_quotes(args[1]) if len(args) > 1 else None
        if forced_category and not self._safe_dir_name(forced_category):
            yield event.plain_result("分类名不合法")
            return
        if raw_source.is_symlink() and not self._admin_external_access(event):
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

    @filter.command("watch", priority=100)
    async def cmd_watch(self, event: AstrMessageEvent):
        self._stop_event(event)
        err = self._access_error(event, admin=True, action="管理监控目录")
        if err:
            yield event.plain_result(err)
            return

        args = self._parse_command_args(event, {"watch"})
        sub = args[0].lower() if args else "list"
        if sub in {"list", "ls"}:
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

        if sub == "add":
            if len(args) < 2:
                yield event.plain_result("用法: /watch add 目录 [分类]")
                return
            raw_source = Path(self._strip_quotes(args[1])).expanduser()
            if raw_source.is_symlink() and not self._admin_external_access(event):
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

        if sub == "rm":
            if len(args) < 2:
                yield event.plain_result("用法: /watch rm 目录")
                return
            target = Path(self._strip_quotes(args[1])).expanduser()
            path = target.resolve()
            removed = await asyncio.to_thread(self.index.remove_watch, str(path))
            yield event.plain_result("已移除监控目录" if removed else "未找到该监控目录")
            return

        if sub == "run":
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

        yield event.plain_result("用法: /watch list|add|rm|run")

    # ---------- tag ----------

    @filter.command("tag", priority=100)
    async def cmd_tag(self, event: AstrMessageEvent):
        self._stop_event(event)
        if error := self._simple_mode_error():
            yield event.plain_result(error)
            return
        args = self._split_first_command_arg(event, {"tag"})
        if len(args) < 1:
            yield event.plain_result("用法: /tag 文件名 [标签...]\n标签前加 - 表示移除，例如 /tag a.txt work -temp")
            return
        err = self._access_error(event, admin=len(args) >= 2, action="修改标签")
        if err:
            yield event.plain_result(err)
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

    # ---------- note ----------

    @filter.command("note", priority=100)
    async def cmd_note(self, event: AstrMessageEvent):
        self._stop_event(event)
        if error := self._simple_mode_error():
            yield event.plain_result(error)
            return
        args = self._split_first_command_arg(event, {"note"})
        if len(args) < 1:
            yield event.plain_result("用法: /note 文件 [备注内容]；内容为 - 表示清空")
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

    @filter.command("preview", priority=100)
    async def cmd_preview(self, event: AstrMessageEvent):
        self._stop_event(event)
        err = self._access_error(event)
        if err:
            yield event.plain_result(err)
            return
        args = self._split_first_command_arg(event, {"preview"}, keep_unquoted=True)
        if len(args) < 1:
            yield event.plain_result("用法: /preview 文件名")
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

    @filter.command("dups", priority=100)
    async def cmd_dups(self, event: AstrMessageEvent):
        self._stop_event(event)
        if error := self._simple_mode_error():
            yield event.plain_result(error)
            return
        err = self._access_error(event, admin=True, action="查看重复文件")
        if err:
            yield event.plain_result(err)
            return
        args = self._split_command_args(event, {"dups"}, maxsplit=1)
        limit = 10
        if args:
            try:
                limit = max(1, min(50, int(args[0].strip())))
            except ValueError:
                yield event.plain_result("用法: /dups [数量]")
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

    @filter.command("batch", priority=100)
    async def cmd_batch(self, event: AstrMessageEvent):
        self._stop_event(event)
        if error := self._simple_mode_error():
            yield event.plain_result(error)
            return
        err = self._access_error(event, admin=True, action="执行批量操作")
        if err:
            yield event.plain_result(err)
            return

        args = self._parse_command_args(event, {"batch"})
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

        if op in {"tag", "addtag"}:
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

        if op in {"untag", "rmtag"}:
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

        if op in {"move", "mv"}:
            if len(args) < 3:
                yield event.plain_result("用法: /batch 选择器 move 目标目录")
                return
            raw_target = Path(self._strip_quotes(args[2])).expanduser()
            target_dir = raw_target.resolve() if raw_target.is_absolute() else (self.root / raw_target).resolve()
            if not self._path_in_event_scope(event, target_dir):
                yield event.plain_result("目标目录不合法")
                return
            moved = 0
            failed = []
            for row in rows:
                ok, message = await self._move_info_to_dir(row, target_dir, event)
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

    @filter.command("export", priority=100)
    async def cmd_export(self, event: AstrMessageEvent):
        self._stop_event(event)
        if error := self._simple_mode_error():
            yield event.plain_result(error)
            return
        err = self._access_error(event, admin=True, action="导出文件")
        if err:
            yield event.plain_result(err)
            return

        args = self._parse_command_args(event, {"export"})
        if len(args) < 1:
            yield event.plain_result("用法: /export 选择器 [文件名.zip]")
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

    # ---------- status / repair ----------

    @filter.command("status", priority=100)
    async def cmd_status(self, event: AstrMessageEvent):
        self._stop_event(event)
        err = self._access_error(event, public_read=False)
        if err:
            yield event.plain_result(err)
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
            f"  版本: {PLUGIN_VERSION}",
            f"  后台检查: {self.auto_repair_interval} 分钟" if self.auto_repair_interval > 0 else "  后台检查: 关闭",
            f"  监控导入: {self.watch_interval} 分钟" if self.watch_interval > 0 else "  监控导入: 关闭",
            f"  公开目录: {self.public_read_root.relative_to(self.root)}" if self.allow_all_users else "  公开目录: 关闭",
            f"  普通用户文件撤回: {self.public_file_recall_minutes} 分钟" if self.public_file_recall_minutes > 0 else "  普通用户文件撤回: 关闭",
            "",
            f"文件统计 (共 {stats['total_count']} 个, {format_size(stats['total_size'])})",
        ]
        for cat, (count, size) in stats["categories"].items():
            if count > 0:
                lines.append(f"  {cat}: {count}个 ({format_size(size)})")

        yield event.plain_result("\n".join(lines))

    @filter.command("repair", priority=100)
    async def cmd_repair(self, event: AstrMessageEvent):
        self._stop_event(event)
        err = self._access_error(event, admin=True, action="修复索引")
        if err:
            yield event.plain_result(err)
            return
        args = self._parse_command_args(event, {"repair"})
        if args:
            sub = args[0].lower()
            if sub == "vacuum":
                busy = self._rebuild_busy_message()
                if busy:
                    yield event.plain_result(busy)
                    return
                yield event.plain_result("正在整理数据库...")
                await asyncio.to_thread(self.index.vacuum)
                yield event.plain_result("数据库整理完成 (VACUUM + ANALYZE)")
                return
            yield event.plain_result("用法: /repair [vacuum]")
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

    @filter.command("nashelp", priority=100)
    async def cmd_nas(self, event: AstrMessageEvent):
        self._stop_event(event)
        yield event.plain_result(self._nas_help_text())

    def _nas_help_text(self) -> str:
        return nas_help_text(simple_mode=self.simple_mode)
