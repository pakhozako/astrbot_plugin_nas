"""SQLite 索引层：文件系统是真相源，SQLite 是缓存"""

import os
import time
import sqlite3
import threading
from pathlib import Path

from .utils import file_hash, FileClassifier


class FileIndex:
    def __init__(self, db_path: str):
        self.db_path = db_path
        self._lock = threading.Lock()
        self._init_db()

    def _init_db(self):
        db = Path(self.db_path)
        if db.exists() and db.stat().st_size > 0:
            try:
                with sqlite3.connect(self.db_path) as conn:
                    conn.execute("PRAGMA integrity_check")
            except Exception:
                import shutil
                ts = time.strftime("%Y%m%d_%H%M%S")
                try:
                    shutil.copy2(self.db_path, self.db_path + f".broken.{ts}")
                except Exception:
                    pass
                try:
                    os.remove(self.db_path)
                except Exception:
                    pass

        with sqlite3.connect(self.db_path) as conn:
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA synchronous=NORMAL")
            self._create_table(conn)
            conn.commit()

    def _create_table(self, conn):
        conn.execute("""
            CREATE TABLE IF NOT EXISTS files (
                path TEXT PRIMARY KEY, hash TEXT NOT NULL, name TEXT NOT NULL,
                size INTEGER NOT NULL, mtime INTEGER NOT NULL,
                category TEXT NOT NULL, created_at INTEGER NOT NULL
            )
        """)
        cols = {row[1] for row in conn.execute("PRAGMA table_info(files)").fetchall()}
        pk_col = next((row[1] for row in conn.execute("PRAGMA table_info(files)").fetchall() if row[5]), None)
        if pk_col == "hash":
            conn.execute("ALTER TABLE files RENAME TO files_old")
            conn.execute("""
                CREATE TABLE files (
                    path TEXT PRIMARY KEY, hash TEXT NOT NULL, name TEXT NOT NULL,
                    size INTEGER NOT NULL, mtime INTEGER NOT NULL,
                    category TEXT NOT NULL, created_at INTEGER NOT NULL
                )
            """)
            old_cols = {row[1] for row in conn.execute("PRAGMA table_info(files_old)").fetchall()}
            old_mtime = "mtime" if "mtime" in old_cols else "0"
            conn.execute(f"""
                INSERT OR REPLACE INTO files(path, hash, name, size, mtime, category, created_at)
                SELECT path, hash, name, size, {old_mtime}, category, created_at FROM files_old
            """)
            conn.execute("DROP TABLE files_old")
            cols = {row[1] for row in conn.execute("PRAGMA table_info(files)").fetchall()}
        if "mtime" not in cols:
            conn.execute("ALTER TABLE files ADD COLUMN mtime INTEGER NOT NULL DEFAULT 0")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_name ON files(name)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_hash ON files(hash)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_category ON files(category)")

    @staticmethod
    def _rows_to_dicts(rows):
        return [{"path": r[0], "name": r[1], "size": r[2], "category": r[3]} for r in rows]

    @staticmethod
    def _iter_category_files(root: Path):
        for cat in FileClassifier.get_all_categories():
            cat_dir = root / cat
            if not cat_dir.exists() or cat_dir.is_symlink():
                continue
            stack = [cat_dir]
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
                        stack.append(entry)
                    elif entry.is_file():
                        yield cat, entry

    def has_hash(self, h: str) -> str | None:
        with self._lock, sqlite3.connect(self.db_path) as conn:
            row = conn.execute("SELECT path FROM files WHERE hash=?", (h,)).fetchone()
            return row[0] if row else None

    def add(self, h: str, path: str, name: str, size: int, mtime: int, category: str):
        with self._lock, sqlite3.connect(self.db_path) as conn:
            conn.execute(
                "INSERT OR REPLACE INTO files(path, hash, name, size, mtime, category, created_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
                (path, h, name, size, mtime, category, int(time.time()))
            )
            conn.commit()

    def remove(self, path: str):
        with self._lock, sqlite3.connect(self.db_path) as conn:
            conn.execute("DELETE FROM files WHERE path=?", (path,))
            conn.commit()

    def move(self, old_path: str, h: str, new_path: str, name: str, size: int, mtime: int, category: str):
        with self._lock, sqlite3.connect(self.db_path) as conn:
            conn.execute(
                "INSERT OR REPLACE INTO files(path, hash, name, size, mtime, category, created_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
                (new_path, h, name, size, mtime, category, int(time.time()))
            )
            if new_path != old_path:
                conn.execute("DELETE FROM files WHERE path=?", (old_path,))
            conn.commit()

    def search(self, keyword: str) -> list:
        with self._lock, sqlite3.connect(self.db_path) as conn:
            rows = conn.execute(
                "SELECT path, name, size, category FROM files WHERE name LIKE ?",
                (f"%{keyword}%",)
            ).fetchall()
            return self._rows_to_dicts(rows)

    def find_by_name(self, name: str) -> list:
        with self._lock, sqlite3.connect(self.db_path) as conn:
            rows = conn.execute(
                "SELECT path, name, size, category FROM files WHERE name=?",
                (name,)
            ).fetchall()
            return self._rows_to_dicts(rows)

    def get_stats(self) -> dict:
        with self._lock, sqlite3.connect(self.db_path) as conn:
            rows = conn.execute("SELECT category, COUNT(*), SUM(size) FROM files GROUP BY category").fetchall()
            total_count = conn.execute("SELECT COUNT(*) FROM files").fetchone()[0]
            total_size = conn.execute("SELECT COALESCE(SUM(size),0) FROM files").fetchone()[0]
            stats = {}
            for cat, count, size in rows:
                stats[cat] = (count, size or 0)
            return {"categories": stats, "total_count": total_count, "total_size": total_size}

    def recent(self, limit: int) -> list:
        with self._lock, sqlite3.connect(self.db_path) as conn:
            rows = conn.execute(
                "SELECT path, name, size, category FROM files ORDER BY created_at DESC, mtime DESC LIMIT ?",
                (limit,)
            ).fetchall()
            return self._rows_to_dicts(rows)

    def repair_from_fs(self, root: Path) -> dict:
        with self._lock, sqlite3.connect(self.db_path) as conn:
            indexed_paths = {row[0] for row in conn.execute("SELECT path FROM files").fetchall()}
            fs_paths = set()
            added = 0
            updated = 0
            now = int(time.time())

            for cat, f in self._iter_category_files(root):
                try:
                    path = str(f)
                    st = f.stat()
                    h = file_hash(path)
                    row = conn.execute("SELECT size, mtime, hash, created_at FROM files WHERE path=?", (path,)).fetchone()
                    changed = row is None or row[:3] != (st.st_size, int(st.st_mtime), h)
                    conn.execute(
                        "INSERT OR REPLACE INTO files(path, hash, name, size, mtime, category, created_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
                        (path, h, f.name, st.st_size, int(st.st_mtime), cat, now if changed else row[3])
                    )
                    fs_paths.add(path)
                    if row is None:
                        added += 1
                    elif changed:
                        updated += 1
                except OSError:
                    continue

            removed = 0
            for path in indexed_paths - fs_paths:
                conn.execute("DELETE FROM files WHERE path=?", (path,))
                removed += 1

            conn.commit()
            return {"added": added, "updated": updated, "removed": removed, "total": len(fs_paths)}

    def rebuild_from_fs(self, root: Path):
        with self._lock, sqlite3.connect(self.db_path) as conn:
            existing = {}
            for row in conn.execute("SELECT path, hash, size, mtime FROM files").fetchall():
                existing[row[0]] = (row[1], row[2], row[3])

            fs_entries = {}
            for cat, f in self._iter_category_files(root):
                try:
                    st = f.stat()
                    fs_entries[str(f)] = (st.st_size, int(st.st_mtime), cat, f.name)
                except OSError:
                    continue

            conn.execute("DELETE FROM files")
            now = int(time.time())

            for path, (size, mtime, cat, name) in fs_entries.items():
                old = existing.get(path)
                if old and old[1] == size and old[2] == mtime:
                    h = old[0]
                else:
                    try:
                        h = file_hash(path)
                    except OSError:
                        continue

                conn.execute(
                    "INSERT OR REPLACE INTO files(path, hash, name, size, mtime, category, created_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
                    (path, h, name, size, mtime, cat, now)
                )

            conn.commit()
            return len(fs_entries)

    def vacuum(self):
        with self._lock, sqlite3.connect(self.db_path) as conn:
            conn.execute("VACUUM")
            conn.execute("ANALYZE")
            conn.commit()

    def get_db_size(self) -> int:
        try:
            return os.path.getsize(self.db_path)
        except OSError:
            return 0
