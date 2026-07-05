"""File, index, preview, import, and export helpers for NAS commands."""

import asyncio
import difflib
import fnmatch
import os
import subprocess
import shutil
import time
import zipfile
from pathlib import Path
from typing import Any

from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent
from astrbot.api.message_components import File

from .constants import IMAGE_EXTENSIONS, INTERNAL_DIRS, INTERNAL_FILES, TEXT_EXTENSIONS
from .utils import file_hash, file_fingerprint, format_size, FileClassifier


class FileServiceMixin:
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
        locations = "\n".join(f"  {self._index_row_label(r)}" for r in results[:8])
        suffix = "\n..." if len(results) > 8 else ""
        return f"找到多个文件:\n{locations}{suffix}\n请使用 {command} 更精确的名称、category/file 或完整相对路径指定"

    def _index_row_label(self, row: dict) -> str:
        path = Path(row["path"])
        try:
            return str(path.resolve().relative_to(self.root.resolve()))
        except ValueError:
            return f"[{row['category']}] {row['name']}"

    def _direct_file_candidates(self, name: str, base_root: Path) -> list[Path]:
        normalized = name.replace("\\", "/")
        candidates = []
        seen = set()

        def add(path: Path):
            resolved = path.resolve()
            key = str(resolved).lower()
            if key not in seen:
                seen.add(key)
                candidates.append(resolved)

        try:
            public_rel = str(self.public_read_root.resolve().relative_to(self.root.resolve())).replace("\\", "/")
        except ValueError:
            public_rel = ""
        if public_rel and (normalized == public_rel or normalized.startswith(public_rel + "/")):
            add(self.root / normalized)

        add(base_root / name)
        if "/" not in normalized:
            add(self.public_read_root / name)
        return candidates

    def _find_filesystem_name_matches(self, root: Path, name: str, limit: int = 9) -> list[Path]:
        matches = []
        stack = [root]
        while stack and len(matches) < limit:
            current = stack.pop()
            try:
                entries = sorted(current.iterdir(), key=lambda x: (x.is_file(), x.name.lower()))
            except OSError:
                continue
            for entry in entries:
                if entry.is_symlink():
                    continue
                if entry.is_dir():
                    if self._skip_internal_dir(entry):
                        continue
                    stack.append(entry)
                    continue
                if entry.is_file() and entry.name == name and not self._skip_internal_file(entry):
                    matches.append(entry.resolve())
                    if len(matches) >= limit:
                        break
        return matches

    @staticmethod
    def _has_glob_pattern(text: str) -> bool:
        return "*" in text or "?" in text

    def _glob_match_values(self, path: Path, base_root: Path) -> list[str]:
        resolved = path.resolve()
        values = [resolved.name, str(resolved).replace("\\", "/")]
        for root in (base_root.resolve(), self.root.resolve()):
            try:
                values.append(str(resolved.relative_to(root)).replace("\\", "/"))
            except ValueError:
                pass
        return list(dict.fromkeys(values))

    def _path_matches_glob(self, path: Path, pattern: str, base_root: Path) -> bool:
        normalized = pattern.replace("\\", "/").lower()
        return any(
            fnmatch.fnmatchcase(value.lower(), normalized)
            for value in self._glob_match_values(path, base_root)
        )

    def _row_matches_glob(self, row: dict, pattern: str, base_root: Path) -> bool:
        return self._path_matches_glob(Path(row["path"]), pattern, base_root)

    def _find_filesystem_glob_matches(self, root: Path, pattern: str, limit: int = 9) -> list[Path]:
        matches = []
        stack = [root]
        while stack and len(matches) < limit:
            current = stack.pop()
            try:
                entries = sorted(current.iterdir(), key=lambda x: (x.is_file(), x.name.lower()))
            except OSError:
                continue
            for entry in entries:
                if entry.is_symlink():
                    continue
                if entry.is_dir():
                    if self._skip_internal_dir(entry):
                        continue
                    stack.append(entry)
                    continue
                if entry.is_file() and not self._skip_internal_file(entry):
                    resolved = entry.resolve()
                    if self._path_matches_glob(resolved, pattern, root):
                        matches.append(resolved)
                        if len(matches) >= limit:
                            break
        return matches

    @staticmethod
    def _fuzzy_key(text: str) -> str:
        return "".join(ch for ch in text.casefold() if ch.isalnum())

    @staticmethod
    def _fuzzy_tokens(text: str) -> list[str]:
        tokens = []
        current = []
        for ch in text.casefold():
            if ch.isalnum():
                current.append(ch)
            elif current:
                tokens.append("".join(current))
                current = []
        if current:
            tokens.append("".join(current))
        return [token for token in tokens if token]

    @staticmethod
    def _is_subsequence(needle: str, haystack: str) -> bool:
        if not needle:
            return False
        pos = 0
        for ch in haystack:
            if ch == needle[pos]:
                pos += 1
                if pos == len(needle):
                    return True
        return False

    def _fuzzy_score(self, query: str, value: str) -> float:
        query_key = self._fuzzy_key(query)
        value_key = self._fuzzy_key(value)
        if len(query_key) < 4 or not value_key:
            return 0.0
        if query_key == value_key:
            return 1.0
        if query_key in value_key:
            return 0.98

        token_score = 0.0
        tokens = self._fuzzy_tokens(query)
        if tokens:
            hits = sum(1 for token in tokens if self._fuzzy_key(token) in value_key)
            if hits == len(tokens):
                token_score = 0.94
            elif hits:
                token_score = 0.58 + 0.24 * (hits / len(tokens))

        ratio = difflib.SequenceMatcher(None, query_key, value_key).ratio()
        subsequence_score = 0.0
        if len(query_key) >= 6 and self._is_subsequence(query_key, value_key):
            subsequence_score = 0.72

        return max(token_score, ratio, subsequence_score)

    def _fuzzy_min_score(self, query: str) -> float:
        query_len = len(self._fuzzy_key(query))
        if query_len >= 10:
            return 0.70
        if query_len >= 6:
            return 0.76
        return 0.84

    def _rank_fuzzy_rows(self, rows: list[dict], query: str, base_root: Path, limit: int = 9) -> list[dict]:
        min_score = self._fuzzy_min_score(query)
        scored = []
        for row in rows:
            path = Path(row["path"])
            score = max(
                self._fuzzy_score(query, value)
                for value in self._glob_match_values(path, base_root)
            )
            if score >= min_score:
                scored.append((score, int(row.get("created_at") or 0), row))
        scored.sort(key=lambda item: (item[0], item[1]), reverse=True)
        if not scored:
            return []

        top_score = scored[0][0]
        keep_delta = 0.02 if top_score >= 0.9 else 0.05
        cutoff = max(min_score, top_score - keep_delta)
        return [row for score, _, row in scored if score >= cutoff][:limit]

    def _find_filesystem_fuzzy_matches(self, root: Path, query: str, limit: int = 9) -> list[Path]:
        min_score = self._fuzzy_min_score(query)
        scored = []
        stack = [root]
        while stack:
            current = stack.pop()
            try:
                entries = sorted(current.iterdir(), key=lambda x: (x.is_file(), x.name.lower()))
            except OSError:
                continue
            for entry in entries:
                if entry.is_symlink():
                    continue
                if entry.is_dir():
                    if self._skip_internal_dir(entry):
                        continue
                    stack.append(entry)
                    continue
                if not entry.is_file() or self._skip_internal_file(entry):
                    continue
                resolved = entry.resolve()
                score = max(
                    self._fuzzy_score(query, value)
                    for value in self._glob_match_values(resolved, root)
                )
                if score >= min_score:
                    try:
                        mtime = int(resolved.stat().st_mtime)
                    except OSError:
                        mtime = 0
                    scored.append((score, mtime, resolved))

        scored.sort(key=lambda item: (item[0], item[1]), reverse=True)
        if not scored:
            return []
        top_score = scored[0][0]
        keep_delta = 0.02 if top_score >= 0.9 else 0.05
        cutoff = max(min_score, top_score - keep_delta)
        return [path for score, _, path in scored if score >= cutoff][:limit]

    async def _resolve_indexed_file(
        self,
        query: str,
        command: str,
        allow_search: bool = True,
        allow_glob: bool = False,
        allow_fuzzy: bool = False,
        event: AstrMessageEvent | None = None,
    ) -> tuple[dict | None, str | None]:
        name = self._strip_quotes(query)
        if not name:
            return None, "文件名不能为空"

        base_root = self._scope_root_for_event(event)
        if allow_glob and self._has_glob_pattern(name):
            rows = await asyncio.to_thread(self.index.find_under_path, str(base_root.resolve()))
            rows = [r for r in rows if self._row_matches_glob(r, name, base_root)]
            valid = await self._valid_existing_rows(rows, event)
            if not valid:
                fs_matches = await asyncio.to_thread(
                    self._find_filesystem_glob_matches,
                    base_root,
                    name,
                )
                valid = [self._info_from_path(path) for path in fs_matches]
            if not valid:
                return None, f"未找到匹配文件: {name}"
            if len(valid) > 1:
                return None, self._multiple_match_message(valid, command)
            return valid[0], None

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

        for file_path in self._direct_file_candidates(name, base_root):
            if not self._safe_path(file_path) or not self._path_in_event_scope(event, file_path):
                continue
            if not file_path.exists():
                continue
            if not file_path.is_file():
                return None, f"不是文件: {name}"
            info = await asyncio.to_thread(self.index.find_by_path, str(file_path))
            return info or self._info_from_path(file_path), None

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
        for row in results:
            path = Path(row["path"])
            if not self._safe_path(path) or not path.exists() or not path.is_file():
                stale.append(row)
            else:
                valid.append(row)
        for row in stale:
            await asyncio.to_thread(self.index.remove, row["path"])

        if not valid and "/" not in normalized:
            fs_matches = await asyncio.to_thread(self._find_filesystem_name_matches, base_root, name)
            valid = [self._info_from_path(path) for path in fs_matches]

        if not valid and allow_fuzzy:
            rows = await asyncio.to_thread(self.index.find_under_path, str(base_root.resolve()))
            rows = self._filter_event_scope(event, rows)
            fuzzy_rows = await asyncio.to_thread(self._rank_fuzzy_rows, rows, name, base_root)
            valid = await self._valid_existing_rows(fuzzy_rows, event)
            if not valid:
                fs_matches = await asyncio.to_thread(
                    self._find_filesystem_fuzzy_matches,
                    base_root,
                    name,
                )
                valid = [self._info_from_path(path) for path in fs_matches]

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

        if key == "tag":
            rows = await asyncio.to_thread(self.index.search_by_tag, value.lstrip("#").lower())
        elif key in {"category", "cat"}:
            rows = await asyncio.to_thread(self.index.find_by_category, value)
        elif key in {"search", "s"}:
            rows = await asyncio.to_thread(self.index.search, value)
        elif key in {"path", "dir"}:
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
        rel_paths = self._export_relative_paths(rows)
        if not rel_paths:
            raise ValueError("没有可导出的有效文件")

        def write_zip():
            seven_zip = self._find_7zip()
            if seven_zip:
                try:
                    self._write_zip_with_7zip(seven_zip, zip_path, rel_paths)
                    return
                except Exception as e:
                    logger.warning(f"[NAS] 7-Zip 导出失败，回退内置 ZIP: {e}")
                    try:
                        zip_path.unlink(missing_ok=True)
                    except OSError:
                        pass
            self._write_zip_with_zipfile(zip_path, rel_paths)

        await asyncio.to_thread(write_zip)
        return zip_path

    def _export_relative_paths(self, rows: list[dict]) -> list[str]:
        rel_paths = []
        for row in rows:
            path = Path(row["path"])
            if path.is_symlink() or not path.is_file():
                continue
            try:
                rel = path.resolve().relative_to(self.root)
            except ValueError:
                continue
            rel_text = str(rel)
            if "\n" in rel_text or "\r" in rel_text:
                continue
            rel_paths.append(rel_text)
        return rel_paths

    def _find_7zip(self) -> str | None:
        candidates = []
        configured = str(getattr(self, "seven_zip_path", "") or "").strip()
        if configured:
            configured_path = Path(configured).expanduser()
            if configured_path.is_dir():
                candidates.extend([configured_path / "7z.exe", configured_path / "7za.exe"])
            else:
                candidates.append(configured_path)
        default_dir = Path(r"D:\7-Zip")
        candidates.extend([default_dir / "7z.exe", default_dir / "7za.exe"])
        for candidate in candidates:
            if candidate.is_file():
                return str(candidate)
        return shutil.which("7z") or shutil.which("7za")

    def _write_zip_with_7zip(self, seven_zip: str, zip_path: Path, rel_paths: list[str]) -> None:
        list_path = self._next_available_path(zip_path.parent, f".{zip_path.stem}.files.txt")
        list_path.write_text("\n".join(rel_paths), encoding="utf-8")
        try:
            result = subprocess.run(
                [
                    seven_zip,
                    "a",
                    "-tzip",
                    "-mx=5",
                    "-scsUTF-8",
                    str(zip_path),
                    f"@{list_path}",
                ],
                cwd=str(self.root),
                capture_output=True,
                text=True,
            )
            if result.returncode != 0:
                output = (result.stderr or result.stdout or "").strip()
                raise RuntimeError(output or f"exit code {result.returncode}")
        finally:
            try:
                list_path.unlink(missing_ok=True)
            except OSError:
                pass

    def _write_zip_with_zipfile(self, zip_path: Path, rel_paths: list[str]) -> None:
        with zipfile.ZipFile(zip_path, "w", compression=zipfile.ZIP_DEFLATED) as zf:
            for rel_path in rel_paths:
                path = self.root / rel_path
                if path.is_file() and not path.is_symlink():
                    zf.write(path, rel_path)

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
        return files, False

    def _skip_internal_dir(self, path: Path) -> bool:
        try:
            rel = path.resolve().relative_to(self.root)
        except ValueError:
            return False
        return bool(rel.parts) and rel.parts[0] in INTERNAL_DIRS

    def _skip_internal_file(self, path: Path) -> bool:
        try:
            rel = path.resolve().relative_to(self.root)
        except ValueError:
            return False
        if bool(rel.parts) and rel.parts[0] in INTERNAL_DIRS:
            return True
        return rel.parent == Path(".") and rel.name in INTERNAL_FILES

    @staticmethod
    def _is_text_file(path: Path) -> bool:
        return path.suffix.lower().lstrip(".") in TEXT_EXTENSIONS

    @staticmethod
    def _is_image_file(path: Path) -> bool:
        return path.suffix.lower().lstrip(".") in IMAGE_EXTENSIONS

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

    def _schedule_recall(self, bot: Any, message_ids: list[Any], delay_seconds: int, routing_params: dict[str, Any]) -> None:
        if not message_ids or delay_seconds <= 0:
            return
        task = asyncio.create_task(
            self._delete_messages_later(bot, message_ids, delay_seconds, routing_params),
        )
        tasks = getattr(self, "_recall_tasks", None)
        if isinstance(tasks, set):
            tasks.add(task)
            task.add_done_callback(tasks.discard)

    @staticmethod
    def _extract_message_ids(result: Any) -> list[Any]:
        if isinstance(result, dict):
            value = result.get("message_id")
            return [value] if value is not None else []
        if isinstance(result, list):
            ids = []
            for item in result:
                ids.extend(FileServiceMixin._extract_message_ids(item))
            return ids
        return []

    async def _delete_messages_later(
        self,
        bot: Any,
        message_ids: list[Any],
        delay_seconds: int,
        routing_params: dict[str, Any],
    ) -> None:
        await asyncio.sleep(delay_seconds)
        for message_id in message_ids:
            try:
                if routing_params:
                    await bot.call_action("delete_msg", message_id=message_id, **routing_params)
                else:
                    await bot.call_action("delete_msg", message_id=message_id)
            except Exception as first_error:
                if not routing_params:
                    logger.warning(f"[NAS] 自动撤回文件消息失败: {first_error}")
                    continue
                try:
                    await bot.call_action("delete_msg", message_id=message_id)
                except Exception as e:
                    logger.warning(f"[NAS] 自动撤回文件消息失败: {e}")

    async def _send_file_with_public_recall(
        self,
        event: AstrMessageEvent,
        file_path: Path,
        file_name: str,
        delay_seconds: int,
    ) -> tuple[bool, bool]:
        bot = getattr(event, "bot", None)
        if bot is None:
            return False, False

        try:
            from astrbot.core.platform.sources.aiocqhttp.aiocqhttp_message_event import (
                AiocqhttpMessageEvent,
            )

            payload = await AiocqhttpMessageEvent._from_segment_to_dict(
                File(name=file_name, file=str(file_path)),
            )
        except Exception as e:
            logger.warning(f"[NAS] 构建可撤回文件消息失败: {e}")
            return False, False

        is_group = bool(event.get_group_id())
        session_id = event.get_group_id() if is_group else event.get_sender_id()
        if not str(session_id).isdigit():
            return False, False

        routing_params = {}
        self_id = event.get_self_id()
        raw_event = getattr(event.message_obj, "raw_message", None)
        if raw_event is not None and hasattr(raw_event, "get"):
            self_id = raw_event.get("self_id") or self_id
        if self_id:
            routing_params["self_id"] = self_id

        try:
            if is_group:
                result = await bot.send_group_msg(
                    group_id=int(session_id),
                    message=[payload],
                    **routing_params,
                )
            else:
                result = await bot.send_private_msg(
                    user_id=int(session_id),
                    message=[payload],
                    **routing_params,
                )
        except Exception as e:
            logger.warning(f"[NAS] 可撤回文件发送失败，回退普通发送: {e}")
            return False, False

        message_ids = self._extract_message_ids(result)
        recalled = bool(message_ids)
        if recalled:
            self._schedule_recall(bot, message_ids, delay_seconds, routing_params)
        else:
            logger.warning("[NAS] 文件已发送，但平台未返回 message_id，无法自动撤回")
        return True, recalled

    async def _send_file(self, event: AstrMessageEvent, info: dict):
        file_path = Path(info["path"])
        file_size = file_path.stat().st_size
        if file_size > self.max_size:
            yield event.plain_result(f"文件过大: {format_size(file_size)}")
            return
        self._log_info(f"[NAS] SEND | {event.get_sender_id()} | {info['category']}/{info['name']}")
        recall_minutes = int(getattr(self, "public_file_recall_minutes", 0) or 0)
        should_recall = recall_minutes > 0 and self._is_public_user(str(event.get_sender_id()))
        if should_recall:
            sent, recalled = await self._send_file_with_public_recall(
                event,
                file_path,
                info["name"],
                recall_minutes * 60,
            )
            if sent:
                suffix = f"，将在 {recall_minutes} 分钟后自动撤回" if recalled else "，但当前平台未返回消息 ID，无法自动撤回"
                yield event.plain_result(f"已发送: {info['name']} ({format_size(file_size)}){suffix}")
                return
        try:
            yield event.chain_result([File(name=info["name"], file=str(file_path))])
        except (asyncio.TimeoutError, Exception) as e:
            logger.warning(f"[NAS] 文件发送失败: {e}")
            yield event.plain_result("文件发送失败，可能文件较大或网络波动，请重试")
            return
        yield event.plain_result(f"已发送: {info['name']} ({format_size(file_size)})")
