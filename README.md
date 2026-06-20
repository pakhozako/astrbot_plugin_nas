<div align="center">

![:name](https://count.getloli.com/@astrbot_plugin_nas?name=astrbot_plugin_nas&theme=minecraft&padding=6&offset=0&align=top&scale=1&pixelated=1&darkmode=auto)

# astrbot_plugin_nas

_✨ [AstrBot](https://github.com/AstrBotDevs/AstrBot) NAS 助手 - 私聊文件自动归档 ✨_  

[![License](https://img.shields.io/badge/License-MIT-green.svg)](https://opensource.org/licenses/MIT)
[![Python 3.10+](https://img.shields.io/badge/Python-3.10%2B-blue.svg)](https://www.python.org/)
[![AstrBot](https://img.shields.io/badge/AstrBot-4.16%2B-orange.svg)](https://github.com/AstrBotDevs/AstrBot)
[![GitHub](https://img.shields.io/badge/作者-pakhozako-blue)](https://github.com/pakhozako)

</div>

## 🤝 介绍

- 一个基于 AstrBot 的 NAS 文件管理插件，将 QQ 私聊变成轻量级文件管理入口。
- 私聊发文件自动分类保存到本地磁盘或 NAS 挂载目录，支持文件管理、搜索、去重、删除二次确认等。

## 📦 安装

- 在 AstrBot 插件市场搜索 `astrbot_plugin_nas`，点击安装，耐心等待安装完成即可。
- 若是安装失败，可以尝试直接克隆源码：

```bash
# 克隆仓库到插件目录
cd /AstrBot/data/plugins
git clone https://github.com/pakhozako/astrbot_plugin_nas

# 重启 AstrBot
```

## ⚙️ 配置说明

| 配置项 | 说明 | 默认值 |
|--------|------|--------|
| save_root | 文件保存根目录 | D:\NAS |
| allowed_users | 允许使用的 QQ 列表，留空则所有用户可用 | [] |
| admin_users | 管理员列表（可删除、移动） | [] |
| max_file_size | 单文件大小上限 (MB) | 2048 |
| auto_save_enabled | 启用自动保存 | true |
| auto_preview | 自动预览文件 | true |
| dedup_enabled | 启用文件去重（MD5） | true |
| delete_confirm_ttl | 删除确认超时 (秒) | 120 |
| log_enabled | 启用操作日志 | true |

## ⌨️ 使用说明

### 自动功能

- 私聊发送文件、图片、视频，插件自动按扩展名分类保存。
- 自动去重，相同文件不重复保存。
- 保存后回复确认信息，包含文件路径。

### 文件分类

| 分类 | 扩展名 |
|------|--------|
| Images | jpg, png, gif, webp, svg, bmp, ico, tiff, heic |
| Videos | mp4, mkv, avi, mov, flv, wmv, webm, ts |
| Music | mp3, flac, wav, aac, ogg, wma, m4a, opus |
| Documents | pdf, doc, xls, ppt, txt, md, csv, json, xml, yaml |
| Archives | zip, rar, 7z, tar, gz, bz2, xz, zst |
| Others | 以上都不匹配的文件 |

### 指令

进行文件管理操作，指令如下：

```
/ls [路径]      - 查看目录内容，不带参数默认显示根目录
/get 文件名     - 发送指定文件，支持跨分类搜索
/search 关键词  - 按文件名搜索
/rm 文件名      - 删除文件（需二次确认，120秒超时）
/mv 源 目标     - 移动或重命名文件
/du             - 查看磁盘空间和文件统计
/nas            - 显示帮助信息
```

安全说明：
- `/rm` 删除需要回复「确认删除」才会执行，回复「取消」放弃。
- 管理员和普通用户权限分离，删除和移动仅限管理员。
- 所有路径操作校验在根目录下，防止路径穿越。

## 🤝 可能用途

- [x] 私聊发文件自动归档到本地磁盘或 NAS。
- [x] 通过 QQ 指令远程管理文件（查看、搜索、发送、删除）。
- [x] 作为轻量级私人文件管理入口，替代复杂的 NAS 客户端。
- [ ] 后续计划：文件版本管理、HTTP 临时分享链接、Web 管理面板。

## 👥 贡献指南

- 🌟 Star 这个项目！
- 🐛 提交 Issue 报告问题
- 💡 提出新功能建议
- 🔧 提交 Pull Request 改进代码

## 📌 注意事项

- 重要：删除操作会要求二次确认，但仍请三思。
- 默认根目录为 `D:\NAS`，可自行修改配置。
- Docker 用户请提前挂载好目标目录。
- 本插件仅供学习交流使用，如有需要可 QQ 联系：2413474391。

## 📝 更新日志

### v1.0.0 (2026-06-20)
- 初始版本发布
- 私聊文件自动接收、自动分类
- 文件去重（MD5）
- 目录浏览、文件搜索、发送、删除（二次确认）、移动
- 磁盘空间统计
- 操作日志记录
- 路径安全校验

## 📄 License

[MIT](LICENSE)
