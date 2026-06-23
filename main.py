"""
NAS 助手 - AstrBot 私聊文件自动归档插件 v2.1.0
文件系统 = 真相源，SQLite = 索引缓存
"""

import os
import shutil
import time
import asyncio
import json
from pathlib import Path

from astrbot.api import AstrBotConfig, logger
from astrbot.api.event import filter, AstrMessageEvent
from astrbot.api.message_components import *
from astrbot.api.star import Context, Star, register

from .utils import file_hash, file_fingerprint, format_size, FileClassifier
from .index import FileIndex


@register("NAS 助手", "pakhozako", "私聊文件自动归档到本地磁盘/NAS", "v2.1.0")
class NASPlugin(Star):

    def __init__(self, context: Context, config: AstrBotConfig):
        super().__init__(context)
        cfg = config or {}
        save_root = cfg.get("save_root") or str(Path("data/plugin_data/astrbot_plugin_nas"))
        self.root = Path(save_root).resolve()
        self.allowed = set(str(u) for u in cfg.get("allowed_users", []))
        self.admins = set(str(u) for u in cfg.get("admin_users", []))
        self.max_size = int(cfg.get("max_file_size", 2048)) * 1024 * 1024
        self.auto_save = bool(cfg.get("auto_save_enabled", True))
        self.dedup = bool(cfg.get("dedup_enabled", True))
        self.confirm_ttl = int(cfg.get("delete_confirm_ttl", 120))
        self.log_enabled = bool(cfg.get("log_enabled", True))
        self._load_categories(str(cfg.get("categories", "") or ""))
        self._delete_pending = {}
        self._rebuilding = False

        self._init_dirs()
        self.index = FileIndex(str(self.root / "files.db"))
        logger.info(f"[NAS] 根目录: {self.root} | 自动保存: {self.auto_save}")

    def _log_info(self, message: str):
        if self.log_enabled:
            logger.info(message)

    def _load_categories(self, raw: str):
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
                normalized[category] = {str(ext).lower().lstrip(".") for ext in extensions}
            FileClassifier.CATEGORIES = normalized
        except Exception as e:
            logger.warning(f"[NAS] 自定义分类规则无效，使用默认分类: {e}")

    @staticmethod
    def _safe_dir_name(name: str) -> bool:
        clean = name.strip()
        return bool(clean) and clean == name and Path(clean).name == clean and "/" not in clean and "\\" not in clean and clean not in {".", ".."}

    def _init_dirs(self):
        for cat in FileClassifier.get_all_categories():
            (self.root / cat).mkdir(parents=True, exist_ok=True)

    # ---------- 安全工具 ----------

    def _is_allowed(self, uid: str) -> bool:
        return not self.allowed or uid in self.allowed

    def _is_admin(self, uid: str) -> bool:
        return uid in self.admins

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

    # ---------- 启动时重建索引 ----------

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

    # ---------- 自动接收 ----------

    @filter.event_message_type(filter.EventMessageType.PRIVATE_MESSAGE, priority=100)
    async def on_file_received(self, event: AstrMessageEvent):
        if not self.auto_save:
            return
        uid = event.get_sender_id()
        if not self._is_allowed(uid):
            return

        for comp in event.get_messages():
            if not isinstance(comp, (File, Image, Video)):
                continue

            source = None
            if hasattr(comp, 'get_file'):
                try:
                    source = await comp.get_file()
                except Exception as e:
                    logger.warning(f"[NAS] get_file 失败: {e}")
            elif hasattr(comp, 'convert_to_file_path'):
                try:
                    source = await comp.convert_to_file_path()
                except Exception as e:
                    logger.warning(f"[NAS] convert_to_file_path 失败: {e}")

            if not source or not os.path.exists(source):
                continue

            if os.path.islink(source):
                yield event.plain_result("跳过软链接文件")
                return

            try:
                file_size = os.path.getsize(source)
            except OSError as e:
                logger.warning(f"[NAS] 获取文件大小失败: {e}")
                continue

            if file_size > self.max_size:
                yield event.plain_result(f"文件超过限制：{format_size(file_size)} (上限 {format_size(self.max_size)})")
                return

            name = getattr(comp, 'name', None) or os.path.basename(source)
            name = Path(str(name)).name
            if not name:
                name = f"file_{int(time.time())}"

            if self.dedup:
                src_hash = file_hash(source)
                existing = await asyncio.to_thread(self.index.has_hash, src_hash)
                if existing:
                    yield event.plain_result(f"文件已存在，跳过: {Path(existing).name}")
                    return
            else:
                src_hash = file_hash(source)

            category = FileClassifier.get_category(name)
            save_dir = self.root / category

            save_path = save_dir / name
            if not self._safe_path(save_path):
                yield event.plain_result("文件名不合法")
                return
            stem, suffix = save_path.stem, save_path.suffix
            idx = 1
            while save_path.exists():
                save_path = save_dir / f"{stem}({idx}){suffix}"
                idx += 1

            try:
                shutil.copy2(source, save_path)
                fp = file_fingerprint(str(save_path))
                await asyncio.to_thread(
                    self.index.add, src_hash, str(save_path), save_path.name,
                    fp[0], fp[1], category
                )
                self._log_info(f"[NAS] SAVE | {uid} | {category}/{save_path.name} | {format_size(file_size)}")
                yield event.plain_result(f"已保存到 {save_path}")
            except Exception as e:
                logger.error(f"[NAS] 文件保存失败: {e}")
                yield event.plain_result(f"保存失败: {e}")
            return

    # ---------- ls ----------

    @filter.regex(r"^/?ls(\s|$)|^查看(\s|$)")
    async def cmd_ls(self, event: AstrMessageEvent):
        if not self._is_allowed(event.get_sender_id()):
            return
        if self._rebuilding:
            yield event.plain_result("NAS索引重建中，请稍后再试")
            return

        args = event.message_str.strip().split(maxsplit=1)
        if len(args) > 1:
            p = Path(args[1])
            target = p.resolve() if p.is_absolute() else (self.root / p).resolve()
        else:
            target = self.root

        if not self._safe_path(target):
            yield event.plain_result("路径不在允许范围内")
            return
        if not target.is_dir():
            yield event.plain_result(f"目录不存在: {target}")
            return

        entries = sorted(target.iterdir(), key=lambda x: (x.is_file(), x.name.lower()))
        if not entries:
            yield event.plain_result(f"{target.relative_to(self.root) or '/'} 是空目录")
            return

        lines = [f"{target.relative_to(self.root) or '/'}\n"]
        for e in entries[:30]:
            if e.is_dir():
                lines.append(f"  {e.name}/")
            else:
                lines.append(f"  {e.name} ({format_size(e.stat().st_size)})")
        if len(entries) > 30:
            lines.append(f"\n... 共 {len(entries)} 项")

        yield event.plain_result("\n".join(lines))

    # ---------- get ----------

    @filter.regex(r"^/?get(\s|$)|^发送文件(\s|$)")
    async def cmd_get(self, event: AstrMessageEvent):
        if not self._is_allowed(event.get_sender_id()):
            return
        if self._rebuilding:
            yield event.plain_result("NAS索引重建中，请稍后再试")
            return

        args = event.message_str.strip().split(maxsplit=1)
        if len(args) < 2:
            yield event.plain_result("用法: /get 文件名")
            return

        name = args[1].strip()

        # 绝对路径
        if os.path.isabs(name):
            file_path = Path(name).resolve()
            if not self._safe_path(file_path):
                yield event.plain_result("路径不在允许范围内")
                return
            if not file_path.exists():
                yield event.plain_result(f"文件不存在: {name}")
                return
            if file_path.is_dir():
                yield event.plain_result(f"是目录不是文件: {name}")
                return
            file_size = file_path.stat().st_size
            if file_size > self.max_size:
                yield event.plain_result(f"文件过大: {format_size(file_size)}")
                return
            self._log_info(f"[NAS] SEND | {event.get_sender_id()} | {file_path}")
            try:
                yield event.chain_result([File(name=file_path.name, file=str(file_path))])
            except (asyncio.TimeoutError, Exception) as e:
                logger.warning(f"[NAS] 文件发送超时: {e}")
                yield event.plain_result("文件发送超时，可能文件较大或网络波动，请重试")
                return
            yield event.plain_result(f"已发送: {file_path.name} ({format_size(file_size)})")
            return

        # 按名称搜索
        if "/" in name:
            cat_part, file_part = name.split("/", 1)
            results = await asyncio.to_thread(self.index.find_by_name, file_part.strip())
            results = [r for r in results if r["category"] == cat_part.strip()]
        else:
            results = await asyncio.to_thread(self.index.find_by_name, name)
            if not results:
                results = await asyncio.to_thread(self.index.search, name)

        if not results:
            yield event.plain_result(f"未找到文件: {name}")
            return
        if len(results) > 1:
            locations = "\n".join(f"  [{r['category']}] {r['name']}" for r in results[:5])
            yield event.plain_result(f"找到多个同名文件:\n{locations}\n用 /get 分类/文件名 指定")
            return

        info = results[0]
        file_path = Path(info["path"])

        if not self._safe_path(file_path):
            await asyncio.to_thread(self.index.remove, str(file_path))
            yield event.plain_result("索引路径不在允许范围内，已清理")
            return

        if not file_path.exists():
            await asyncio.to_thread(self.index.remove, str(file_path))
            yield event.plain_result("文件已被外部删除，已清理索引")
            return
        if info["size"] > self.max_size:
            yield event.plain_result(f"文件过大: {format_size(info['size'])}")
            return

        self._log_info(f"[NAS] SEND | {event.get_sender_id()} | {info['category']}/{info['name']}")
        try:
            yield event.chain_result([File(name=info["name"], file=str(file_path))])
        except (asyncio.TimeoutError, Exception) as e:
            logger.warning(f"[NAS] 文件发送超时: {e}")
            yield event.plain_result("文件发送超时，可能文件较大或网络波动，请重试")
            return
        yield event.plain_result(f"已发送: {info['name']} ({format_size(info['size'])})")

    # ---------- search ----------

    @filter.regex(r"^/?search(\s|$)|^搜索文件(\s|$)")
    async def cmd_search(self, event: AstrMessageEvent):
        if not self._is_allowed(event.get_sender_id()):
            return
        if self._rebuilding:
            yield event.plain_result("NAS索引重建中，请稍后再试")
            return

        args = event.message_str.strip().split(maxsplit=1)
        if len(args) < 2:
            yield event.plain_result("用法: /search 关键词")
            return

        keyword = args[1].strip()
        results = await asyncio.to_thread(self.index.search, keyword)

        valid = []
        stale = []
        for r in results:
            path = Path(r["path"])
            if not self._safe_path(path):
                stale.append(r)
            elif os.path.exists(r["path"]):
                valid.append(r)
            else:
                stale.append(r)

        if stale:
            for s in stale:
                await asyncio.to_thread(self.index.remove, s["path"])
            self._log_info(f"[NAS] 搜索懒清理: {len(stale)} 条脏记录")

        if not valid:
            yield event.plain_result(f"未找到包含「{keyword}」的文件")
            return

        lines = [f"搜索结果 ({len(valid)}个):\n"]
        for r in valid[:20]:
            lines.append(f"  [{r['category']}] {r['name']} ({format_size(r['size'])})")
        yield event.plain_result("\n".join(lines))

    # ---------- recent ----------

    @filter.regex(r"^/?recent(\s|$)|^最近文件(\s|$)")
    async def cmd_recent(self, event: AstrMessageEvent):
        if not self._is_allowed(event.get_sender_id()):
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
            if not self._safe_path(path):
                await asyncio.to_thread(self.index.remove, r["path"])
            elif os.path.exists(r["path"]):
                valid.append(r)
            else:
                await asyncio.to_thread(self.index.remove, r["path"])

        if not valid:
            yield event.plain_result("暂无文件记录")
            return

        lines = [f"最近文件 ({len(valid)}个):\n"]
        for r in valid:
            lines.append(f"  [{r['category']}] {r['name']} ({format_size(r['size'])})")
        yield event.plain_result("\n".join(lines))

    # ---------- tree ----------

    @filter.regex(r"^/?tree(\s|$)|^目录树(\s|$)")
    async def cmd_tree(self, event: AstrMessageEvent):
        if not self._is_allowed(event.get_sender_id()):
            return

        args = event.message_str.strip().split(maxsplit=2)
        target = self.root
        max_depth = 2
        if len(args) > 1:
            p = Path(args[1])
            target = p.resolve() if p.is_absolute() else (self.root / p).resolve()
        if len(args) > 2:
            try:
                max_depth = max(1, min(5, int(args[2].strip())))
            except ValueError:
                yield event.plain_result("用法: /tree [路径] [深度]")
                return

        if not self._safe_path(target):
            yield event.plain_result("路径不在允许范围内")
            return
        if not target.is_dir():
            yield event.plain_result(f"目录不存在: {target}")
            return

        root_name = str(target.relative_to(self.root) or "/")
        lines = [root_name]
        limit = 80

        def walk_dir(path: Path, depth: int):
            if len(lines) >= limit or depth > max_depth:
                return
            entries = sorted(path.iterdir(), key=lambda x: (x.is_file(), x.name.lower()))
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
        yield event.plain_result("\n".join(lines))

    # ---------- rm ----------

    @filter.regex(r"^/?rm(\s|$)|^删除文件(\s|$)")
    async def cmd_rm(self, event: AstrMessageEvent):
        uid = event.get_sender_id()
        if not self._is_admin(uid):
            yield event.plain_result("仅管理员可删除文件")
            return

        args = event.message_str.strip().split(maxsplit=1)
        if len(args) < 2:
            yield event.plain_result("用法: /rm 文件名")
            return

        name = args[1].strip()
        self._cleanup_pending()

        results = await asyncio.to_thread(self.index.find_by_name, name)
        if not results:
            results = await asyncio.to_thread(self.index.search, name)

        if not results:
            yield event.plain_result(f"未找到文件: {name}")
            return
        if len(results) > 1:
            locations = "\n".join(f"  [{r['category']}] {r['name']}" for r in results[:5])
            yield event.plain_result(f"找到多个文件:\n{locations}\n请指定完整路径")
            return

        info = results[0]
        target = Path(info["path"])

        if not self._safe_path(target):
            await asyncio.to_thread(self.index.remove, str(target))
            yield event.plain_result("索引路径不在允许范围内，已清理")
            return
        if not target.exists():
            await asyncio.to_thread(self.index.remove, str(target))
            yield event.plain_result("文件已被外部删除，已清理索引")
            return

        sig = (target.stat().st_size, target.stat().st_mtime_ns)
        self._delete_pending[uid] = {
            "path": target, "name": target.name,
            "sig": sig, "time": time.time(), "category": info["category"]
        }
        yield event.plain_result(
            f"确认删除 [{info['category']}] {target.name} ({format_size(info['size'])})？\n"
            f"{self.confirm_ttl}秒内回复「确认删除」执行，「取消」放弃"
        )

    @filter.regex(r"^确认删除$")
    async def cmd_confirm_delete(self, event: AstrMessageEvent):
        uid = event.get_sender_id()
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
        if (target.stat().st_size, target.stat().st_mtime_ns) != waiting["sig"]:
            yield event.plain_result("文件已变化，请重新发起删除")
            return

        target.unlink()
        await asyncio.to_thread(self.index.remove, str(target))
        self._log_info(f"[NAS] DELETE | {uid} | {waiting['category']}/{waiting['name']}")
        yield event.plain_result(f"已删除: {waiting['name']}")

    @filter.regex(r"^取消$")
    async def cmd_cancel(self, event: AstrMessageEvent):
        if self._delete_pending.pop(event.get_sender_id(), None):
            yield event.plain_result("已取消删除")

    # ---------- mv ----------

    @filter.regex(r"^/?mv(\s|$)|^移动文件(\s|$)")
    async def cmd_mv(self, event: AstrMessageEvent):
        if not self._is_admin(event.get_sender_id()):
            yield event.plain_result("仅管理员可移动文件")
            return

        args = event.message_str.strip().split(maxsplit=2)
        if len(args) < 3:
            yield event.plain_result("用法: /mv 源文件 目标路径")
            return

        src = Path(args[1]).resolve() if Path(args[1]).is_absolute() else self.root / args[1]
        dst = Path(args[2]).resolve() if Path(args[2]).is_absolute() else self.root / args[2]

        if not self._safe_path(src) or not self._safe_path(dst):
            yield event.plain_result("路径不合法")
            return
        if not src.exists():
            yield event.plain_result(f"源文件不存在: {args[1]}")
            return
        if dst.is_dir():
            dst = dst / src.name

        try:
            shutil.move(str(src), str(dst))
            fp = file_fingerprint(str(dst))
            h = file_hash(str(dst))
            new_cat = FileClassifier.get_category(dst.name)
            await asyncio.to_thread(
                self.index.move, str(src), h, str(dst), dst.name,
                fp[0], fp[1], new_cat
            )
            self._log_info(f"[NAS] MOVE | {event.get_sender_id()} | {src.name} -> {dst}")
            yield event.plain_result(f"已移动到 {dst}")
        except Exception as e:
            logger.error(f"[NAS] 移动失败: {e}")
            yield event.plain_result(f"移动失败: {e}")

    # ---------- rename ----------

    @filter.regex(r"^/?rename(\s|$)|^重命名(\s|$)")
    async def cmd_rename(self, event: AstrMessageEvent):
        if not self._is_admin(event.get_sender_id()):
            yield event.plain_result("仅管理员可重命名文件")
            return

        args = event.message_str.strip().split(maxsplit=2)
        if len(args) < 3:
            yield event.plain_result("用法: /rename 源文件 新名称")
            return

        raw_name = args[2].strip()
        if not raw_name or "/" in raw_name or "\\" in raw_name or Path(raw_name).name != raw_name:
            yield event.plain_result("新名称不能包含路径")
            return

        src_arg = args[1].strip()
        src = Path(src_arg).resolve() if Path(src_arg).is_absolute() else (self.root / src_arg).resolve()
        if not src.exists():
            results = await asyncio.to_thread(self.index.find_by_name, src_arg)
            if not results:
                results = await asyncio.to_thread(self.index.search, src_arg)
            if len(results) > 1:
                locations = "\n".join(f"  [{r['category']}] {r['name']}" for r in results[:5])
                yield event.plain_result(f"找到多个文件:\n{locations}\n请指定完整路径")
                return
            if results:
                src = Path(results[0]["path"]).resolve()

        if not self._safe_path(src):
            yield event.plain_result("路径不合法")
            return
        if not src.exists() or not src.is_file():
            yield event.plain_result(f"源文件不存在: {src_arg}")
            return

        dst = src.with_name(raw_name).resolve()
        if not self._safe_path(dst):
            yield event.plain_result("新名称不合法")
            return
        if dst.exists():
            yield event.plain_result(f"目标已存在: {raw_name}")
            return

        try:
            src.rename(dst)
            fp = file_fingerprint(str(dst))
            h = file_hash(str(dst))
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

    # ---------- du ----------

    @filter.regex(r"^/?du(\s|$)|^空间$")
    async def cmd_du(self, event: AstrMessageEvent):
        if not self._is_allowed(event.get_sender_id()):
            return
        if self._rebuilding:
            yield event.plain_result("NAS索引重建中，请稍后再试")
            return

        usage = shutil.disk_usage(self.root)
        stats = await asyncio.to_thread(self.index.get_stats)
        db_size = self.index.get_db_size()
        status = "重建中" if self._rebuilding else "正常"

        lines = [
            f"空间与状态",
            f"  总空间: {format_size(usage.total)}",
            f"  已用: {format_size(usage.used)}",
            f"  剩余: {format_size(usage.free)}",
            f"  数据库: {format_size(db_size)}",
            f"  索引状态: {status}",
            f"",
            f"文件统计 (共 {stats['total_count']} 个, {format_size(stats['total_size'])})",
        ]
        for cat, (count, size) in stats["categories"].items():
            if count > 0:
                lines.append(f"  {cat}: {count}个 ({format_size(size)})")

        yield event.plain_result("\n".join(lines))

    # ---------- health ----------

    @filter.regex(r"^/?health$")
    async def cmd_health(self, event: AstrMessageEvent):
        if not self._is_allowed(event.get_sender_id()):
            return

        stats = await asyncio.to_thread(self.index.get_stats)
        db_size = self.index.get_db_size()
        status = "重建中" if self._rebuilding else "正常"

        yield event.plain_result(
            f"NAS \u72b6\u6001\n\n"
            f"\u6587\u4ef6\u6570: {stats['total_count']}\n"
            f"\u6570\u636e\u5e93\u5927\u5c0f: {format_size(db_size)}\n"
            f"NAS\u5360\u7528: {format_size(stats['total_size'])}\n"
            f"\u91cd\u5efa\u72b6\u6001: {status}\n"
            f"\u7248\u672c: v2.1.0"
        )

    # ---------- repair ----------

    @filter.regex(r"^/?repair$")
    async def cmd_repair(self, event: AstrMessageEvent):
        if not self._is_admin(event.get_sender_id()):
            yield event.plain_result("仅管理员可修复索引")
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

    @filter.regex(r"^/?nas(\s|$)")
    async def cmd_help(self, event: AstrMessageEvent):
        yield event.plain_result(
            "NAS 助手 v2.1\n\n"
            "/nas                 - 显示帮助\n"
            "/ls [路径]           - 查看目录内容\n"
            "/tree [路径] [深度]  - 查看目录树\n"
            "/get 文件名          - 发送文件\n"
            "/search 关键词       - 搜索文件\n"
            "/recent [数量]       - 最近文件\n"
            "/rm 文件名           - 删除文件 (管理员，需确认)\n"
            "/rename 源 新名称    - 重命名文件 (管理员)\n"
            "/du                  - 空间与状态统计\n"
            "/repair              - 修复索引 (管理员)"
        )

    # ---------- vacuum ----------

    @filter.regex(r"^/?vacuum$")
    async def cmd_vacuum(self, event: AstrMessageEvent):
        if not self._is_admin(event.get_sender_id()):
            yield event.plain_result("仅管理员可执行")
            return
        yield event.plain_result("正在整理数据库...")
        await asyncio.to_thread(self.index.vacuum)
        yield event.plain_result("数据库整理完成 (VACUUM + ANALYZE)")
