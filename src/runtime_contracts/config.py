"""Configuration management — YAML, env, defaults."""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

logger = logging.getLogger("runtime_contracts")

DEFAULT_CONFIG_DIR = Path.home() / ".config" / "contract-check"
DEFAULT_CONFIG_PATH = DEFAULT_CONFIG_DIR / "config.yaml"


@dataclass
class Config:
    """Runtime contract checker configuration."""

    default_timeout: int = 10
    report_output_dir: str = "."
    log_level: str = "WARNING"

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> Config:
        """Create a Config from a dictionary, ignoring unknown keys."""
        return cls(
            default_timeout=int(data.get("default_timeout", 10)),
            report_output_dir=str(data.get("report_output_dir", ".")),
            log_level=str(data.get("log_level", "WARNING")).upper(),
        )

    def apply_env_overrides(self) -> None:
        """Override config fields from CONTRACT_CHECK_* environment variables."""
        if v := os.environ.get("CONTRACT_CHECK_TIMEOUT"):
            try:
                self.default_timeout = max(1, int(v))
            except ValueError:
                pass
        if v := os.environ.get("CONTRACT_CHECK_REPORT_DIR"):
            self.report_output_dir = v
        if v := os.environ.get("CONTRACT_CHECK_LOG_LEVEL"):
            self.log_level = v.upper()

    def validate(self) -> list[str]:
        """Validate config values, returning a list of warnings."""
        warnings: list[str] = []
        valid_levels = {"DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"}
        if self.log_level not in valid_levels:
            warnings.append(f"Invalid log_level '{self.log_level}', using WARNING")
            self.log_level = "WARNING"
        if self.default_timeout < 1:
            warnings.append(f"default_timeout {self.default_timeout} too low, using 1")
            self.default_timeout = 1
        return warnings


def load_config(path: Path | None = None) -> Config:
    """Load configuration from YAML file + env overrides.

    Falls back to defaults if the file doesn't exist or can't be parsed.
    """
    config_path = path or DEFAULT_CONFIG_PATH
    data: dict[str, Any] = {}

    if config_path.exists():
        try:
            import yaml
            raw = yaml.safe_load(config_path.read_text(encoding="utf-8"))
            if isinstance(raw, dict):
                data = raw
                logger.debug("Loaded config from %s", config_path)
        except ImportError:
            logger.debug("PyYAML not installed, skipping config file")
        except Exception as e:
            logger.warning("Failed to parse config %s: %s", config_path, e)

    config = Config.from_dict(data)
    config.apply_env_overrides()
    warnings = config.validate()
    for w in warnings:
        logger.warning(w)
    return config
