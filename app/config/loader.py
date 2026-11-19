from functools import lru_cache
from pathlib import Path

import yaml

from app.config.schema import BusinessConfig


@lru_cache(maxsize=8)
def load_config(path: str | Path) -> BusinessConfig:
    config_path = Path(path).resolve()
    data = yaml.safe_load(config_path.read_text(encoding="utf-8-sig"))
    if not isinstance(data, dict):
        raise ValueError(f"configuration {config_path} must contain a YAML object")
    return BusinessConfig.model_validate(data)


def get_config() -> BusinessConfig:
    import os

    name = os.getenv("BUSINESS_CONFIG", "clinic")
    return load_config(Path(__file__).resolve().parents[2] / "configs" / f"{name}.yaml")
