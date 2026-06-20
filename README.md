# NAS 助手 — AstrBot 私聊文件自动归档插件

![:name](https://count.getloli.com/@astrbot_plugin_nas?name=astrbot_plugin_nas&theme=minecraft&padding=6&offset=0&align=top&scale=1&pixelated=1&darkmode=auto)

> 一个基于 SQLite 索引的 NAS 文件管理插件，支持自动分类、秒级搜索、灾难恢复和大规模文件管理。

[![License](https://img.shields.io/badge/License-MIT-green.svg)](LICENSE)
[![Python 3.10+](https://img.shields.io/badge/Python-3.10%2B-blue.svg)](https://www.python.org/)
[![AstrBot](https://img.shields.io/badge/AstrBot-%3E%3D4.16-orange.svg)](https://github.com/AstrBotDevs/AstrBot)
[![GitHub](https://img.shields.io/badge/作者-pakhozako-blue)](https://github.com/pakhozako)

---

## ✨ 功能亮点

| 功能 | 状态 | 说明 |
|------|------|------|
| 🗂️ 自动分类 | ✅ | 按扩展名自动归档到 Images/Videos/Music/Documents/Archives/Others |
| 🔍 秒级搜索 | ✅ | SQLite 索引 + LIKE 查询，10 万文件毫秒级响应 |
| 🔐 文件去重 | ✅ | MD5 哈希检测，相同文件不重复保存 |
| 📁 文件系统真相源 | ✅ | SQLite 可随时从文件系统重建，删库不丢文件 |
| ⚡ WAL 优化 | ✅ | `PRAGMA journal_mode=WAL`，并发读写不锁表 |
| 🛡️ 灾难恢复 | ✅ | 数据库损坏自动备份 + 重建，插件不崩溃 |
| 🔄 指纹重建 | ✅ | 启动时先比对 `(size, mtime)` 指纹，变化的才算 MD5 |
| 🔒 路径安全校验 | ✅ | 所有操作校验路径在根目录下，防止路径穿越 |
| 👥 权限管理 | ✅ | 管理员/普通用户分离，删除和移动仅限管理员 |
| 📏 文件大小限制 | ✅ | 可配置单文件上限，默认 2GB |
| 🚫 软链接保护 | ✅ | 自动跳过软链接，不遍历外部目录 |
| 🧹 懒清理 | ✅ | 搜索和获取时自动清理脏索引记录 |
| 💾 异步 IO | ✅ | 所有 SQLite 操作通过 `asyncio.to_thread` 隔离，不阻塞事件循环 |
| 📊 磁盘统计 | ✅ | 通过索引缓存返回统计，不扫描全盘 |

---

## 🏗️ 架构设计

```
用户发送文件 / 输入指令
        ↓
   AstrBot 框架
        ↓
   NAS 插件 (regex 过滤器)
        ↓
  ┌─────────────┐
  │  SQLite 索引  │ ← 缓存层，可随时重建
  │  (WAL 模式)   │
  └──────┬──────┘
         ↓
  ┌─────────────┐
  │   文件系统    │ ← 真相源，不可丢失
  │  (本地/NAS)   │
  └─────────────┘
```

**核心原则：文件系统是真相源，SQLite 是索引缓存。**

- 删除 `files.db` → 重启 → 自动从文件系统重建索引
- 外部删除文件 → 搜索时自动清理脏记录
- 数据库损坏 → 自动备份 → 重建 → 服务不中断

---

## 📦 安装

### 方式一：插件市场安装（推荐）

1. 打开 AstrBot WebUI → **插件市场**
2. 搜索 `astrbot_plugin_nas`
3. 点击安装，重启 AstrBot

### 方式二：GitHub 克隆

```bash
cd /AstrBot/data/plugins
git clone https://github.com/pakhozako/astrbot_plugin_nas
```

重启 AstrBot 即可。

### 方式三：手动安装

1. 下载 [最新 Release](https://github.com/pakhozako/astrbot_plugin_nas/releases)
2. 解压到 `AstrBot/data/plugins/astrbot_plugin_nas/`
3. 重启 AstrBot

---

## ⚙️ 配置说明

在 AstrBot WebUI → 插件管理 → NAS 助手中配置。

| 配置项 | 默认值 | 说明 |
|--------|--------|------|
| `save_root` | `data/plugin_data/astrbot_plugin_nas` | 文件保存根目录，支持本地路径或 NAS 挂载路径 |
| `allowed_users` | `[]` | 允许使用的 QQ 列表，留空则所有用户可用 |
| `admin_users` | `[]` | 管理员列表，仅管理员可删除、移动文件 |
| `max_file_size` | `2048` | 单文件大小上限（MB） |
| `auto_save_enabled` | `true` | 启用私聊文件自动保存 |
| `dedup_enabled` | `true` | 启用文件去重（MD5） |
| `delete_confirm_ttl` | `120` | 删除确认超时（秒） |
| `log_enabled` | `true` | 启用操作日志 |
| `categories` | `""` | 自定义分类规则（JSON），留空使用默认分类 |

**示例配置：**

```json
{
  "save_root": "D:\\NAS",
  "allowed_users": ["2413474391"],
  "admin_users": ["2413474391"],
  "max_file_size": 2048,
  "auto_save_enabled": true,
  "dedup_enabled": true,
  "delete_confirm_ttl": 120,
  "log_enabled": true
}
```

---

## 🎮 使用方法

### 命令一览

| 命令 | 说明 | 示例 |
|------|------|------|
| `ls [路径]` | 查看目录内容 | `ls`、`ls Documents` |
| `get 文件名` | 发送已保存的文件 | `get 报告.pdf`、`get Music/歌曲.flac` |
| `search 关键词` | 搜索文件 | `search 周杰伦` |
| `rm 文件名` | 删除文件（需二次确认） | `rm 旧文件.zip` |
| `mv 源 目标` | 移动/重命名文件 | `mv a.txt Documents/a.txt` |
| `du` | 查看磁盘空间和文件统计 | `du` |
| `vacuum` | 数据库整理（管理员） | `vacuum` |
| `nas` | 显示帮助 | `nas` |
| `确认删除` | 确认删除操作 | 回复 `确认删除` |
| `取消` | 取消删除操作 | 回复 `取消` |

### 自动功能

私聊发送文件 → 自动分类保存 → 回复确认路径。

---

## 🔄 工作流程

### 文件上传

```
用户私聊发送文件
      ↓
  文件大小检查 → 超限拒绝
      ↓
  软链接检查 → 跳过软链接
      ↓
  MD5 去重检查 → 已存在则跳过
      ↓
  扩展名分类 → Images/Videos/Music/...
      ↓
  处理重名 → 自动加序号
      ↓
  复制到目标目录
      ↓
  SQLite 索引更新
      ↓
  回复确认
```

### 启动重建

```
AstrBot 启动
      ↓
  SQLite integrity_check → 损坏则备份+重建
      ↓
  扫描文件系统 → 收集 (path, size, mtime)
      ↓
  对比 SQLite 指纹
      ↓
  未变化 → 复用旧 hash
  变化   → 重算 MD5
  新增   → 算 MD5 + 写入
  删除   → 清理记录
      ↓
  索引就绪
```

---

## ⚡ 性能表现

| 文件数量 | 启动重建 | 搜索耗时 | 获取耗时 | 统计耗时 |
|----------|----------|----------|----------|----------|
| 1,000 | < 1s | < 5ms | < 2ms | < 1ms |
| 10,000 | < 3s | < 10ms | < 5ms | < 2ms |
| 50,000 | < 10s | < 20ms | < 10ms | < 3ms |
| 100,000 | < 30s | < 50ms | < 15ms | < 5ms |

> ⚠️ 以上为理论估算值，实际性能取决于硬件和文件系统类型。指纹机制确保启动时只对变化文件计算 MD5，大幅减少启动时间。

---

## 🛡️ 安全性

| 安全特性 | 说明 |
|----------|------|
| 🔐 路径穿越防护 | 所有路径操作校验 `relative_to(root)`，防止访问根目录外文件 |
| 👥 权限控制 | 管理员/普通用户分离，删除和移动仅限管理员 |
| 📏 文件大小限制 | 可配置单文件上限，防止超大文件写入 |
| 🚫 软链接保护 | 遍历时自动跳过软链接，不进入外部目录 |
| 🔑 删除二次确认 | 删除需回复「确认删除」，超时自动取消 |
| 📝 操作日志 | 所有文件操作记录到 AstrBot 日志 |

---

## 🔥 灾难恢复

**核心原则：文件系统是真相源。**

### 场景一：数据库损坏

```
files.db 损坏
    ↓
启动时 PRAGMA integrity_check 检测到异常
    ↓
自动备份: files.db.broken.时间戳
    ↓
重建空数据库
    ↓
从文件系统扫描恢复索引
    ↓
服务正常运行
```

### 场景二：误删数据库

```
手动删除 files.db
    ↓
重启 AstrBot
    ↓
自动从文件系统重建索引
    ↓
所有文件仍然存在，功能完全恢复
```

### 场景三：外部删除文件

```
SMB/文件管理器删除文件
    ↓
用户执行 /get 或 /search
    ↓
检测到文件不存在
    ↓
自动清理脏索引记录
    ↓
提示「文件已被外部删除，已清理索引」
```

---

## ❓ FAQ

### 为什么删除数据库不会丢文件？

因为文件系统是真相源。SQLite 只是索引缓存，存储的是文件的路径、哈希、大小等元数据。实际文件保存在磁盘上，与数据库无关。删除数据库后重启插件，会自动扫描文件系统重建索引。

### 为什么搜索不到文件？

可能原因：
1. 索引正在重建中（重启后首次启动会重建，请等待完成）
2. 文件在 `save_root` 目录外（插件只管理分类目录下的文件）
3. 文件名关键词不匹配（尝试更短的关键词）

### 如何重建索引？

重启 AstrBot 即可。插件启动时会自动执行 `rebuild_from_fs`，指纹机制确保只对变化文件重算 MD5。

### 如何清理数据库？

执行 `/vacuum` 命令（需管理员权限），会执行 `VACUUM` 和 `ANALYZE`，回收碎片空间并优化索引。

### 如何迁移 NAS 目录？

1. 修改配置中的 `save_root` 为新路径
2. 移动文件到新目录（保持分类子目录结构）
3. 重启 AstrBot，索引自动重建

---

## 🗺️ Roadmap

### v2.x（当前）
- ✅ 文件系统真相源架构
- ✅ SQLite WAL 模式
- ✅ 指纹优先重建
- ✅ 异步 IO 隔离
- ✅ 灾难恢复

### v3.x（规划）
- 🔲 `/health` 健康检查命令
- 🔲 `/repair` 索引修复命令
- 🔲 Web 管理界面
- 🔲 文件预览（图片缩略图/文本前 100 字）
- 🔲 定期自动校验任务
- 🔲 文件版本管理

---

## 📄 License

[MIT](LICENSE)

---

## 🙏 致谢

- [AstrBot](https://github.com/AstrBotDevs/AstrBot) — 优秀的 AI 助手框架
- [astrbot_plugin_file](https://github.com/Chris95743/astrbot_plugin_file) — 原始文件操作插件，本项目基于其改造

---

## 📬 联系方式

- GitHub Issues: [提交问题](https://github.com/pakhozako/astrbot_plugin_nas/issues)
- QQ: 2413474391
