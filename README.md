# 📦 astrbot_plugin_nas

> **Language:** 中文 | [English](./README_EN.md)

![:name](https://count.getloli.com/@astrbot_plugin_nas?name=astrbot_plugin_nas&theme=minecraft&padding=6&offset=0&align=top&scale=1&pixelated=1&darkmode=auto)

> 🚀 **AstrBot 私聊文件自动归档插件** — 基于 SQLite WAL 索引 + 文件系统 Single Source of Truth 架构，支持自动分类、去重、模糊检索与灾难自愈。

[![License](https://img.shields.io/badge/License-MIT-green.svg)](LICENSE)
[![Python 3.10+](https://img.shields.io/badge/Python-3.10%2B-blue.svg)](https://www.python.org/)
[![AstrBot](https://img.shields.io/badge/AstrBot-%3E%3D4.16-orange.svg)](https://github.com/AstrBotDevs/AstrBot)

---

## 🏗️ 项目结构

```
main.py          ← 插件入口 + 命令处理 (482行)
├── index.py     ← SQLite 索引层 (161行)
└── utils.py     ← 工具函数 + 分类器 (52行)
```

```
                ┌──────────────────┐
                │   用户（私聊）     │
                └────────┬─────────┘
                         │
                 AstrBot 框架
                         │
              ┌──────────▼──────────┐
              │    NAS 插件          │
              │  (正则过滤器)         │
              └──────────┬──────────┘
                         │
              ┌──────────▼──────────┐
              │   SQLite (WAL)      │  ← 📊 索引缓存（可重建）
              │  Hash / Path / Name │
              └──────────┬──────────┘
                         │
              ┌──────────▼──────────┐
              │    文件系统           │  ← 💾 唯一真实数据源
              │   (本地 / NAS)       │
              └─────────────────────┘
```

**💡 设计原则：** 文件系统是真实数据源，SQLite 只是可丢弃的索引缓存——删除 `files.db` 后重启，索引会通过指纹扫描自动重建。

---

## ✨ 核心功能

| 🔧 功能 | 📝 说明 |
|---------|---------|
| 🗂️ **自动分类** | 按扩展名自动归档到 `Images/` `Videos/` `Music/` `Documents/` `Archives/` `Others/` |
| 🔐 **文件去重** | MD5 哈希校验，内容相同的文件自动跳过 |
| 🔍 **模糊搜索** | SQLite `LIKE` 查询 + `name` 列索引，O(log n) 检索 |
| ⚡ **WAL 模式** | `PRAGMA journal_mode=WAL`，并发读写不锁表 |
| 🧵 **异步 IO** | 所有 SQLite 操作通过 `asyncio.to_thread()` 隔离，不阻塞事件循环 |
| 🛡️ **灾难恢复** | 数据库损坏 → 自动备份 + 从文件系统重建；脏索引 → 查询时懒清理 |
| 🔄 **指纹重建** | 启动时先比对 `(size, mtime)` 指纹，未变化的跳过 MD5 计算——O(1) |
| 🚫 **路径穿越防护** | 所有操作通过 `Path.resolve().relative_to(root)` 校验 |
| 👥 **权限管理** | 管理员/普通用户分离，删除和移动仅限管理员 |
| ⛓️ **软链接保护** | 遍历时自动跳过软链接，防止目录逃逸 |
| ✅ **删除二次确认** | `/rm` 需要回复「确认删除」才执行，超时自动取消 |
| 🔄 **事务化移动** | `/mv` 使用单条 `UPDATE ... WHERE path=?`，原子操作不产生孤儿记录 |
| 🏥 **健康检查** | `/health` 一键查看：文件数、数据库大小、NAS 占用、重建状态 |
| ⏱️ **超时处理** | 文件发送失败时捕获异常，友好提示用户 |

---

## 🚀 快速开始

### 📲 通过插件市场安装

AstrBot WebUI → 插件市场 → 搜索 `astrbot_plugin_nas` → 安装 → 重启

### 💻 通过 Git 安装

```bash
cd /AstrBot/data/plugins
git clone https://github.com/pakhozako/astrbot_plugin_nas
```

重启 AstrBot。

---

## ⚙️ 配置说明

| 🔑 配置项 | 📌 默认值 | 📖 说明 |
|----------|----------|---------|
| `save_root` | `data/plugin_data/astrbot_plugin_nas` | 📁 文件保存根目录（本地路径或 SMB/NFS 挂载路径） |
| `allowed_users` | `[]` | ✅ 允许使用的 QQ 列表，留空则所有用户可用 |
| `admin_users` | `[]` | 👑 管理员 QQ 列表（可删除/移动文件） |
| `max_file_size` | `2048` | 📏 单文件大小上限（MB） |
| `auto_save_enabled` | `true` | 💾 私聊收到文件时自动保存 |
| `dedup_enabled` | `true` | 🔐 基于 MD5 的文件去重 |
| `delete_confirm_ttl` | `120` | ⏱️ 删除确认超时时间（秒） |
| `log_enabled` | `true` | 📝 启用操作日志 |
| `categories` | `""` | 🏷️ 自定义分类规则（JSON），留空使用默认分类 |

---

## 🎮 命令列表

| 📋 命令 | 📖 说明 |
|---------|---------|
| `ls [路径]` | 📂 查看目录内容 |
| `get <文件名>` | 📤 发送已保存的文件（支持绝对路径） |
| `search <关键词>` | 🔍 模糊搜索文件 |
| `rm <文件名>` | 🗑️ 删除文件（需二次确认） |
| `mv <源> <目标>` | 📁 移动/重命名文件 |
| `du` | 💾 磁盘空间和文件统计 |
| `health` | 🏥 健康检查（文件数、数据库大小、NAS 占用、重建状态） |
| `vacuum` | 🧹 SQLite VACUUM + ANALYZE（仅管理员） |
| `nas` | ❓ 显示帮助 |
| `确认删除` | ✅ 确认待执行的删除操作 |
| `取消` | ❌ 取消待执行的删除操作 |

---

## 🛡️ 灾难恢复

| 🚨 场景 | ⚙️ 行为 |
|---------|---------|
| `files.db` 被删除 | 🔄 重启 → 从文件系统自动重建 → 完全恢复 |
| `files.db` 损坏 | 💥 `integrity_check` 失败 → 备份为 `files.db.broken.<时间戳>` → 重建 |
| 文件被外部删除 | 🧹 `/get` 或 `/search` 检测到缺失 → 自动清理脏索引记录 |
| 重建进行中 | ⏳ 所有读命令返回「索引重建中，请稍后再试」 |

**🛡️ 零数据丢失保证** —— 文件存储在磁盘上，索引随时可重建。

---

## ⚡ 性能表现（理论值）

| 📊 文件数 | 🔄 重建 | 🔍 `/search` | 📤 `/get` | 💾 `/du` |
|----------|--------|-------------|----------|---------|
| 1K | < 1s | < 5ms | < 2ms | < 1ms |
| 10K | < 3s | < 10ms | < 5ms | < 2ms |
| 50K | < 10s | < 20ms | < 10ms | < 3ms |
| 100K | < 30s | < 50ms | < 15ms | < 5ms |

🔄 基于指纹的重建机制确保未变化文件 O(1) 跳过。

---

## ❓ 常见问题

**Q: 🗑️ 删除数据库后文件会丢失吗？**
A: 💾 不会。文件系统是唯一真实数据源，SQLite 只是索引缓存。文件独立于数据库存在。

**Q: 🔍 为什么 `/search` 搜不到文件？**
A: ⏳ 索引可能正在重建中（等待完成），文件可能在 `save_root` 目录外，或关键词不匹配（试试更短的关键词）。

**Q: 🔄 如何重建索引？**
A: 🔄 重启 AstrBot 即可。插件启动时会自动执行 `rebuild_from_fs`。

**Q: 🧹 如何优化数据库？**
A: 🧹 执行 `/vacuum`（仅管理员）—— 执行 `VACUUM` + `ANALYZE` 回收空间并优化索引。

---

## 🗺️ 路线图

**v2.x（当前）**
- ✅ 文件系统作为唯一真实数据源
- ✅ SQLite WAL 模式
- ✅ 基于指纹的重建机制
- ✅ 异步 IO 隔离
- ✅ 数据库损坏灾难恢复
- ✅ `/health` 健康检查
- ✅ 文件发送超时处理

**v3.x（计划中）**
- 🔧 `/repair` 索引完整性检查
- 🖥️ Web 管理面板
- 🖼️ 文件预览（缩略图/文本摘要）
- ⏰ 定期后台一致性检查
- 📚 文件版本管理

---

## 📄 许可证

[MIT](LICENSE)

---

## 🙏 致谢

- 🤖 [AstrBot](https://github.com/AstrBotDevs/AstrBot) — Agentic AI 助手框架

---

## 📬 联系方式

- 🐙 GitHub: [pakhozako/astrbot_plugin_nas](https://github.com/pakhozako/astrbot_plugin_nas)
- 🐛 Issues: [提交问题](https://github.com/pakhozako/astrbot_plugin_nas/issues)
