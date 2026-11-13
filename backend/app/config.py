"""Central, versioned strategy configuration. No parameters live in engine code."""
from __future__ import annotations

import copy
import hashlib
import json
import os
from pathlib import Path
from typing import Any

import yaml

DEFAULT_PATH = Path(os.environ.get("GPTA_STRATEGY_FILE",
                                   Path(__file__).resolve().parents[2] / "config" / "strategy.yaml"))

TF_SECONDS = {"M1": 60, "M5": 300, "M15": 900, "H1": 3600}
TD_INTERVAL = {"M1": "1min", "M5": "5min", "M15": "15min", "H1": "1h"}


class Cfg:
    """Dot-access, read-only view over the nested config dict."""

    def __init__(self, data: dict[str, Any]):
        self._d = data

    def __getattr__(self, k: str):
        try:
            v = self._d[k]
        except KeyError as e:
            raise AttributeError(k) from e
        return Cfg(v) if isinstance(v, dict) else v

    def __getitem__(self, k):
        return self._d[k]

    def raw(self) -> dict[str, Any]:
        return copy.deepcopy(self._d)

    def version_hash(self) -> str:
        blob = json.dumps(self._d, sort_keys=True, default=str).encode()
        return hashlib.sha256(blob).hexdigest()[:12]


def load_config(path: str | Path | None = None, overrides: dict | None = None) -> Cfg:
    data = yaml.safe_load(Path(path or DEFAULT_PATH).read_text())
    if overrides:
        _merge(data, overrides)
    return Cfg(data)


def _merge(base: dict, extra: dict) -> None:
    for k, v in extra.items():
        if isinstance(v, dict) and isinstance(base.get(k), dict):
            _merge(base[k], v)
        else:
            base[k] = v
