from pathlib import Path

import yaml

from .models import Settings, Source


def load_config(directory: Path) -> tuple[Settings, list[Source]]:
    settings = Settings.model_validate(yaml.safe_load((directory / "settings.yaml").read_text("utf-8")))
    raw = yaml.safe_load((directory / "sources.yaml").read_text("utf-8"))
    sources = [Source.model_validate(value) for value in raw["sources"]]
    if len({source.id for source in sources}) != len(sources):
        raise ValueError("Duplicate source IDs")
    return settings, sources
