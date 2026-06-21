# astrbot_plugin_nas

![:name](https://count.getloli.com/@astrbot_plugin_nas?name=astrbot_plugin_nas&theme=minecraft&padding=6&offset=0&align=top&scale=1&pixelated=1&darkmode=auto)

> **AstrBot 私聊文件自动归档插件** — 基于 SQLite WAL 索引 + 文件系统 Single Source of Truth 架构，支持自动分类、去重、模糊检索与灾难自愈。

[![License](https://img.shields.io/badge/License-MIT-green.svg)](LICENSE)
[![Python 3.10+](https://img.shields.io/badge/Python-3.10%2B-blue.svg)](https://www.python.org/)
[![AstrBot](https://img.shields.io/badge/AstrBot-%3E%3D4.16-orange.svg)](https://github.com/AstrBotDevs/AstrBot)

---

## Architecture

```
                ┌──────────────────┐
                │   User (Private)  │
                └────────┬─────────┘
                         │
                 AstrBot Framework
                         │
              ┌──────────▼──────────┐
              │    NAS Plugin       │
              │  (regex filter)     │
              └──────────┬──────────┘
                         │
              ┌──────────▼──────────┐
              │   SQLite (WAL)      │  ← Index Cache (rebuildable)
              │  Hash / Path / Name │
              └──────────┬──────────┘
                         │
              ┌──────────▼──────────┐
              │    File System      │  ← Single Source of Truth
              │   (Local / NAS)     │
              └─────────────────────┘
```

**Design Principle:** File system is the ground truth. SQLite is a disposable index cache — delete `files.db`, restart, and the index rebuilds automatically via fingerprint-based scan.

---

## Key Features

| Feature | Description |
|---------|-------------|
| **Auto-classify** | Extension-based routing to `Images/` `Videos/` `Music/` `Documents/` `Archives/` `Others/` |
| **Deduplication** | MD5 hash check; skip if content-identical file exists |
| **Fuzzy Search** | SQLite `LIKE` query with indexed `name` column — O(log n) retrieval |
| **WAL Mode** | `PRAGMA journal_mode=WAL` — concurrent read/write without table lock |
| **Async I/O** | All SQLite ops wrapped in `asyncio.to_thread()` — never blocks the event loop |
| **Disaster Recovery** | Corrupt DB → auto-backup + rebuild from FS; stale index → lazy cleanup on query |
| **Fingerprint Rebuild** | Startup scan compares `(size, mtime)` first; MD5 only on delta — O(1) for unchanged files |
| **Path Traversal Guard** | All ops validated via `Path.resolve().relative_to(root)` |
| **RBAC** | Admin / User separation; delete & move restricted to admins |
| **Symlink Protection** | Skip symlinks during traversal to prevent directory escape |
| **Soft-delete Confirmation** | `/rm` requires explicit `确认删除` reply with TTL-based expiry |
| **Transactional Move** | `/mv` uses single `UPDATE ... WHERE path=?` — atomic, no orphan records |

---

## Quick Start

### Install via Plugin Market

AstrBot WebUI → Plugin Market → search `astrbot_plugin_nas` → Install → Restart

### Install via Git

```bash
cd /AstrBot/data/plugins
git clone https://github.com/pakhozako/astrbot_plugin_nas
```

Restart AstrBot.

---

## Configuration

| Key | Default | Description |
|-----|---------|-------------|
| `save_root` | `data/plugin_data/astrbot_plugin_nas` | Root directory (local path or SMB/NFS mount) |
| `allowed_users` | `[]` | Whitelist of QQ IDs; empty = all users allowed |
| `admin_users` | `[]` | Admin QQ IDs (can delete/move files) |
| `max_file_size` | `2048` | Max file size in MB |
| `auto_save_enabled` | `true` | Auto-save files received via private chat |
| `dedup_enabled` | `true` | MD5-based deduplication |
| `delete_confirm_ttl` | `120` | Delete confirmation timeout (seconds) |
| `log_enabled` | `true` | Enable operation logging |
| `categories` | `""` | Custom category rules (JSON); empty = default |

---

## Commands

| Command | Description |
|---------|-------------|
| `ls [path]` | List directory contents |
| `get <filename>` | Send saved file by name |
| `search <keyword>` | Fuzzy search across all files |
| `rm <filename>` | Delete file (requires confirmation) |
| `mv <src> <dst>` | Move / rename file |
| `du` | Disk usage & file statistics |
| `vacuum` | SQLite VACUUM + ANALYZE (admin only) |
| `nas` | Show help |
| `确认删除` | Confirm pending delete |
| `取消` | Cancel pending delete |

---

## Disaster Recovery

| Scenario | Behavior |
|----------|----------|
| `files.db` deleted | Restart → auto-rebuild from FS → full recovery |
| `files.db` corrupt | `integrity_check` fails → backup as `files.db.broken.<timestamp>` → rebuild |
| File deleted externally | `/get` or `/search` detects missing → auto-clean stale index entry |
| Rebuild in progress | All read commands return "索引重建中，请稍后再试" |

**Zero data loss guarantee** — files live on disk, index is always reconstructible.

---

## Performance (Theoretical)

| Files | Rebuild | `/search` | `/get` | `/du` |
|-------|---------|-----------|--------|-------|
| 1K | < 1s | < 5ms | < 2ms | < 1ms |
| 10K | < 3s | < 10ms | < 5ms | < 2ms |
| 50K | < 10s | < 20ms | < 10ms | < 3ms |
| 100K | < 30s | < 50ms | < 15ms | < 5ms |

Fingerprint-based rebuild ensures O(1) skip for unchanged files.

---

## FAQ

**Q: Why won't deleting the database lose my files?**
A: File system is the Single Source of Truth. SQLite is an index cache. Files exist independently of the database.

**Q: Why can't I find a file via `/search`?**
A: Index may be rebuilding (wait for completion), file may be outside `save_root`, or keyword mismatch (try shorter term).

**Q: How to rebuild the index?**
A: Restart AstrBot. The plugin runs `rebuild_from_fs` automatically on startup.

**Q: How to optimize the database?**
A: Run `/vacuum` (admin only) — executes `VACUUM` + `ANALYZE` to reclaim space and optimize indexes.

---

## Roadmap

**v2.x (Current)**
- ✅ File system as Single Source of Truth
- ✅ SQLite WAL mode
- ✅ Fingerprint-based rebuild
- ✅ Async I/O isolation
- ✅ Disaster recovery with corruption detection

**v3.x (Planned)**
- `/health` endpoint
- `/repair` index integrity check
- Web dashboard for file management
- File preview (thumbnail / text excerpt)
- Periodic background consistency check
- File versioning

---

## License

[MIT](LICENSE)

---

## Acknowledgements

- [AstrBot](https://github.com/AstrBotDevs/AstrBot) — Agentic AI assistant framework
- [astrbot_plugin_file](https://github.com/Chris95743/astrbot_plugin_file) — Original file operations plugin

---

## Contact

- GitHub: [pakhozako/astrbot_plugin_nas](https://github.com/pakhozako/astrbot_plugin_nas)
- Issues: [Submit here](https://github.com/pakhozako/astrbot_plugin_nas/issues)
- QQ: 2413474391
