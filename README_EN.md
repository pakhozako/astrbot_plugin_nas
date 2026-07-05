# 📦 astrbot_plugin_nas

> **Language:** [中文](./README.md) | English

![:name](https://count.getloli.com/@astrbot_plugin_nas?name=astrbot_plugin_nas&theme=minecraft&padding=6&offset=0&align=top&scale=1&pixelated=1&darkmode=auto)

> 🚀 **AstrBot Private Chat File Auto-Archiving Plugin** — SQLite WAL index + file-system ground truth, with classification, deduplication, search, previews, tags, path import, and index repair.

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
| 🔐 Access control | Supports admin configuration; group commands are off by default |
| 🔍 Search | SQLite `LIKE` name search plus `tag:<tag>` |
| 🏷️ Tags | `/tag` or `/标签` to view, add, and remove tags |
| 📥 Path import | `/add` or `/添加` imports from any local path or mounted NAS path |
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
| `allow_group_commands` | `false` | Allow commands in group chats |
| `max_file_size` | `2048` | Max file size in MB |
| `auto_save_enabled` | `true` | Auto-save private chat files |
| `dedup_enabled` | `true` | MD5 content deduplication |
| `delete_confirm_ttl` | `120` | Delete confirmation TTL in seconds |
| `log_enabled` | `true` | Enable operation logs |
| `preview_text_chars` | `1200` | Max characters for text previews |
| `path_import_max_files` | `2000` | Max files imported by one `/add` directory run |
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
| `/status` | `/状态`, `/空间` | Space and status summary |
| `/add source [category]` | `/添加 源路径 [分类]` | Import from any local/NAS path, admin only |
| `/tag file [tags...]` | `/标签 文件 [标签...]` | View/add/remove tags; `-tag` removes, admin only |
| `/rm file` | `/删除 文件` | Delete file, admin only, requires `/确认删除` |
| `/确认删除` | - | Confirm pending delete |
| `/取消` | - | Cancel pending delete |
| `/mv source target` | `/移动 源 目标` | Move file, admin only |
| `/rename source name` | `/重命名 源 新名称` | Rename file, admin only |
| `/repair` | `/修复` | Repair index, admin only |
| `/vacuum` | `/整理` | Compact/analyze database, admin only |

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
