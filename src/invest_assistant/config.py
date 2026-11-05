"""配置加载：config/config.yaml（非密钥，版本化）+ .env（密钥，gitignore）。"""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path
from typing import Any

import yaml
from pydantic_settings import BaseSettings, SettingsConfigDict

ROOT = Path(__file__).resolve().parents[2]


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=str(ROOT / ".env"),
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # 密钥与敏感配置（.env）
    deepseek_api_key: str = ""
    smtp_host: str = ""
    smtp_port: int = 465
    smtp_user: str = ""
    smtp_password: str = ""
    notify_email_to: str = ""
    serverchan_sendkey: str = ""
    tushare_token: str = ""
    panel_password: str = ""
    session_secret: str = "invest-assistant-local-dev"

    # 路径
    root_dir: Path = ROOT
    data_dir: Path = ROOT / "data"
    logs_dir: Path = ROOT / "logs"
    config_file: Path = ROOT / "config" / "config.yaml"

    @property
    def override_file(self) -> Path:
        """「设置」页保存的用户覆盖配置（data/overrides.yaml，随数据目录，gitignore）。"""
        return self.data_dir / "overrides.yaml"

    def app_config(self) -> dict[str, Any]:
        """读取 config.yaml 并叠加用户覆盖（data/overrides.yaml）。"""
        data: dict[str, Any] = {}
        if self.config_file.exists():
            data = yaml.safe_load(self.config_file.read_text(encoding="utf-8")) or {}
        override: dict[str, Any] = {}
        if self.override_file.exists():
            try:
                override = yaml.safe_load(self.override_file.read_text(encoding="utf-8")) or {}
            except Exception:  # noqa: BLE001 — 覆盖文件损坏时退回主配置
                override = {}
        return deep_merge(data, override)

    def ensure_dirs(self) -> None:
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.logs_dir.mkdir(parents=True, exist_ok=True)


@lru_cache
def get_settings() -> Settings:
    return Settings()


def deep_merge(base: dict[str, Any] | None, extra: dict[str, Any] | None) -> dict[str, Any]:
    """递归合并配置（extra 覆盖 base；同层 dict 合并，其他类型直接覆盖）。"""
    out: dict[str, Any] = dict(base or {})
    for key, value in (extra or {}).items():
        if isinstance(value, dict) and isinstance(out.get(key), dict):
            out[key] = deep_merge(out[key], value)
        else:
            out[key] = value
    return out
