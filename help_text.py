"""User-facing command help text."""

from .constants import PLUGIN_DISPLAY_VERSION


def nas_help_text(simple_mode: bool = True) -> str:
    core = (
        f"NAS 助手 {PLUGIN_DISPLAY_VERSION}\n\n"
        "核心功能:\n"
        "/ls [路径]                         - 查看目录\n"
        "/tree [路径] [深度]                - 查看目录树\n"
        "/get 文件或绝对路径                - 发送文件\n"
        "/preview 文件                      - 预览图片和文本\n"
        "/search 关键词|--recent            - 搜索或查看最近文件\n"
        "/status                            - 空间、索引与运行状态\n\n"
        "管理:\n"
        "/add 源路径 [分类]                 - 从任意本机/NAS路径导入\n"
        "/watch list|add|rm|run             - 管理监控目录\n"
        "/rm 文件                           - 删除文件，需 /confirm\n"
        "/confirm | /cancel                 - 确认或取消删除\n"
        "/mv 源 目标路径或新文件名          - 移动或重命名文件\n"
        "/repair [vacuum]                   - 修复索引或整理数据库"
    )
    if simple_mode:
        return core
    return (
        core
        + "\n\n高级功能:\n"
        "/tag 文件 [标签...]                - 查看或修改标签\n"
        "/note 文件 [内容]                  - 查看或修改备注\n"
        "/dups [数量]                       - 重复文件审计\n"
        "/batch 选择器 tag|untag|move ...   - 批量操作\n"
        "/export 选择器 [zip]               - 导出 ZIP"
    )
