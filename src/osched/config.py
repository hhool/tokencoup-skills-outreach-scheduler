"""Configuration loader.

Loads YAML config with startup validation and fast failure. Real secrets are
only ever read from env files / environment variables — never stored in YAML
or committed. 中文说明：配置全部来自 YAML 文件 + 环境变量，密钥绝不入库。
"""
from __future__ import annotations

import os
import pathlib
from typing import Any

import yaml

DEFAULT_CONFIG_PATHS = (
    "config/global.yaml",
    "config/global.local.yaml",
)


class ConfigError(Exception):
    """Raised for invalid or missing configuration."""


def load_config(path: str | None = None) -> dict[str, Any]:
    """Load and validate global config. Later files override earlier ones."""
    if path:
        candidates = [path]
    else:
        candidates = [p for p in DEFAULT_CONFIG_PATHS if os.path.exists(p)]
    if not candidates:
        raise ConfigError(
            "No config found. Copy config/global.example.yaml to config/global.yaml"
        )
    merged: dict[str, Any] = {}
    for cand in candidates:
        with open(cand, "r", encoding="utf-8") as fh:
            doc = yaml.safe_load(fh) or {}
        if not isinstance(doc, dict):
            raise ConfigError(f"{cand}: top-level YAML must be a mapping")
        merged.update(doc)
    _validate(merged, os.path.dirname(candidates[0]))
    return merged


def _validate(cfg: dict[str, Any], base_dir: str) -> None:
    data_root = cfg.get("data_root")
    if not data_root:
        raise ConfigError("data_root is required (repo-local 'data/' or outside path)")
    root = pathlib.Path(data_root)
    if not root.is_absolute():
        root = pathlib.Path(base_dir).resolve().parent / data_root
    cfg["_data_root_resolved"] = str(root)

    sources = cfg.get("sources")
    if not isinstance(sources, dict) or not sources:
        raise ConfigError("sources: at least one source block is required")

    quotas = cfg.setdefault("quotas", {})
    quotas.setdefault("per_domain_per_day", 100)
    quotas.setdefault("per_project_per_day", 10)

    report = cfg.setdefault("report", {})
    report.setdefault("email", "")  # empty = disabled (by design)

    captcha = cfg.setdefault("captcha", {})
    captcha.setdefault("channel", "cua")
    captcha.setdefault("solver_api", "")
    captcha.setdefault("monthly_budget_usd", 0)

    probe = cfg.setdefault("probe", {})
    probe.setdefault("timeout_seconds", 12.0)
    probe.setdefault("candidates_per_site", 3)
    probe.setdefault("per_run_limit", 20)
    probe.setdefault("audit_days", 30)      # §4c freshness audit interval


def data_root(cfg: dict[str, Any]) -> pathlib.Path:
    """Resolved data root directory (created on demand)."""
    root = pathlib.Path(cfg["_data_root_resolved"])
    root.mkdir(parents=True, exist_ok=True)
    return root


def require_env_key(env_file: str | None, key: str) -> str:
    """Read a credential from env file (`export KEY=value` lines) or os.environ.

    中文：凭据唯一来源是环境变量 / env 文件（权限 600），绝不进入代码库。
    """
    value = os.environ.get(key, "").strip()
    if value:
        return value
    if env_file:
        path = pathlib.Path(env_file).expanduser()
        if path.exists():
            for line in path.read_text(encoding="utf-8").splitlines():
                line = line.strip()
                if line.startswith("#"):
                    continue
                for prefix in ("export ", ""):
                    if line.startswith(prefix + key + "="):
                        raw = line[len(prefix) + len(key) + 1:].strip()
                        return raw.strip("\"'")
    raise ConfigError(f"Missing credential {key} (env or {env_file})")
