"""
NAS 助手 - AstrBot 私聊文件自动归档插件 v2.1.0
文件系统 = 真相源，SQLite = 索引缓存
"""

import os
import shutil
import time
import asyncio
import hashlib
import sqlite3
import threading
from pathlib import Path

from astrbot.api import AstrBotConfig, logger
from astrbot.api.event import filter, AstrMessageEvent
from astrbot.api.message_components import *
from astrbot.api.star import Context, Star, register


# ==================== 文件分类器 ====================

class FileClassifier:
    CATEGORIES = {
        "Images":    {"jpg", "jpeg", "png", "gif", "bmp", "webp", "svg", "ico", "tiff", "heic", "heif"},
        "Videos":    {"mp4", "mkv", "avi", "mov", "flv", "wmv", "webm", "ts", "m4v"},
        "Music":     {"mp3", "flac", "wav", "aac", "ogg", "wma", "m4a", "opus"},
        "Documents": {"pdf", "doc", "docx", "xls", "xlsx", "ppt", "pptx", "txt", "md", "csv", "json", "xml", "yaml", "yml"},
        "Archives":  {"zip", "rar", "7z", "tar", "gz", "bz2", "xz", "zst"},
    }

    @classmethod
    def get_category(cls, filename: str) -> str:
        ext = Path(filename).suffix.lower().lstrip(".")
        for category, extensions in cls.CATEGORIES.items():
            if ext in extensions:
                return category
        return "Others"

    @classmethod
    def get_all_categories(cls) -> list:
        return list(cls.CATEGORIES.keys()) + ["Others"]


# ==================== 工具函数 ====================

def file_hash(path: str) -> str:
    h = hashlib.md5()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(8192), b""):
            h.update(chunk)
    return h.hexdigest()


def file_fingerprint(path: str) -> tuple:
    """快速指纹：(size, int(mtime))"""
    st = os.stat(path)
    return (st.st_size, int(st.st_mtime))


def format_size(size: int) -> str:
    for unit in ["B", "KB", "MB", "GB", "TB"]:
        if size < 1024:
            return f"{size:.1f}{unit}"
        size /= 1024
    return f"{size:.1f}PB"


# ==================== SQLite 索引（全部同步，外层用 asyncio.to_thread 包装） ====================

class FileIndex:
    """
    文件索引 = 缓存层。
    文件系统是真相源，SQLite 可随时从文件系统重建。
    """

    def __init__(self, db_path: str):
        self.db_path = db_path
        self._lock = threading.Lock()
        self._init_db()

    def _init_db(self):
        with sqlite3.connect(self.db_path) as conn:
            conn.execute("""
                CREATE TABLE IF NOT EXISTS files (
                    hash TEXT PRIMARY KEY,
                    path TEXT NOT NULL,
                    name TEXT NOT NULL,
                    size INTEGER NOT NULL,
                    mtime INTEGER NOT NULL,
                    category TEXT NOT NULL,
                    created_at INTEGER NOT NULL
                )
            """)
            conn.execute("CREATE INDEX IF NOT EXISTS idx_name ON files(name)")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_category ON files(category)")
            conn.commit()

    def has_hash(self, h: str) -> str | None:
        with self._lock, sqlite3.connect(self.db_path) as conn:
            row = conn.execute("SELECT path FROM files WHERE hash=?", (h,)).fetchone()
            return row[0] if row else None

    def add(self, h: str, path: str, name: str, size: int, mtime: int, category: str):
        with self._lock, sqlite3.connect(self.db_path) as conn:
            conn.execute(
                "INSERT OR REPLACE INTO files VALUES (?, ?, ?, ?, ?, ?, ?)",
                (h, path, name, size, mtime, category, int(time.time()))
            )
            conn.commit()

    def remove(self, path: str):
        with self._lock, sqlite3.connect(self.db_path) as conn:
            conn.execute("DELETE FROM files WHERE path=?", (path,))
            conn.commit()

    def move(self, old_path: str, h: str, new_path: str, name: str, size: int, mtime: int, category: str):
        """事务化移动：先更新再提交，失败则回滚"""
        with self._lock, sqlite3.connect(self.db_path) as conn:
            conn.execute("BEGIN")
            try:
                conn.execute("DELETE FROM files WHERE path=?", (old_path,))
                conn.execute(
                    "INSERT INTO files VALUES (?, ?, ?, ?, ?, ?, ?)",
                    (h, new_path, name, size, mtime, category, int(time.time()))
                )
                conn.execute("COMMIT")
            except Exception:
                conn.execute("ROLLBACK")
                raise

    def search(self, keyword: str) -> list:
        with self._lock, sqlite3.connect(self.db_path) as conn:
            rows = conn.execute(
                "SELECT path, name, size, category FROM files WHERE name LIKE ?",
                (f"%{keyword}%",)
            ).fetchall()
            return [{"path": r[0], "name": r[1], "size": r[2], "category": r[3]} for r in rows]

    def find_by_name(self, name: str) -> list:
        with self._lock, sqlite3.connect(self.db_path) as conn:
            rows = conn.execute(
                "SELECT path, name, size, category FROM files WHERE name=?",
                (name,)
            ).fetchall()
            return [{"path": r[0], "name": r[1], "size": r[2], "category": r[3]} for r in rows]

    def get_stats(self) -> dict:
        with self._lock, sqlite3.connect(self.db_path) as conn:
            rows = conn.execute("SELECT category, COUNT(*), SUM(size) FROM files GROUP BY category").fetchall()
            total_count = conn.execute("SELECT COUNT(*) FROM files").fetchone()[0]
            total_size = conn.execute("SELECT COALESCE(SUM(size),0) FROM files").fetchone()[0]
            stats = {}
            for cat, count, size in rows:
                stats[cat] = (count, size or 0)
            return {"categories": stats, "total_count": total_count, "total_size": total_size}

    def get_all_entries(self) -> dict:
        """返回 {path: (hash, size, mtime)} 用于校验"""
        with self._lock, sqlite3.connect(self.db_path) as conn:
            rows = conn.execute("SELECT path, hash, size, mtime FROM files").fetchall()
            return {r[0]: (r[1], r[2], r[3]) for r in rows}

    def rebuild_from_fs(self, root: Path):
        """
        指纹优先重建：
        1. 扫描文件系统，收集 (path, size, mtime)
        2. 对比 SQLite 中的记录
        3. 未变化 → 跳过
        4. 新增/变化 → 算 MD5，更新索引
        5. SQLite 中存在但文件系统不存在 → 删除记录
        """
        with self._lock, sqlite3.connect(self.db_path) as conn:
            # 加载现有索引
            existing = {}
            for row in conn.execute("SELECT path, hash, size, mtime FROM files").fetchall():
                existing[row[0]] = (row[1], row[2], row[3])

            # 扫描文件系统
            fs_entries = {}  # {path: (size, mtime)}
            for cat in FileClassifier.get_all_categories():
                cat_dir = root / cat
                if not cat_dir.exists():
                    continue
                for f in cat_dir.iterdir():
                    if not f.is_file() or f.is_symlink():
                        continue
                    try:
                        st = f.stat()
                        fs_entries[str(f)] = (st.st_size, int(st.st_mtime), cat, f.name)
                    except OSError:
                        continue

            # 清空重建
            conn.execute("DELETE FROM files")
            now = int(time.time())

            for path, (size, mtime, cat, name) in fs_entries.items():
                old = existing.get(path)
                if old and old[1] == size and old[2] == mtime:
                    # 指纹未变，复用旧 hash
                    h = old[0]
                else:
                    # 新增或变化，算 MD5
                    try:
                        h = file_hash(path)
                    except OSError:
                        continue

                conn.execute(
                    "INSERT OR IGNORE INTO files VALUES (?, ?, ?, ?, ?, ?, ?)",
                    (h, path, name, size, mtime, cat, now)
                )

            conn.commit()
            return len(fs_entries)

    def vacuum(self):
        with self._lock, sqlite3.connect(self.db_path) as conn:
            conn.execute("VACUUM")
            conn.execute("ANALYZE")
            conn.commit()

    def remove_stale(self, path: str):
        """移除指向不存在文件的记录"""
        if not os.path.exists(path):
            self.remove(path)
            return True
        return False


# ==================== 核心插件 ====================

@register("NAS 助手", "pakhozako", "私聊文件自动归档到本地磁盘/NAS", "v2.1.0")
class NASPlugin(Star):

    def __init__(self, context: Context, config: AstrBotConfig):
        super().__init__(context)
        cfg = config or {}
        self.root = Path(cfg.get("save_root", str(Path("data/plugin_data/astrbot_plugin_nas")))).resolve()
        self.allowed = set(str(u) for u in cfg.get("allowed_users", []))
        self.admins = set(str(u) for u in cfg.get("admin_users", []))
        self.max_size = int(cfg.get("max_file_size", 2048)) * 1024 * 1024
        self.auto_save = bool(cfg.get("auto_save_enabled", True))
        self.dedup = bool(cfg.get("dedup_enabled", True))
        self.confirm_ttl = int(cfg.get("delete_confirm_ttl", 120))
        self.log_enabled = bool(cfg.get("log_enabled", True))
        self._delete_pending = {}

        self._init_dirs()
        self.index = FileIndex(str(self.root / "files.db"))
        logger.info(f"[NAS] 根目录: {self.root} | 自动保存: {self.auto_save}")

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

    # ---------- 启动时重建索引（指纹优先） ----------

    @filter.on_astrbot_loaded()
    async def on_loaded(self, event):
        count = await asyncio.to_thread(self.index.rebuild_from_fs, self.root)
        logger.info(f"[NAS] 索引重建完成: {count} 个文件")

    # ---------- 核心：自动接收 ----------

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
                except Exception:
                    pass
            elif hasattr(comp, 'convert_to_file_path'):
                try:
                    source = await comp.convert_to_file_path()
                except Exception:
                    pass

            if not source or not os.path.exists(source):
                continue

            if os.path.islink(source):
                yield event.plain_result("跳过软链接文件")
                return

            try:
                file_size = os.path.getsize(source)
            except OSError:
                continue

            if file_size > self.max_size:
                yield event.plain_result(f"文件超过限制：{format_size(file_size)} (上限 {format_size(self.max_size)})")
                return

            name = getattr(comp, 'name', None) or os.path.basename(source)
            if not name:
                name = f"file_{int(time.time())}"

            # 去重
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
            save_dir.mkdir(parents=True, exist_ok=True)

            save_path = save_dir / name
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
                logger.info(f"SAVE | {uid} | {category}/{save_path.name} | {format_size(file_size)}")
                yield event.plain_result(f"已保存到 {save_path}")
            except Exception as e:
                yield event.plain_result(f"保存失败: {e}")
            return

    # ---------- 指令：查看目录 ----------

    @filter.regex(r"^/?ls(\s|$)|^查看(\s|$)")
    async def cmd_ls(self, event: AstrMessageEvent):
        if not self._is_allowed(event.get_sender_id()):
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

    # ---------- 指令：发送文件 ----------

    @filter.regex(r"^/?get(\s|$)|^发送文件(\s|$)")
    async def cmd_get(self, event: AstrMessageEvent):
        if not self._is_allowed(event.get_sender_id()):
            return

        args = event.message_str.strip().split(maxsplit=1)
        if len(args) < 2:
            yield event.plain_result("用法: /get 文件名")
            return

        name = args[1].strip()

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

        # 文件系统是真相源：文件不存在则清理索引
        if not file_path.exists():
            await asyncio.to_thread(self.index.remove, str(file_path))
            yield event.plain_result("文件已被外部删除，已清理索引")
            return
        if info["size"] > self.max_size:
            yield event.plain_result(f"文件过大: {format_size(info['size'])}")
            return

        logger.info(f"SEND | {event.get_sender_id()} | {info['category']}/{info['name']}")
        yield event.chain_result([File(name=info["name"], file=str(file_path))])

    # ---------- 指令：搜索 ----------

    @filter.regex(r"^/?search(\s|$)|^搜索文件(\s|$)")
    async def cmd_search(self, event: AstrMessageEvent):
        if not self._is_allowed(event.get_sender_id()):
            return

        args = event.message_str.strip().split(maxsplit=1)
        if len(args) < 2:
            yield event.plain_result("用法: /search 关键词")
            return

        keyword = args[1].strip()
        results = await asyncio.to_thread(self.index.search, keyword)

        # 懒清理：搜索结果中文件不存在的自动移除
        valid = []
        stale = []
        for r in results:
            if os.path.exists(r["path"]):
                valid.append(r)
            else:
                stale.append(r)

        if stale:
            for s in stale:
                await asyncio.to_thread(self.index.remove, s["path"])
            logger.info(f"[NAS] 搜索懒清理: {len(stale)} 条脏记录")

        if not valid:
            yield event.plain_result(f"未找到包含「{keyword}」的文件")
            return

        lines = [f"搜索结果 ({len(valid)}个):\n"]
        for r in valid[:20]:
            lines.append(f"  [{r['category']}] {r['name']} ({format_size(r['size'])})")
        yield event.plain_result("\n".join(lines))

    # ---------- 指令：删除 ----------

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

        # 文件系统校验
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
        if not target.exists():
            await asyncio.to_thread(self.index.remove, str(target))
            yield event.plain_result("文件已被外部删除，已清理索引")
            return
        if (target.stat().st_size, target.stat().st_mtime_ns) != waiting["sig"]:
            yield event.plain_result("文件已变化，请重新发起删除")
            return

        target.unlink()
        await asyncio.to_thread(self.index.remove, str(target))
        logger.info(f"DELETE | {uid} | {waiting['category']}/{waiting['name']}")
        yield event.plain_result(f"已删除: {waiting['name']}")

    @filter.regex(r"^取消$")
    async def cmd_cancel(self, event: AstrMessageEvent):
        if self._delete_pending.pop(event.get_sender_id(), None):
            yield event.plain_result("已取消删除")

    # ---------- 指令：移动（事务化） ----------

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
            logger.info(f"MOVE | {event.get_sender_id()} | {src.name} -> {dst}")
            yield event.plain_result(f"已移动到 {dst}")
        except Exception as e:
            yield event.plain_result(f"移动失败: {e}")

    # ---------- 指令：磁盘空间 ----------

    @filter.regex(r"^/?du(\s|$)|^空间$")
    async def cmd_du(self, event: AstrMessageEvent):
        if not self._is_allowed(event.get_sender_id()):
            return

        usage = shutil.disk_usage(self.root)
        stats = await asyncio.to_thread(self.index.get_stats)

        lines = [
            f"磁盘空间",
            f"  总空间: {format_size(usage.total)}",
            f"  已用: {format_size(usage.used)}",
            f"  剩余: {format_size(usage.free)}",
            f"",
            f"文件统计 (共 {stats['total_count']} 个, {format_size(stats['total_size'])})",
        ]
        for cat, (count, size) in stats["categories"].items():
            if count > 0:
                lines.append(f"  {cat}: {count}个 ({format_size(size)})")

        yield event.plain_result("\n".join(lines))

    # ---------- 指令：帮助 ----------

    @filter.regex(r"^/?nas(\s|$)")
    async def cmd_help(self, event: AstrMessageEvent):
        yield event.plain_result(
            "NAS 助手 v2.1\n\n"
            "自动功能: 私聊发文件自动分类保存\n\n"
            "/ls [路径]      - 查看目录\n"
            "/get 文件名     - 发送文件\n"
            "/search 关键词  - 搜索文件\n"
            "/rm 文件名      - 删除文件 (需确认)\n"
            "/mv 源 目标     - 移动/重命名\n"
            "/du             - 磁盘空间\n"
            "/nas            - 此帮助\n\n"
            "分类: Images / Videos / Music / Documents / Archives / Others"
        )
