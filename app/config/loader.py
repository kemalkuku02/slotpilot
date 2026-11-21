import os
from pathlib import Path
import re
import tempfile

import yaml

from app.config.schema import BusinessConfig


PROJECT_ROOT = Path(__file__).resolve().parents[2]


def config_path(name: str | None = None, *, writable: bool = False) -> Path:
    config_name = name or os.getenv("BUSINESS_CONFIG", "clinic")
    if not re.fullmatch(r"[a-z0-9_-]+", config_name):
        raise ValueError("business config name must contain only lowercase letters, digits, _ or -")
    defaults = PROJECT_ROOT / "configs" / f"{config_name}.yaml"
    override_dir = Path(os.getenv("BUSINESS_CONFIG_DIR", PROJECT_ROOT / "instance" / "configs")).resolve()
    override = override_dir / f"{config_name}.yaml"
    return override if writable or override.is_file() else defaults


def load_config(path: str | Path) -> BusinessConfig:
    source = Path(path).resolve()
    data = yaml.safe_load(source.read_text(encoding="utf-8-sig"))
    if not isinstance(data, dict):
        raise ValueError(f"configuration {source} must contain a YAML object")
    return BusinessConfig.model_validate(data)


def get_config() -> BusinessConfig:
    return load_config(config_path())


def save_config(config: BusinessConfig, name: str | None = None) -> Path:
    target = config_path(name, writable=True)
    target.parent.mkdir(parents=True, exist_ok=True)
    data = config.model_dump(mode="python")
    data["specialists"] = [
        {
            "id": specialist.id,
            "name": specialist.name,
            "schedule": {
                day: [{"start": interval.start.strftime("%H:%M"), "end": interval.end.strftime("%H:%M")}
                      for interval in intervals]
                for day, intervals in specialist.schedule.items()
            },
        }
        for specialist in config.specialists
    ]
    handle = tempfile.NamedTemporaryFile(
        mode="w", encoding="utf-8", newline="\n", prefix=f".{target.stem}.", suffix=".tmp",
        dir=target.parent, delete=False,
    )
    temporary = Path(handle.name)
    try:
        with handle:
            yaml.safe_dump(data, handle, allow_unicode=True, sort_keys=False)
        os.replace(temporary, target)
    finally:
        if temporary.exists():
            temporary.unlink()
    return target
