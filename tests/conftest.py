"""Shared test setup.

Every test runs with its own empty per-machine folders, so no test can read or
change the real ones:

- ``MES_DATA_HOME``, ``MES_STATE_HOME``, ``MES_CONFIG`` point into ``tmp_path``.
- ``XDG_DATA_HOME``, ``XDG_STATE_HOME``, ``XDG_CONFIG_HOME`` point into
  ``tmp_path`` too (this also covers systemd user units).
- ``MES_ALLOW`` and ``MES_DEBUG`` are removed.

Fixture ``mes_home`` returns a dict with the paths: ``root``, ``data``,
``state``, ``config`` (the config file path) and ``xdg_config``.
"""

from __future__ import annotations

import pytest


@pytest.fixture(autouse=True)
def mes_home(tmp_path, monkeypatch):
    root = tmp_path / "machine"
    homes = {
        "root": root,
        "data": root / "data",
        "state": root / "state",
        "config": root / "config" / "mes" / "config.toml",
        "xdg_config": root / "config",
    }
    monkeypatch.setenv("MES_DATA_HOME", str(homes["data"]))
    monkeypatch.setenv("MES_STATE_HOME", str(homes["state"]))
    monkeypatch.setenv("MES_CONFIG", str(homes["config"]))
    monkeypatch.setenv("XDG_DATA_HOME", str(root / "xdg-data"))
    monkeypatch.setenv("XDG_STATE_HOME", str(root / "xdg-state"))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(homes["xdg_config"]))
    monkeypatch.delenv("MES_ALLOW", raising=False)
    monkeypatch.delenv("MES_DEBUG", raising=False)
    return homes
