# 📦 astrbot_plugin_nas

> **Language:** [中文](./README.md) | English

![:name](https://count.getloli.com/@astrbot_plugin_nas?name=astrbot_plugin_nas&theme=minecraft&padding=6&offset=0&align=top&scale=1&pixelated=1&darkmode=auto)

> 🚀 **AstrBot Private Chat File Auto-Archiving Plugin** — SQLite WAL index + file-system ground truth, with classification, deduplication, search, previews, tags, notes, path import, directory watch, batch operations, ZIP export, and index repair.

[![License](https://img.shields.io/badge/License-AGPL--3.0-green.svg)](LICENSE)
[![Python 3.10+](https://img.shields.io/badge/Python-3.10%2B-blue.svg)](https://www.python.org/)
[![AstrBot](https://img.shields.io/badge/AstrBot-%3E%3D4.16-orange.svg)](https://github.com/AstrBotDevs/AstrBot)

---

## 🏗️ Project Structure

```
main.py          ← Plugin entry + command handling
├── index.py     ← SQLite index layer
└── utils.py     ← Utilities + classifier
```

**Design principle:** the file system is the source of truth and SQLite is a rebuildable index cache. Delete `files.db`, restart, or run `/repair` to rebuild the index from archived files.

---

## ✨ Features

| Feature | Description |
|---------|-------------|
| 🗂️ Auto-classification | Extension-based routing to `Images/` `Videos/` `Music/` `Documents/` `Archives/` `Others/` |
| 🔐 Access control | Supports a management list; optional read-only access for everyone with one public directory and per-minute rate limits |
| 🔍 Search | SQLite `LIKE` name/note search plus `tag:<tag>` |
| 🏷️ Tags | `/tag` or `/标签` to view, add, and remove tags |
| 📝 Notes | `/note` or `/备注` stores file notes that are searchable |
| 📥 Path import | `/add` or `/添加` imports from any local path or mounted NAS path |
| 👀 Directory watch | `/watch` or `/监控` adds external directories for manual or scheduled import |
| 🧬 Duplicate audit | `/dups` or `/重复` lists duplicate groups by content hash |
| 📦 Batch/export | `/batch` tags, untags, or moves matches; `/export` creates ZIP packages from selectors |
| 🖼️ Preview | `/preview` or `/预览` supports images and text excerpts |
| 🧵 I/O isolation | Hashing, copy, move, traversal, and SQLite operations run in worker threads |
| 🛡️ Path guard | Managed read/delete/move operations stay inside `save_root` |
| ⛓️ Symlink protection | Symlinks are skipped during import and traversal |
| ✅ Delete confirmation | `/rm` or `/删除` requires `/确认删除` |
| 🔄 Index repair | Startup rebuild, manual `/repair` or `/修复`, optional background consistency checks |

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
| `allow_group_commands` | `false` | Allow commands in group chats |
| `max_file_size` | `2048` | Max file size in MB |
| `auto_save_enabled` | `true` | Auto-save private chat files |
| `dedup_enabled` | `true` | MD5 content deduplication |
| `delete_confirm_ttl` | `120` | Delete confirmation TTL in seconds |
| `log_enabled` | `true` | Enable operation logs |
| `preview_text_chars` | `1200` | Max characters for text previews |
| `path_import_max_files` | `2000` | Max files imported by one `/add` directory run |
| `watch_interval_minutes` | `0` | Scheduled watch import interval; 0 disables it |
| `export_max_files` | `100` | Max files per `/export` |
| `batch_max_files` | `100` | Max files per `/batch` |
| `rebuild_busy_timeout_seconds` | `600` | Timeout for index busy state, preventing stale rebuild status from blocking forever |
| `auto_repair_interval_minutes` | `0` | Background consistency check interval; 0 disables it |
| `categories` | `""` | Custom category JSON |

---

## 🎮 Commands

| English | Chinese | Description |
|---------|---------|-------------|
| `/nashelp` | `/nas帮助` | Show help |
| `/ls [path]` | `/列表 [路径]`, `/查看 [路径]` | List files |
| `/get file` | `/获取 文件`, `/下载 文件` | Send archived file; supports `category/file` |
| `/preview file` | `/预览 文件` | Image preview or text excerpt |
| `/search keyword` | `/搜索 关键词` | Search files; use `tag:<tag>` for tags |
| `/recent [limit]` | `/最近 [数量]` | Show recent files |
| `/tags file` | `/查看标签 文件` | Show file tags |
| `/note file [note]` | `/备注 文件 [备注]` | Show or set notes; use `-` to clear |
| `/status` | `/状态`, `/空间` | Space and status summary |
| `/add source [category]` | `/添加 源路径 [分类]` | Import from any local/NAS path |
| `/watch list|add|rm|run` | `/监控 列表|添加|删除|扫描` | Manage watched directories and scan them |
| `/dups [limit]` | `/重复 [数量]` | Show duplicate file groups |
| `/batch selector tag|untag|move ...` | `/批量 选择器 标签|移除标签|移动 ...` | Batch tag, untag, or move files |
| `/export selector [name.zip]` | `/导出 选择器 [文件名.zip]` | Export matching files as ZIP |
| `/tag file [tags...]` | `/标签 文件 [标签...]` | View/add/remove tags; `-tag` removes |
| `/rm file` | `/删除 文件` | Delete file; requires `/确认删除` |
| `/确认删除` | - | Confirm pending delete |
| `/取消` | - | Cancel pending delete |
| `/mv source target` | `/移动 源 目标` | Move file |
| `/rename source name` | `/重命名 源 新名称` | Rename file |
| `/repair` | `/修复` | Repair index |
| `/vacuum` | `/整理` | Compact/analyze database |

Selectors support `tag:<tag>`, `category:<category>`, `search:<keyword>`, and `path:<directory>`. When `allow_all_users` is enabled, ordinary users can only browse, search, preview, fetch, and view tags/notes inside `public_read_dir`.

All commands require `/` to avoid accidental triggers in normal chat.

---

## 🛡️ Disaster Recovery

| Scenario | Behavior |
|----------|----------|
| `files.db` deleted | Restart or `/repair` rebuilds from archived files |
| `files.db` corrupt | `integrity_check != ok` backs it up as `files.db.broken.<timestamp>` and rebuilds |
| File deleted externally | `/get`, `/search`, `/recent`, etc. lazily clean stale records |
| Rebuild in progress | Read commands return "NAS索引重建中，请稍后再试" |

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
