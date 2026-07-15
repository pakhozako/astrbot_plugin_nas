"""Configuration normalization for NAS plugin."""

from dataclasses import dataclass
from pathlib import Path

from astrbot.api import AstrBotConfig


def _to_int(value, default: int, minimum: int | None = None, maximum: int | None = None) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        parsed = default
    if minimum is not None:
        parsed = max(minimum, parsed)
    if maximum is not None:
        parsed = min(maximum, parsed)
    return parsed


@dataclass(frozen=True)
class NASSettings:
    root: Path
    admin_users: set[str]
    admin_external_paths: bool
    simple_mode: bool
    allow_all_users: bool
    allow_group_commands: bool
    max_file_size_bytes: int
    auto_save_enabled: bool
    dedup_enabled: bool
    delete_confirm_ttl: int
    log_enabled: bool
    preview_text_chars: int
    path_import_max_files: int
    auto_repair_interval_minutes: int
    watch_interval_minutes: int
    export_max_files: int
    batch_max_files: int
    public_rate_limit_per_minute: int
    public_file_recall_minutes: int
    rebuild_busy_timeout_seconds: int
    public_read_dir: str
    seven_zip_path: str
    categories_raw: str

    @classmethod
    def from_config(cls, config: AstrBotConfig | dict | None) -> "NASSettings":
        cfg = config or {}
        save_root = cfg.get("save_root") or str(Path("data/plugin_data/astrbot_plugin_nas"))
        return cls(
            root=Path(save_root).resolve(),
            admin_users={str(u) for u in cfg.get("admin_users", [])},
            admin_external_paths=bool(cfg.get("admin_external_paths", True)),
            simple_mode=bool(cfg.get("simple_mode", True)),
            allow_all_users=bool(cfg.get("allow_all_users", False)),
            allow_group_commands=bool(cfg.get("allow_group_commands", False)),
            max_file_size_bytes=_to_int(cfg.get("max_file_size", 2048), 2048, 1) * 1024 * 1024,
            auto_save_enabled=bool(cfg.get("auto_save_enabled", True)),
            dedup_enabled=bool(cfg.get("dedup_enabled", True)),
            delete_confirm_ttl=_to_int(cfg.get("delete_confirm_ttl", 120), 120, 10),
            log_enabled=bool(cfg.get("log_enabled", True)),
            preview_text_chars=_to_int(cfg.get("preview_text_chars", 1200), 1200, 100),
            path_import_max_files=_to_int(cfg.get("path_import_max_files", 2000), 2000, 1),
            auto_repair_interval_minutes=_to_int(cfg.get("auto_repair_interval_minutes", 0), 0, 0),
            watch_interval_minutes=_to_int(cfg.get("watch_interval_minutes", 0), 0, 0),
            export_max_files=_to_int(cfg.get("export_max_files", 100), 100, 1),
            batch_max_files=_to_int(cfg.get("batch_max_files", 100), 100, 1),
            public_rate_limit_per_minute=_to_int(cfg.get("public_rate_limit_per_minute", 10), 10, 0),
            public_file_recall_minutes=_to_int(cfg.get("public_file_recall_minutes", 0), 0, 0),
            rebuild_busy_timeout_seconds=_to_int(cfg.get("rebuild_busy_timeout_seconds", 600), 600, 60),
            public_read_dir=str(cfg.get("public_read_dir") or "Public"),
            seven_zip_path=str(cfg.get("seven_zip_path") or r"D:\7-Zip\7z.exe"),
            categories_raw=str(cfg.get("categories", "") or ""),
        )
