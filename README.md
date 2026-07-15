# 📦 astrbot_plugin_nas

> **Language:** 中文 | [English](./README_EN.md)

![:name](https://count.getloli.com/@astrbot_plugin_nas?name=astrbot_plugin_nas&theme=minecraft&padding=6&offset=0&align=top&scale=1&pixelated=1&darkmode=auto)

> 🚀 **AstrBot 私聊文件自动归档插件** — 基于 SQLite WAL 索引 + 文件系统 Single Source of Truth 架构，支持自动分类、去重、搜索、预览、标签、备注、路径导入、目录监控、批量操作、ZIP 导出与索引修复。

[![License](https://img.shields.io/badge/License-AGPL--3.0-green.svg)](LICENSE)
[![Python 3.10+](https://img.shields.io/badge/Python-3.10%2B-blue.svg)](https://www.python.org/)
[![AstrBot](https://img.shields.io/badge/AstrBot-%3E%3D4.16-orange.svg)](https://github.com/AstrBotDevs/AstrBot)

---

## 🏗️ 项目结构

```
main.py              ← 插件入口 + AstrBot Handler
├── access_control.py← 权限、群聊和普通用户目录作用域
├── config.py        ← 配置归一化
├── constants.py     ← 版本、内部目录和扩展名常量
├── command_args.py  ← 命令文本解析
├── file_services.py ← 文件查找、导入、预览、导出与发送
├── help_text.py     ← /nashelp 帮助文本
├── runtime_state.py ← 限流与索引任务状态
├── index.py         ← SQLite 索引层
└── utils.py         ← 工具函数 + 分类器
```

**设计原则：** 文件系统是真实数据源，SQLite 是索引缓存。删除 `files.db` 后重启或执行 `/repair` 可从归档目录重新生成索引。

---

## ✨ 核心功能

| 功能 | 说明 |
|------|------|
| 🗂️ 自动分类 | 按扩展名归档到 `Images/` `Videos/` `Music/` `Documents/` `Archives/` `Others/` |
| 🔐 访问控制 | 支持管理列表；可开启所有人只读访问，并限制到单一公开目录和每分钟速率 |
| 🔍 搜索 | SQLite `LIKE` 文件名、备注搜索，支持 `tag:标签` |
| 🏷️ 标签 | `/tag` 查看、添加、移除标签 |
| 📝 备注 | `/note` 为文件写备注，搜索可命中备注内容 |
| 📥 路径导入 | `/add` 可从任意本机路径或 NAS 挂载路径导入文件或目录 |
| 👀 目录监控 | `/watch` 添加外部目录，可手动或定时扫描导入 |
| 🧬 重复审计 | `/dups` 按内容哈希列出重复文件组 |
| 📦 批量与导出 | `/batch` 批量打标签、移除标签、移动；`/export` 默认使用 7-Zip 按选择器打包 ZIP |
| 🖼️ 预览 | `/preview` 支持图片预览和文本摘要 |
| 🧵 IO 隔离 | 大文件哈希、复制、移动、遍历和 SQLite 操作放入线程执行 |
| 🛡️ 路径防护 | 归档后的读取、删除、移动都限制在 `save_root` 内 |
| ⛓️ 软链接保护 | 导入和遍历时跳过软链接，避免目录逃逸 |
| ✅ 删除二次确认 | `/rm` 后，需要回复 `/confirm` |
| 🔄 索引修复 | 启动重建、`/repair` 手动修复，可选后台一致性检查 |

---

## 🚀 快速开始

### 通过插件市场安装

AstrBot WebUI → 插件市场 → 搜索 `astrbot_plugin_nas` → 安装 → 重启。

### 通过 Git 安装

```bash
cd /AstrBot/data/plugins
git clone https://github.com/pakhozako/astrbot_plugin_nas
```

重启 AstrBot 后，在插件配置页按需填写管理员列表和保存目录。

---

## ⚙️ 配置说明

默认启用精简模式，只保留归档、浏览、获取、预览、搜索、外部路径导入/监控、移动、确认删除、状态与索引修复。管理员外部路径访问默认开启；普通用户始终受 `save_root` 和公开目录约束。关闭 `simple_mode` 可恢复标签、备注、重复审计、批处理和 ZIP 导出。

| 配置项 | 默认值 | 说明 |
|--------|--------|------|
| `save_root` | `data/plugin_data/astrbot_plugin_nas` | 文件保存根目录，本地路径或 NAS 挂载路径 |
| `admin_users` | `[]` | 管理员列表 |
| `admin_external_paths` | `true` | 管理员可直接访问 `save_root` 外的真实路径 |
| `simple_mode` | `true` | 停用标签、备注、重复审计、批处理和 ZIP 导出 |
| `allow_all_users` | `false` | 开启所有人只读访问 |
| `public_read_dir` | `Public` | 普通用户可查看和获取的目录，位于 `save_root` 内 |
| `public_rate_limit_per_minute` | `10` | 普通用户每分钟只读命令上限，0 表示不限制 |
| `public_file_recall_minutes` | `0` | 普通用户 `/get` 文件自动撤回延迟，0 表示关闭；群聊会删除群文件实体 |
| `allow_group_commands` | `false` | 是否允许群聊命令 |
| `max_file_size` | `2048` | 单文件大小上限，单位 MB |
| `auto_save_enabled` | `true` | 私聊收到文件时自动保存 |
| `dedup_enabled` | `true` | 基于 MD5 的内容去重 |
| `delete_confirm_ttl` | `120` | 删除确认超时时间，单位秒 |
| `log_enabled` | `true` | 是否记录操作日志 |
| `preview_text_chars` | `1200` | 文本预览最大字符数 |
| `path_import_max_files` | `2000` | `/add` 单次目录导入上限 |
| `watch_interval_minutes` | `0` | 监控目录自动导入间隔，0 表示关闭 |
| `export_max_files` | `100` | `/export` 单次导出最大文件数 |
| `seven_zip_path` | `D:\7-Zip\7z.exe` | `/export` 使用的 7-Zip 路径，留空或无效时自动探测并回退内置 ZIP |
| `batch_max_files` | `100` | `/batch` 单次处理最大文件数 |
| `rebuild_busy_timeout_seconds` | `600` | 索引任务忙碌状态超时，避免异常状态一直显示重建中 |
| `auto_repair_interval_minutes` | `0` | 后台一致性检查间隔，0 表示关闭 |
| `categories` | `""` | 自定义分类 JSON |

---

## 🎮 命令列表

| 命令 | 说明 |
|------|------|
| `/nashelp` | 显示帮助 |
| `/ls [路径]` | 查看目录内容 |
| `/tree [路径] [深度]` | 查看目录树，默认深度 2，最大深度 5 |
| `/get 文件` | 发送已保存文件，支持裸文件名、相对路径、通配符和模糊匹配 |
| `/preview 文件` | 图片预览或文本摘要 |
| `/search 关键词` | 搜索文件；标签搜索用 `tag:标签` |
| `/search --recent [数量]` | 查看最近文件，默认 10，最大 30 |
| `/tag 文件 [标签...]` | 查看、添加、移除标签，`-标签` 表示移除 |
| `/note 文件 [备注]` | 查看或设置备注，备注为 `-` 表示清空 |
| `/status` | 空间、索引和运行状态统计 |
| `/add 源路径 [分类]` | 从任意本机/NAS 路径导入文件或目录 |
| `/watch list|add|rm|run` | 管理监控目录并手动扫描 |
| `/dups [数量]` | 查看重复文件组 |
| `/batch 选择器 tag|untag|move ...` | 批量修改标签或移动文件 |
| `/export 选择器 [文件名.zip]` | 按选择器打包导出 ZIP |
| `/rm 文件` | 删除文件，需 `/confirm` |
| `/confirm` | 执行待确认删除 |
| `/cancel` | 取消待确认删除 |
| `/mv 源 目标路径或新文件名` | 移动或重命名文件 |
| `/repair` | 修复索引 |
| `/repair vacuum` | 整理数据库 |

选择器支持 `tag:标签`、`category:分类`、`search:关键词`、`path:目录`。开启 `allow_all_users` 后，普通用户只能在 `public_read_dir` 内使用查看、搜索、预览、获取、查看标签和查看备注类命令。

文件名或路径包含空格时可使用引号，例如 `/get "my file.zip"`。

所有命令都必须带 `/`，避免普通聊天误触发。

---

## 🛡️ 灾难恢复

| 场景 | 行为 |
|------|------|
| `files.db` 被删除 | 重启或 `/repair` 后从归档目录重建 |
| `files.db` 损坏 | `integrity_check` 不为 `ok` 时自动备份为 `files.db.broken.<时间戳>` 并重建 |
| 文件被外部删除 | `/get`、`/search`、`/search --recent` 等操作会懒清理脏索引 |
| 索引重建中 | 读命令继续可用，`/status` 显示当前索引任务；新的 `/repair` 会等任务结束或超时接管 |

文件内容存储在磁盘上；SQLite 保存索引和标签。

---

## 📄 许可证

[AGPL-3.0](LICENSE)

---

## 🙏 致谢

- 🤖 [AstrBot](https://github.com/AstrBotDevs/AstrBot) — Agentic AI 助手框架

---

## 📬 联系方式

- 🐙 GitHub: [pakhozako/astrbot_plugin_nas](https://github.com/pakhozako/astrbot_plugin_nas)
- 🐛 Issues: [提交问题](https://github.com/pakhozako/astrbot_plugin_nas/issues)
