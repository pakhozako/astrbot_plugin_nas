# 📦 astrbot_plugin_nas

> **Language:** [中文](./README.md) | English

![:name](https://count.getloli.com/@astrbot_plugin_nas?name=astrbot_plugin_nas&theme=minecraft&padding=6&offset=0&align=top&scale=1&pixelated=1&darkmode=auto)

> 🚀 **AstrBot Private Chat File Auto-Archiving Plugin** — SQLite WAL index + file-system ground truth, with classification, deduplication, search, tags, notes, batch operations, ZIP export, and index repair.

[![License](https://img.shields.io/badge/License-AGPL--3.0-green.svg)](LICENSE)
[![Python 3.10+](https://img.shields.io/badge/Python-3.10%2B-blue.svg)](https://www.python.org/)
[![AstrBot](https://img.shields.io/badge/AstrBot-%3E%3D4.16-orange.svg)](https://github.com/AstrBotDevs/AstrBot)

---

## 🏗️ Project Structure

```
main.py              ← Plugin entry + AstrBot handlers
├── access_control.py← Permission, group, and public scope helpers
├── config.py        ← Configuration normalization
├── constants.py     ← Version and internal path constants
├── command_args.py  ← Command text parsing
├── file_services.py ← File lookup, export, and send helpers
├── help_text.py     ← /nashelp text
├── runtime_state.py ← Rate limit and index task state
├── index.py         ← SQLite index layer
└── utils.py         ← Utilities + classifier
```

**Design principle:** the file system is the source of truth and SQLite is a rebuildable index cache. Delete `files.db`, restart, or run `/repair` to rebuild the index from archived files.

---

## ✨ Features

| Feature | Description |
|---------|-------------|
| 🗂️ Auto-classification | Extension-based routing to `Images/` `Videos/` `Music/` `Documents/` `Archives/` `Others/` |
| 🔐 Access control | Supports a management list; optional read-only access for everyone with one public directory and per-minute rate limits |
| 🔍 Search | SQLite `LIKE` name/note search plus `tag:<tag>` |
| 🏷️ Tags | `/tag` views, adds, and removes tags |
| 📝 Notes | `/note` stores file notes that are searchable |
| 🧬 Duplicate audit | `/dups` lists duplicate groups by content hash |
| 📦 Batch/export | `/batch` tags, untags, or moves matches; `/export` uses 7-Zip by default to create ZIP packages from selectors |
| 🧵 I/O isolation | Hashing, copy, move, traversal, and SQLite operations run in worker threads |
| 🛡️ Path guard | Managed read/delete/move operations stay inside `save_root` |
| ⛓️ Symlink protection | Symlinks are skipped during archiving and traversal |
| ✅ Delete confirmation | `/rm` requires `/confirm` |
| 🔄 Index repair | Startup rebuild, manual `/repair`, optional background consistency checks |

---

## 🚀 Quick Start

### Install via Plugin Market

AstrBot WebUI → Plugin Market → search `astrbot_plugin_nas` → Install → Restart.

### Install via Git

```bash
cd /AstrBot/data/plugins
git clone https://github.com/pakhozako/astrbot_plugin_nas
```

After restarting AstrBot, configure the admin list and archive path as needed in the plugin settings.

---

## ⚙️ Configuration

| Key | Default | Description |
|-----|---------|-------------|
| `save_root` | `data/plugin_data/astrbot_plugin_nas` | Archive root, local path or mounted NAS path |
| `admin_users` | `[]` | Admin user list |
| `allow_all_users` | `false` | Enable read-only access for everyone |
| `public_read_dir` | `Public` | Directory ordinary users can browse and fetch, inside `save_root` |
| `public_rate_limit_per_minute` | `10` | Read-only command limit per ordinary user per minute; 0 disables the limit |
| `public_file_recall_minutes` | `0` | Auto-recall delay for ordinary-user `/get` files; 0 disables it; group chats delete the group file entity |
| `allow_group_commands` | `false` | Allow commands in group chats |
| `max_file_size` | `2048` | Max file size in MB |
| `auto_save_enabled` | `true` | Auto-save private chat files |
| `dedup_enabled` | `true` | MD5 content deduplication |
| `delete_confirm_ttl` | `120` | Delete confirmation TTL in seconds |
| `log_enabled` | `true` | Enable operation logs |
| `export_max_files` | `100` | Max files per `/export` |
| `seven_zip_path` | `D:\7-Zip\7z.exe` | 7-Zip executable used by `/export`; invalid or empty values auto-discover 7-Zip and then fall back to built-in ZIP |
| `batch_max_files` | `100` | Max files per `/batch` |
| `rebuild_busy_timeout_seconds` | `600` | Timeout for index busy state, preventing stale rebuild status from blocking forever |
| `auto_repair_interval_minutes` | `0` | Background consistency check interval; 0 disables it |
| `categories` | `""` | Custom category JSON |

---

## 🎮 Commands

| Command | Description |
|---------|-------------|
| `/nashelp` | Show help |
| `/ls [path]` | List files |
| `/tree [path] [depth]` | Show a directory tree; default depth 2, max depth 5 |
| `/get file` | Send archived file; supports bare names, relative paths, wildcards, and fuzzy matching |
| `/search keyword` | Search files; use `tag:<tag>` for tags |
| `/search --recent [limit]` | Show recent files; default 10, max 30 |
| `/tag file [tags...]` | View/add/remove tags; `-tag` removes |
| `/note file [note]` | Show or set notes; use `-` to clear |
| `/status` | Space, index, and runtime status summary |
| `/dups [limit]` | Show duplicate file groups |
| `/batch selector tag|untag|move ...` | Batch tag, untag, or move files |
| `/export selector [name.zip]` | Export matching files as ZIP |
| `/rm file` | Delete file; requires `/confirm` |
| `/confirm` | Confirm pending delete |
| `/cancel` | Cancel pending delete |
| `/mv source target-path-or-new-name` | Move or rename a file |
| `/repair` | Repair index |
| `/repair vacuum` | Compact/analyze database |

Selectors support `tag:<tag>`, `category:<category>`, `search:<keyword>`, and `path:<directory>`. When `allow_all_users` is enabled, ordinary users can only browse, search, fetch, and view tags/notes inside `public_read_dir`.

Quote file names or paths that contain spaces, for example `/get "my file.zip"`.

All commands require `/` to avoid accidental triggers in normal chat.

---

## 🛡️ Disaster Recovery

| Scenario | Behavior |
|----------|----------|
| `files.db` deleted | Restart or `/repair` rebuilds from archived files |
| `files.db` corrupt | `integrity_check != ok` backs it up as `files.db.broken.<timestamp>` and rebuilds |
| File deleted externally | `/get`, `/search`, `/search --recent`, etc. lazily clean stale records |
| Rebuild in progress | Read commands remain usable; `/status` shows the active index task and new `/repair` waits or takes over after timeout |

Files live on disk. SQLite stores the index and tags.

---

## 📄 License

[AGPL-3.0](LICENSE)

---

## 🙏 Acknowledgements

- 🤖 [AstrBot](https://github.com/AstrBotDevs/AstrBot) — Agentic AI assistant framework

---

## 📬 Contact

- 🐙 GitHub: [pakhozako/astrbot_plugin_nas](https://github.com/pakhozako/astrbot_plugin_nas)
- 🐛 Issues: [Submit here](https://github.com/pakhozako/astrbot_plugin_nas/issues)
