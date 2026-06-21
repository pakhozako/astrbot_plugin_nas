"""工具函数：文件哈希、格式化、指纹"""

import os
import hashlib
from pathlib import Path


def file_hash(path: str) -> str:
    """计算文件 MD5"""
    h = hashlib.md5()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
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
