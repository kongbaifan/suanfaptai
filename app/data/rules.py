import json
from functools import lru_cache
from pathlib import Path
from typing import Any


RULES_PATH = Path(__file__).with_name("rules.json")


@lru_cache(maxsize=1)
def load_rules() -> dict[str, Any]:
    with RULES_PATH.open("r", encoding="utf-8") as file:
        return dict(json.load(file))


def rule_value(path: str, default=None):
    current = load_rules()
    for part in path.split("."):
        if not isinstance(current, dict) or part not in current:
            return default
        current = current[part]
    return current

