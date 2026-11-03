from dataclasses import fields, replace
import json
import math
from pathlib import Path

from .config import TradingConfig


def load_config_experiments(path: str | Path, base_config: TradingConfig) -> list[tuple[str, TradingConfig]]:
    source = Path(path)
    try:
        document = json.loads(source.read_text(encoding="utf-8"))
    except OSError as exc:
        raise ValueError(f"Cannot read experiment file {source}: {exc}") from exc
    except json.JSONDecodeError as exc:
        raise ValueError(f"Experiment file is not valid JSON: {exc}") from exc

    if not isinstance(document, dict) or set(document) != {"experiments"}:
        raise ValueError("Experiment JSON must contain only an 'experiments' array")
    experiments = document["experiments"]
    if not isinstance(experiments, list) or not experiments:
        raise ValueError("'experiments' must be a non-empty array")

    known_fields = {field.name for field in fields(TradingConfig)}
    names = set()
    results = []
    for index, experiment in enumerate(experiments, start=1):
        if not isinstance(experiment, dict) or set(experiment) != {"name", "overrides"}:
            raise ValueError(f"Experiment {index} requires exactly 'name' and 'overrides'")
        name = experiment["name"]
        overrides = experiment["overrides"]
        if not isinstance(name, str) or not name.strip():
            raise ValueError(f"Experiment {index} name must be a non-empty string")
        name = name.strip()
        if name in names:
            raise ValueError(f"Duplicate experiment name: {name}")
        names.add(name)
        if not isinstance(overrides, dict):
            raise ValueError(f"Experiment {name}: overrides must be a JSON object")

        unknown = set(overrides) - known_fields
        if unknown:
            raise ValueError(
                f"Experiment {name}: unknown TradingConfig fields: {', '.join(sorted(unknown))}"
            )
        for key, value in overrides.items():
            current = getattr(base_config, key)
            if isinstance(current, bool):
                valid = isinstance(value, bool)
            elif isinstance(current, int):
                valid = isinstance(value, int) and not isinstance(value, bool)
            elif isinstance(current, float):
                valid = (
                    isinstance(value, (int, float))
                    and not isinstance(value, bool)
                    and math.isfinite(value)
                )
            elif isinstance(current, str):
                valid = isinstance(value, str)
            else:
                valid = False
            if not valid:
                raise ValueError(f"Experiment {name}: invalid value for {key}")
        results.append((name, replace(base_config, **overrides)))
    return results
