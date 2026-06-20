"""
NAS 助手 - AstrBot 私聊文件自动归档插件
自动将私聊文件分类保存到本地磁盘/NAS，支持文件管理、搜索、去重等
"""

import os
import shutil
import time
import stat
import hashlib
from pathlib import Path
from datetime import datetime

from astrbot.api import logger
from astrbot.api.event import filter, AstrMessageEvent
from astrbot.api.message_components import File, Image, Video
from astrbot.api.star import Star


# ==================== 文件分类器 ====================

class FileClassifier:
    """按扩展名自动分类文件"""

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
    """计算文件 MD5"""
    h = hashlib.md5()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(8192), b""):
            h.update(chunk)
    return h.hexdigest()


def format_size(size: int) -> str:
    for unit in ["B", "KB", "MB", "GB", "TB"]:
        if size < 1024:
            return f"{size:.1f}{unit}"
        size /= 1024
    return f"{size:.1f}PB"


# ==================== 核心插件 ====================

class NASPlugin(Star):
    """NAS 助手插件"""

    def __init__(self, context, config=None):
        super().__init__(context)
        cfg = config or {}
        self.root = Path(cfg.get("save_root", r"D:\NAS")).resolve()
        self.allowed = set(str(u) for u in cfg.get("allowed_users", []))
        self.admins = set(str(u) for u in cfg.get("admin_users", []))
        self.max_size = int(cfg.get("max_file_size", 2048)) * 1024 * 1024
        self.auto_save = bool(cfg.get("auto_save_enabled", True))
        self.auto_preview = bool(cfg.get("auto_preview", True))
        self.dedup = bool(cfg.get("dedup_enabled", True))
        self.confirm_ttl = int(cfg.get("delete_confirm_ttl", 120))
        self.log_enabled = bool(cfg.get("log_enabled", True))
        self._delete_waiting = {}

        self._init_dirs()
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

    def _log(self, msg: str):
        if not self.log_enabled:
            return
        log_dir = self.root / "logs"
        log_dir.mkdir(exist_ok=True)
        ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        try:
            with open(log_dir / "nas.log", "a", encoding="utf-8") as f:
                f.write(f"[{ts}] {msg}\n")
        except Exception:
            pass

    # ---------- 核心：自动接收 ----------

    @filter.event_message_type(filter.EventMessageType.PRIVATE_MESSAGE, priority=100)
    async def on_file_received(self, event: AstrMessageEvent):
        """私聊收到文件时自动保存"""
        if not self.auto_save:
            return
        uid = event.get_sender_id()
        if not self._is_allowed(uid):
            return

        for comp in event.get_messages():
            if not isinstance(comp, (File, Image, Video)):
                continue

            # 获取文件路径
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

            # 确定文件名
            name = getattr(comp, 'name', None) or os.path.basename(source)
            if not name:
                name = f"file_{int(time.time())}"

            # 去重检测
            if self.dedup:
                src_hash = file_hash(source)
                for cat in FileClassifier.get_all_categories():
                    cat_dir = self.root / cat
                    if not cat_dir.exists():
                        continue
                    for f in cat_dir.iterdir():
                        if f.is_file() and file_hash(str(f)) == src_hash:
                            yield event.plain_result(f"文件已存在，跳过: [{cat}] {f.name}")
                            return

            # 自动分类
            category = FileClassifier.get_category(name)
            save_dir = self.root / category
            save_dir.mkdir(parents=True, exist_ok=True)

            # 处理重名
            save_path = save_dir / name
            stem, suffix = save_path.stem, save_path.suffix
            idx = 1
            while save_path.exists():
                save_path = save_dir / f"{stem}({idx}){suffix}"
                idx += 1

            # 复制文件
            try:
                shutil.copy2(source, save_path)
                size = save_path.stat().st_size
                self._log(f"SAVE | {uid} | {category}/{save_path.name} | {format_size(size)}")
                yield event.plain_result(f"已保存到 {save_path}")
            except Exception as e:
                yield event.plain_result(f"保存失败: {e}")
            return

    # ---------- 指令：查看目录 ----------

    @filter.command("ls")
    @filter.command("查看")
    async def cmd_ls(self, event: AstrMessageEvent):
        if not self._is_allowed(event.get_sender_id()):
            return

        args = event.message_str.strip().split(maxsplit=1)
        target = Path(args[1]).resolve() if len(args) > 1 and Path(args[1]).is_absolute() else self.root

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

    @filter.command("get")
    @filter.command("发送文件")
    async def cmd_get(self, event: AstrMessageEvent):
        if not self._is_allowed(event.get_sender_id()):
            return

        args = event.message_str.strip().split(maxsplit=1)
        if len(args) < 2:
            yield event.plain_result("用法: /get 文件名")
            return

        name = args[1].strip()

        # 支持 分类/文件名 格式
        if "/" in name:
            cat_part, file_part = name.split("/", 1)
            search_dirs = [self.root / cat_part.strip()]
        else:
            search_dirs = [self.root / cat for cat in FileClassifier.get_all_categories()]

        found = []
        for cat_dir in search_dirs:
            if not cat_dir.exists():
                continue
            for f in cat_dir.rglob(name if "/" not in name else file_part.strip()):
                if f.is_file():
                    found.append(f)

        if not found:
            yield event.plain_result(f"未找到文件: {name}")
            return
        if len(found) > 1:
            locations = "\n".join(f"  [{f.parent.name}] {f.name}" for f in found[:5])
            yield event.plain_result(f"找到多个同名文件:\n{locations}\n用 /get 分类/文件名 指定")
            return

        file = found[0]
        if file.stat().st_size > self.max_size:
            yield event.plain_result(f"文件过大: {format_size(file.stat().st_size)}")
            return

        self._log(f"SEND | {event.get_sender_id()} | {file.relative_to(self.root)}")
        yield event.chain_result([File(name=file.name, file=str(file))])

    # ---------- 指令：搜索 ----------

    @filter.command("search")
    @filter.command("搜索文件")
    async def cmd_search(self, event: AstrMessageEvent):
        if not self._is_allowed(event.get_sender_id()):
            return

        args = event.message_str.strip().split(maxsplit=1)
        if len(args) < 2:
            yield event.plain_result("用法: /search 关键词")
            return

        keyword = args[1].strip().lower()
        results = []
        for cat in FileClassifier.get_all_categories():
            cat_dir = self.root / cat
            if not cat_dir.exists():
                continue
            for f in cat_dir.rglob("*"):
                if f.is_file() and keyword in f.name.lower():
                    results.append((cat, f))

        if not results:
            yield event.plain_result(f"未找到包含「{keyword}」的文件")
            return

        lines = [f"搜索结果 ({len(results)}个):\n"]
        for cat, f in results[:20]:
            lines.append(f"  [{cat}] {f.name} ({format_size(f.stat().st_size)})")
        yield event.plain_result("\n".join(lines))

    # ---------- 指令：删除（二次确认） ----------

    @filter.command("rm")
    @filter.command("删除文件")
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

        # 搜索文件
        found = []
        for cat in FileClassifier.get_all_categories():
            cat_dir = self.root / cat
            if not cat_dir.exists():
                continue
            for f in cat_dir.rglob(name):
                if f.is_file():
                    found.append(f)

        if not found:
            yield event.plain_result(f"未找到文件: {name}")
            return
        if len(found) > 1:
            locations = "\n".join(f"  [{f.parent.name}] {f.name}" for f in found[:5])
            yield event.plain_result(f"找到多个文件:\n{locations}\n请指定完整路径")
            return

        target = found[0]
        sig = (target.stat().st_size, target.stat().st_mtime_ns)
        self._delete_waiting[uid] = {
            "path": target, "name": target.name,
            "sig": sig, "time": time.time()
        }
        yield event.plain_result(
            f"确认删除 [{target.parent.name}] {target.name} ({format_size(target.stat().st_size)})？\n"
            f"{self.confirm_ttl}秒内回复「确认删除」执行，「取消」放弃"
        )

    @filter.command("确认删除")
    async def cmd_confirm_delete(self, event: AstrMessageEvent):
        uid = event.get_sender_id()
        waiting = self._delete_waiting.pop(uid, None)
        if not waiting:
            yield event.plain_result("没有待确认的删除")
            return

        if time.time() - waiting["time"] > self.confirm_ttl:
            yield event.plain_result("删除确认已超时")
            return

        target: Path = waiting["path"]
        if not target.exists():
            yield event.plain_result("文件已不存在")
            return
        if (target.stat().st_size, target.stat().st_mtime_ns) != waiting["sig"]:
            yield event.plain_result("文件已变化，请重新发起删除")
            return

        target.unlink()
        self._log(f"DELETE | {uid} | {target.relative_to(self.root)}")
        yield event.plain_result(f"已删除: {waiting['name']}")

    @filter.command("取消")
    async def cmd_cancel(self, event: AstrMessageEvent):
        if self._delete_waiting.pop(event.get_sender_id(), None):
            yield event.plain_result("已取消删除")

    # ---------- 指令：移动 ----------

    @filter.command("mv")
    @filter.command("移动文件")
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
            self._log(f"MOVE | {event.get_sender_id()} | {src.name} -> {dst}")
            yield event.plain_result(f"已移动到 {dst}")
        except Exception as e:
            yield event.plain_result(f"移动失败: {e}")

    # ---------- 指令：磁盘空间 ----------

    @filter.command("du")
    @filter.command("空间")
    async def cmd_du(self, event: AstrMessageEvent):
        if not self._is_allowed(event.get_sender_id()):
            return

        usage = shutil.disk_usage(self.root)
        total_files = 0
        total_size = 0
        cat_stats = {}

        for cat in FileClassifier.get_all_categories():
            cat_dir = self.root / cat
            if not cat_dir.exists():
                continue
            count, size = 0, 0
            for f in cat_dir.rglob("*"):
                if f.is_file():
                    count += 1
                    size += f.stat().st_size
            cat_stats[cat] = (count, size)
            total_files += count
            total_size += size

        lines = [
            f"磁盘空间",
            f"  总空间: {format_size(usage.total)}",
            f"  已用: {format_size(usage.used)}",
            f"  剩余: {format_size(usage.free)}",
            f"",
            f"文件统计 (共 {total_files} 个, {format_size(total_size)})",
        ]
        for cat, (count, size) in cat_stats.items():
            if count > 0:
                lines.append(f"  {cat}: {count}个 ({format_size(size)})")

        yield event.plain_result("\n".join(lines))

    # ---------- 指令：帮助 ----------

    @filter.command("nas")
    @filter.command("nas帮助")
    async def cmd_help(self, event: AstrMessageEvent):
        yield event.plain_result(
            "NAS 助手指令\n\n"
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
