"""SQLite 索引层：文件系统是真相源，SQLite 是缓存。"""

import os
import time
import sqlite3
import threading
from pathlib import Path

from .constants import INTERNAL_DIRS, INTERNAL_FILES
from .utils import file_hash, FileClassifier


class FileIndex:
    def __init__(self, db_path: str):
        self.db_path = db_path
        self._lock = threading.Lock()
        self._init_db()

    def _init_db(self):
        db = Path(self.db_path)
        db.parent.mkdir(parents=True, exist_ok=True)
        if db.exists() and db.stat().st_size > 0:
            try:
                with sqlite3.connect(self.db_path) as conn:
                    result = conn.execute("PRAGMA integrity_check").fetchone()
                    if not result or result[0].lower() != "ok":
                        raise sqlite3.DatabaseError(f"integrity_check={result[0] if result else None}")
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
                path TEXT PRIMARY KEY,
                hash TEXT NOT NULL,
                name TEXT NOT NULL,
                size INTEGER NOT NULL,
                mtime INTEGER NOT NULL,
                category TEXT NOT NULL,
                created_at INTEGER NOT NULL,
                owner TEXT NOT NULL DEFAULT '',
                source_path TEXT NOT NULL DEFAULT '',
                note TEXT NOT NULL DEFAULT ''
            )
        """)
        table_info = conn.execute("PRAGMA table_info(files)").fetchall()
        cols = {row[1] for row in table_info}
        pk_col = next((row[1] for row in table_info if row[5]), None)
        if pk_col == "hash":
            conn.execute("ALTER TABLE files RENAME TO files_old")
            conn.execute("""
                CREATE TABLE files (
                    path TEXT PRIMARY KEY,
                    hash TEXT NOT NULL,
                    name TEXT NOT NULL,
                    size INTEGER NOT NULL,
                    mtime INTEGER NOT NULL,
                    category TEXT NOT NULL,
                    created_at INTEGER NOT NULL,
                    owner TEXT NOT NULL DEFAULT '',
                    source_path TEXT NOT NULL DEFAULT '',
                    note TEXT NOT NULL DEFAULT ''
                )
            """)
            old_cols = {row[1] for row in conn.execute("PRAGMA table_info(files_old)").fetchall()}
            old_mtime = "mtime" if "mtime" in old_cols else "0"
            old_owner = "owner" if "owner" in old_cols else "''"
            old_source = "source_path" if "source_path" in old_cols else "''"
            old_note = "note" if "note" in old_cols else "''"
            conn.execute(f"""
                INSERT OR REPLACE INTO files(path, hash, name, size, mtime, category, created_at, owner, source_path, note)
                SELECT path, hash, name, size, {old_mtime}, category, created_at, {old_owner}, {old_source}, {old_note}
                FROM files_old
            """)
            conn.execute("DROP TABLE files_old")
            cols = {row[1] for row in conn.execute("PRAGMA table_info(files)").fetchall()}
        if "mtime" not in cols:
            conn.execute("ALTER TABLE files ADD COLUMN mtime INTEGER NOT NULL DEFAULT 0")
        if "owner" not in cols:
            conn.execute("ALTER TABLE files ADD COLUMN owner TEXT NOT NULL DEFAULT ''")
        if "source_path" not in cols:
            conn.execute("ALTER TABLE files ADD COLUMN source_path TEXT NOT NULL DEFAULT ''")
        if "note" not in cols:
            conn.execute("ALTER TABLE files ADD COLUMN note TEXT NOT NULL DEFAULT ''")

        conn.execute("""
            CREATE TABLE IF NOT EXISTS tags (
                file_path TEXT NOT NULL,
                tag TEXT NOT NULL,
                created_at INTEGER NOT NULL,
                PRIMARY KEY(file_path, tag)
            )
        """)
        conn.execute("CREATE INDEX IF NOT EXISTS idx_name ON files(name)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_hash ON files(hash)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_category ON files(category)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_owner ON files(owner)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_tag ON tags(tag)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_source_path ON files(source_path)")

    @staticmethod
    def _rows_to_dicts(rows):
        return [
            {
                "path": r[0],
                "name": r[1],
                "size": r[2],
                "category": r[3],
                "created_at": r[4],
                "owner": r[5],
                "source_path": r[6],
                "note": r[7],
            }
            for r in rows
        ]

    @staticmethod
    def _iter_category_files(root: Path):
        known_categories = set(FileClassifier.get_all_categories())
        stack = [root]
        while stack:
            current = stack.pop()
            try:
                entries = list(current.iterdir())
            except OSError:
                continue
            for entry in entries:
                if entry.is_symlink():
                    continue
                try:
                    rel = entry.relative_to(root)
                except ValueError:
                    continue
                if rel.parts and rel.parts[0] in INTERNAL_DIRS:
                    continue
                if entry.is_dir():
                    stack.append(entry)
                elif entry.is_file():
                    if rel.parent == Path(".") and entry.name in INTERNAL_FILES:
                        continue
                    first = rel.parts[0] if rel.parts else ""
                    cat = first if first in known_categories else FileClassifier.get_category(entry.name)
                    yield cat, entry

    def _select_rows(self, conn, where: str = "", params: tuple = (), suffix: str = "") -> list:
        sql = """
            SELECT path, name, size, category, created_at, owner, source_path, note
            FROM files
        """
        if where:
            sql += f" WHERE {where}"
        if suffix:
            sql += f" {suffix}"
        return self._rows_to_dicts(conn.execute(sql, params).fetchall())

    def has_hash(self, h: str) -> str | None:
        with self._lock, sqlite3.connect(self.db_path) as conn:
            row = conn.execute("SELECT path FROM files WHERE hash=?", (h,)).fetchone()
            return row[0] if row else None

    def has_source_path(self, source_path: str) -> str | None:
        with self._lock, sqlite3.connect(self.db_path) as conn:
            row = conn.execute("SELECT path FROM files WHERE source_path=?", (source_path,)).fetchone()
            return row[0] if row else None

    def add(
        self,
        h: str,
        path: str,
        name: str,
        size: int,
        mtime: int,
        category: str,
        owner: str = "",
        source_path: str = "",
        note: str | None = None,
    ):
        with self._lock, sqlite3.connect(self.db_path) as conn:
            old = conn.execute("SELECT created_at, note FROM files WHERE path=?", (path,)).fetchone()
            created_at = old[0] if old else int(time.time())
            saved_note = old[1] if old and note is None else (note or "")
            conn.execute(
                """
                INSERT OR REPLACE INTO files(path, hash, name, size, mtime, category, created_at, owner, source_path, note)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (path, h, name, size, mtime, category, created_at, owner or "", source_path or "", saved_note),
            )
            conn.commit()

    def remove(self, path: str):
        with self._lock, sqlite3.connect(self.db_path) as conn:
            conn.execute("DELETE FROM files WHERE path=?", (path,))
            conn.execute("DELETE FROM tags WHERE file_path=?", (path,))
            conn.commit()

    def move(self, old_path: str, h: str, new_path: str, name: str, size: int, mtime: int, category: str):
        with self._lock, sqlite3.connect(self.db_path) as conn:
            row = conn.execute(
                "SELECT created_at, owner, source_path, note FROM files WHERE path=?",
                (old_path,),
            ).fetchone()
            created_at, owner, source_path, note = row if row else (int(time.time()), "", "", "")
            conn.execute(
                """
                INSERT OR REPLACE INTO files(path, hash, name, size, mtime, category, created_at, owner, source_path, note)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (new_path, h, name, size, mtime, category, created_at, owner, source_path, note),
            )
            if new_path != old_path:
                conn.execute("DELETE FROM files WHERE path=?", (old_path,))
                conn.execute("UPDATE tags SET file_path=? WHERE file_path=?", (new_path, old_path))
            conn.commit()

    def search(self, keyword: str) -> list:
        with self._lock, sqlite3.connect(self.db_path) as conn:
            term = f"%{keyword}%"
            return self._select_rows(conn, "(name LIKE ? OR note LIKE ?)", (term, term), "ORDER BY created_at DESC")

    def find_by_name(self, name: str) -> list:
        with self._lock, sqlite3.connect(self.db_path) as conn:
            return self._select_rows(conn, "name=?", (name,), "ORDER BY created_at DESC")

    def find_by_category(self, category: str) -> list:
        with self._lock, sqlite3.connect(self.db_path) as conn:
            return self._select_rows(conn, "category=?", (category,), "ORDER BY created_at DESC")

    def find_under_path(self, root_path: str) -> list:
        prefix = root_path.rstrip("\\/") + os.sep
        with self._lock, sqlite3.connect(self.db_path) as conn:
            return self._select_rows(
                conn,
                "(path=? OR path LIKE ?)",
                (root_path, prefix + "%"),
                "ORDER BY created_at DESC",
            )

    def find_by_path(self, path: str) -> dict | None:
        with self._lock, sqlite3.connect(self.db_path) as conn:
            rows = self._select_rows(conn, "path=?", (path,))
            return rows[0] if rows else None

    def set_note(self, path: str, note: str) -> bool:
        with self._lock, sqlite3.connect(self.db_path) as conn:
            cur = conn.execute("UPDATE files SET note=? WHERE path=?", (note or "", path))
            conn.commit()
            return cur.rowcount > 0

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
            return self._select_rows(conn, suffix="ORDER BY created_at DESC, mtime DESC LIMIT ?", params=(limit,))

    def add_tags(self, path: str, tags: list[str]):
        now = int(time.time())
        cleaned = sorted({t.strip().lower() for t in tags if t.strip()})
        with self._lock, sqlite3.connect(self.db_path) as conn:
            for tag in cleaned:
                conn.execute(
                    "INSERT OR IGNORE INTO tags(file_path, tag, created_at) VALUES (?, ?, ?)",
                    (path, tag, now),
                )
            conn.commit()
            return cleaned

    def remove_tags(self, path: str, tags: list[str]):
        cleaned = sorted({t.strip().lower() for t in tags if t.strip()})
        with self._lock, sqlite3.connect(self.db_path) as conn:
            for tag in cleaned:
                conn.execute("DELETE FROM tags WHERE file_path=? AND tag=?", (path, tag))
            conn.commit()
            return cleaned

    def list_tags(self, path: str) -> list[str]:
        with self._lock, sqlite3.connect(self.db_path) as conn:
            rows = conn.execute("SELECT tag FROM tags WHERE file_path=? ORDER BY tag", (path,)).fetchall()
            return [r[0] for r in rows]

    def search_by_tag(self, tag: str) -> list:
        with self._lock, sqlite3.connect(self.db_path) as conn:
            rows = conn.execute(
                """
                SELECT f.path, f.name, f.size, f.category, f.created_at, f.owner, f.source_path, f.note
                FROM files f
                JOIN tags t ON t.file_path = f.path
                WHERE t.tag=?
                ORDER BY f.created_at DESC
                """,
                (tag.strip().lower(),),
            ).fetchall()
            return self._rows_to_dicts(rows)

    def duplicate_groups(self, limit: int = 20) -> list[dict]:
        with self._lock, sqlite3.connect(self.db_path) as conn:
            rows = conn.execute(
                """
                SELECT hash, COUNT(*), COALESCE(SUM(size), 0)
                FROM files
                GROUP BY hash
                HAVING COUNT(*) > 1
                ORDER BY COUNT(*) DESC, COALESCE(SUM(size), 0) DESC
                LIMIT ?
                """,
                (limit,),
            ).fetchall()
            groups = []
            for h, count, size in rows:
                files = self._select_rows(conn, "hash=?", (h,), "ORDER BY created_at DESC")
                groups.append({"hash": h, "count": count, "size": size or 0, "files": files})
            return groups

    def repair_from_fs(self, root: Path) -> dict:
        with self._lock, sqlite3.connect(self.db_path) as conn:
            old_rows = {
                row[0]: row
                for row in conn.execute(
                    "SELECT path, size, mtime, hash, created_at, owner, source_path, note FROM files"
                ).fetchall()
            }

        fs_paths = set()
        upserts = []
        added = 0
        updated = 0
        now = int(time.time())

        for cat, f in self._iter_category_files(root):
            try:
                path = str(f)
                st = f.stat()
                row = old_rows.get(path)
                size = st.st_size
                mtime = int(st.st_mtime)
                changed = row is None or row[1] != size or row[2] != mtime
                h = file_hash(path) if changed else row[3]
                created_at = now if row is None else row[4]
                owner = "" if row is None else row[5]
                source_path = "" if row is None else row[6]
                note = "" if row is None else row[7]
                upserts.append((path, h, f.name, size, mtime, cat, created_at, owner, source_path, note))
                fs_paths.add(path)
                if row is None:
                    added += 1
                elif changed:
                    updated += 1
            except OSError:
                continue

        with self._lock, sqlite3.connect(self.db_path) as conn:
            conn.executemany(
                """
                INSERT OR REPLACE INTO files(path, hash, name, size, mtime, category, created_at, owner, source_path, note)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                upserts,
            )
            removed = 0
            for path in set(old_rows) - fs_paths:
                conn.execute("DELETE FROM files WHERE path=?", (path,))
                conn.execute("DELETE FROM tags WHERE file_path=?", (path,))
                removed += 1

            conn.commit()
            return {"added": added, "updated": updated, "removed": removed, "total": len(fs_paths)}

    def rebuild_from_fs(self, root: Path):
        with self._lock, sqlite3.connect(self.db_path) as conn:
            existing = {}
            for row in conn.execute(
                "SELECT path, hash, size, mtime, created_at, owner, source_path, note FROM files"
            ).fetchall():
                existing[row[0]] = {
                    "hash": row[1],
                    "size": row[2],
                    "mtime": row[3],
                    "created_at": row[4],
                    "owner": row[5],
                    "source_path": row[6],
                    "note": row[7],
                }

        fs_entries = {}
        for cat, f in self._iter_category_files(root):
            try:
                st = f.stat()
                fs_entries[str(f)] = (st.st_size, int(st.st_mtime), cat, f.name)
            except OSError:
                continue

        upserts = []
        now = int(time.time())
        for path, (size, mtime, cat, name) in fs_entries.items():
            old = existing.get(path)
            if old and old["size"] == size and old["mtime"] == mtime:
                h = old["hash"]
            else:
                try:
                    h = file_hash(path)
                except OSError:
                    continue

            upserts.append(
                (
                    path,
                    h,
                    name,
                    size,
                    mtime,
                    cat,
                    old["created_at"] if old else now,
                    old["owner"] if old else "",
                    old["source_path"] if old else "",
                    old["note"] if old else "",
                )
            )

        with self._lock, sqlite3.connect(self.db_path) as conn:
            conn.execute("DELETE FROM files")
            conn.executemany(
                """
                INSERT OR REPLACE INTO files(path, hash, name, size, mtime, category, created_at, owner, source_path, note)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                upserts,
            )

            missing = set(existing) - set(fs_entries)
            for path in missing:
                conn.execute("DELETE FROM tags WHERE file_path=?", (path,))

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
