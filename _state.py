"""Per-plugin durable state under ``$HERMES_HOME/plugins/<plugin-name>/``.

Zoom's chatbot send / edit / delete APIs require an ``account_id`` routing
parameter that the ``client_credentials`` chatbot token does **not** carry. The
only reliable source is the inbound webhook payload's ``accountId`` field. We
persist the first one we see so that outbound cron pushes (which have no
inbound context) keep working after a restart. There is no
``ZOOM_ACCOUNT_ID`` configuration at all — ``account_id`` is never user-set.

State location follows the built-in ``hermes-achievements`` / ``disk-cleanup``
convention:

  * state lives under ``HERMES_HOME`` (not the plugin's source dir), so it
    survives plugin updates and reinstalls;
  * it is namespaced by the plugin's own ``name`` from ``plugin.yaml`` (read,
    not hardcoded) so plugins can't clobber each other;
  * the file is written atomically (temp file + ``os.replace``) so a crash
    mid-write can't leave a corrupt or empty ``state.json``.

This mirrors ``disk-cleanup``'s ``tracked.json`` discipline.
"""
from __future__ import annotations

import json
import logging
import os
import tempfile
from pathlib import Path
from typing import Optional

log = logging.getLogger("zoom_plugin.state")

try:
    import yaml  # PyYAML — Hermes hard dependency (parses plugin.yaml/config.yaml)

    _YAML_AVAILABLE = True
except ImportError:  # pragma: no cover — Hermes always ships PyYAML
    _YAML_AVAILABLE = False

_PLUGIN_DIR = Path(__file__).parent
_STATE_FILENAME = "state.json"


def _plugin_name() -> str:
    """This plugin's ``name`` from its own ``plugin.yaml`` (e.g. ``zoom-platform``).

    Used to namespace per-plugin state under
    ``$HERMES_HOME/plugins/<name>/`` rather than hardcoding the directory.
    Falls back to the plugin's install directory name if the manifest can't be
    parsed (PyYAML missing or plugin.yaml unreadable).
    """
    if _YAML_AVAILABLE:
        try:
            with open(_PLUGIN_DIR / "plugin.yaml") as f:
                data = yaml.safe_load(f) or {}
            name = str(data.get("name") or "").strip()
            if name:
                return name
        except Exception:  # noqa: BLE001
            log.warning(
                "[zoom] could not read name from %s — falling back to dir name",
                _PLUGIN_DIR / "plugin.yaml",
                exc_info=True,
            )
    return _PLUGIN_DIR.name or "zoom-platform"


def state_dir() -> Path:
    """``$HERMES_HOME/plugins/<plugin-name>/`` — durable per-plugin state dir."""
    home = os.environ.get("HERMES_HOME") or os.path.expanduser("~/.hermes")
    return Path(home) / "plugins" / _plugin_name()


def state_file() -> Path:
    """Full path to this plugin's ``state.json``."""
    return state_dir() / _STATE_FILENAME


def load_account_id() -> Optional[str]:
    """Read the persisted ``account_id``, or ``None`` if no state file exists yet.

    Never raises: a missing or corrupt file is treated as "nothing persisted".
    """
    path = state_file()
    try:
        with open(path) as f:
            data = json.load(f)
    except FileNotFoundError:
        return None
    except Exception:  # noqa: BLE001 — corrupt file must not crash the adapter
        log.warning("[zoom] state file %s unreadable — ignoring", path)
        return None
    return str(data.get("account_id") or "").strip() or None


def save_account_id(account_id: str) -> bool:
    """Atomically persist ``account_id`` to the state file.

    Creates the state dir if missing. Returns ``True`` on success, ``False`` on
    any I/O failure (never raises — state writes must not break inbound
    delivery).
    """
    account_id = (account_id or "").strip()
    if not account_id:
        return False
    path = state_file()
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        payload = json.dumps({"account_id": account_id}, indent=2) + "\n"
        # Temp file in the SAME dir → same filesystem → os.replace is atomic.
        fd, tmp = tempfile.mkstemp(dir=str(path.parent), suffix=".tmp")
        try:
            with os.fdopen(fd, "w") as f:
                f.write(payload)
            os.replace(tmp, path)
        except Exception:
            try:
                os.unlink(tmp)
            except OSError:
                pass
            raise
    except Exception:  # noqa: BLE001
        log.warning("[zoom] failed to persist account_id to %s", path, exc_info=True)
        return False
    log.info("[zoom] persisted account_id to %s", path)
    return True
