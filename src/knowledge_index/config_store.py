"""Atomic JSON persistence for non-secret appliance configuration.

Precedence is ``environment > saved file > defaults``.

Why that way round, and why it is the safe choice for the security settings in
particular. The environment is the deployment contract: it lives in the firm's
docker-compose / systemd unit / Helm values, it is reviewed and version-controlled, and
it is the only lever an operator has *before* the appliance is running. The file is
runtime admin state written through the admin API by whoever is logged in today.

Until this rule existed the file won, silently and totally: one ``PUT /api/config``
wrote ``/data/config.json`` and from that moment every ``KI_*`` variable was ignored.
An operator could set ``KI_SECURITY__AUTH_MODE=oidc``, restart, see no change, and get
no hint why — and a config file written months earlier could hold a deployment on
``trusted_header`` or on ``mcp_allow_trusted_header: true`` forever. A stale file must
never be able to lower a posture the current deployment declares.

Three properties keep it honest rather than merely inverted:

* the environment only wins for settings it actually names, so everything else stays
  editable at runtime and running workers still pick changes up without a restart;
* a save that would change an environment-pinned setting is refused, by name, instead
  of being written and then quietly ignored on the next read;
* an environment-pinned setting is never written into the file at all, so it cannot
  outlive the deployment that set it — remove the variable and the value falls back to
  the code default, not to a ghost nobody chose.
"""

from __future__ import annotations

import json
import logging
import os
import tempfile
from pathlib import Path
from typing import Any

from pydantic import BaseModel
from pydantic_settings import EnvSettingsSource

from knowledge_index.config import AppConfig

log = logging.getLogger(__name__)

_MISSING = object()

# Values never echoed back through the precedence API. Everything here is already
# admin-only, but a shared secret has no business appearing in a "where does this come
# from" listing — the question is which source wins, not what the value is.
_REDACTED_PATH_MARKERS = ("secret", "password", "token")


class EnvironmentPinnedSetting(RuntimeError):
    """A save tried to change a setting the environment owns.

    Carries the dotted setting paths and the variables that pin them so the caller can
    tell the operator exactly which line of their compose file is in charge."""

    def __init__(self, paths: list[str]) -> None:
        self.paths = paths
        named = ", ".join(f"{path} ({_env_var_for(path)})" for path in paths)
        super().__init__(
            "these settings are pinned by the environment of this deployment and cannot "
            f"be changed from the admin API: {named}. Change the variable and restart, "
            "or unset it to hand the setting back to the admin API."
        )


class ConfigStore:
    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self._config: AppConfig | None = None
        self._mtime_ns: int | None = None
        self._environment: dict[str, Any] = {}
        self._pinned_paths: list[str] = []
        self._file_data: dict[str, Any] = {}

    def get(self) -> AppConfig:
        """Return the effective config, reloading if the file changed on disk.

        The app and the Hatchet worker run in separate processes with separate
        ConfigStore instances but share ``KI_CONFIG_PATH``. The worker resolves the
        config once per task via this getter, so it must observe admin edits (model
        assignments, stage toggles, the reindex target) written by the app — hence the
        mtime check rather than a permanent in-process cache. The environment is
        re-applied on every reload, so it can never be shadowed by a later save."""
        mtime_ns = self.path.stat().st_mtime_ns if self.path.exists() else None
        if self._config is None or mtime_ns != self._mtime_ns:
            self._config = self._load(mtime_ns)
            self._mtime_ns = mtime_ns
        return self._config

    def save(self, config: AppConfig) -> None:
        """Persist admin-owned settings; refuse to overwrite what the environment pins.

        A caller that submits the config it just read back unchanged in the pinned
        fields — which is what the admin UI does — saves normally. One that genuinely
        tries to change a pinned setting is told which variable owns it rather than
        having the write accepted and then ignored at the next read."""
        self.get()  # refresh the environment/file snapshot this check is made against
        submitted = config.model_dump(mode="json")
        effective = self._config.model_dump(mode="json") if self._config else {}
        conflicts = [
            path
            for path in self._pinned_paths
            if _value_at(submitted, path) != _value_at(effective, path)
        ]
        if conflicts:
            raise EnvironmentPinnedSetting(conflicts)

        # Pinned settings are stripped, not stored: the file then holds only choices an
        # administrator actually made, and unsetting a variable falls back to the code
        # default instead of resurrecting whatever the environment happened to hold when
        # somebody last pressed save.
        payload = _without_paths(submitted, self._pinned_paths)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fd, temp_name = tempfile.mkstemp(prefix="config-", suffix=".json", dir=self.path.parent)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                handle.write(json.dumps(payload, indent=2, ensure_ascii=False))
                handle.flush()
                os.fsync(handle.fileno())
            Path(temp_name).replace(self.path)
        except Exception:
            Path(temp_name).unlink(missing_ok=True)
            raise
        # Rebuild from what is actually on disk plus the environment rather than trusting
        # the submitted object: the effective config is the merge, and only the merge.
        self._mtime_ns = self.path.stat().st_mtime_ns
        self._config = self._load(self._mtime_ns)

    def precedence(self) -> dict:
        """Where each setting's value comes from, for an operator who has to know.

        Without this the rule is invisible: an administrator edits a field, it does not
        move, and nothing says an environment variable is holding it."""
        # The effective value, not the raw variable: the environment gives every setting
        # as a string, and an operator comparing "false" against false is one more thing
        # to be confused by.
        effective = self.get().model_dump(mode="json")
        entries = []
        for path in self._pinned_paths:
            file_value = _value_at(self._file_data, path)
            entries.append(
                {
                    "path": path,
                    "env_var": _env_var_for(path),
                    "value": _display(path, _value_at(effective, path)),
                    "shadows_file": file_value is not _MISSING,
                    "file_value": (
                        None if file_value is _MISSING else _display(path, file_value)
                    ),
                }
            )
        return {
            "rule": "environment > saved file > defaults",
            "config_path": str(self.path),
            "config_file_exists": self.path.exists(),
            "environment": entries,
        }

    def _load(self, mtime_ns: int | None) -> AppConfig:
        file_data: dict[str, Any] = {}
        if mtime_ns is not None:
            loaded = json.loads(self.path.read_text(encoding="utf-8"))
            if not isinstance(loaded, dict):
                raise ValueError(f"{self.path} does not contain a configuration object")
            file_data = loaded
        environment = _environment_settings()
        merged, pinned = _merge(AppConfig, file_data, environment)
        self._file_data = file_data
        self._environment = environment
        self._pinned_paths = pinned
        shadowed = [path for path in pinned if _value_at(file_data, path) is not _MISSING]
        if shadowed:
            # Loud on purpose: this is the moment the saved file stops applying, and the
            # operator has to be able to find out from the log alone.
            log.warning(
                "environment overrides %s from %s: %s",
                "a saved setting" if len(shadowed) == 1 else f"{len(shadowed)} saved settings",
                self.path,
                ", ".join(f"{path} <- {_env_var_for(path)}" for path in shadowed),
            )
        elif pinned:
            log.info(
                "settings pinned by the environment: %s",
                ", ".join(f"{path} <- {_env_var_for(path)}" for path in pinned),
            )
        return AppConfig.model_validate(merged)


def _environment_settings() -> dict[str, Any]:
    """The ``KI_*`` settings the environment actually declares, as a nested dict.

    Read through pydantic-settings' own environment source, so nesting, casing and
    JSON-valued fields behave exactly as they do when AppConfig is built from the
    environment alone. Settings the environment does not name are absent — that is what
    makes "the environment wins" mean "for the settings it really sets"."""
    return EnvSettingsSource(AppConfig)()


def _merge(
    model: type[BaseModel],
    file_data: dict[str, Any],
    environment: dict[str, Any],
    prefix: str = "",
) -> tuple[dict[str, Any], list[str]]:
    """Overlay the environment onto the file, returning the merge and the pinned paths.

    Recursion follows the *model*, not the data: a nested settings section merges field
    by field (so ``KI_SECURITY__AUTH_MODE`` leaves the other security settings alone),
    while a dict-valued field such as ``security.principal_aliases`` is replaced whole,
    because half a mapping from each source is a mapping nobody wrote."""
    merged = dict(file_data)
    pinned: list[str] = []
    for name, value in environment.items():
        path = f"{prefix}{name}"
        annotation = model.model_fields[name].annotation if name in model.model_fields else None
        nested = isinstance(annotation, type) and issubclass(annotation, BaseModel)
        if nested and isinstance(value, dict):
            below = merged.get(name)
            child, child_pinned = _merge(
                annotation, below if isinstance(below, dict) else {}, value, f"{path}."
            )
            merged[name] = child
            pinned.extend(child_pinned)
        else:
            merged[name] = value
            pinned.append(path)
    return merged, pinned


def _value_at(data: Any, path: str) -> Any:
    for part in path.split("."):
        if not isinstance(data, dict) or part not in data:
            return _MISSING
        data = data[part]
    return data


def _without_paths(data: dict[str, Any], paths: list[str]) -> dict[str, Any]:
    result = json.loads(json.dumps(data))  # deep copy of an already JSON-safe payload
    for path in paths:
        parts = path.split(".")
        cursor = result
        for part in parts[:-1]:
            cursor = cursor.get(part) if isinstance(cursor, dict) else None
            if cursor is None:
                break
        if isinstance(cursor, dict):
            cursor.pop(parts[-1], None)
    return result


def _env_var_for(path: str) -> str:
    """The variable that declares this setting — the one that actually exists if a
    parent object was set as a whole (``KI_SECURITY='{...}'``), else the canonical name."""
    prefix = str(AppConfig.model_config.get("env_prefix") or "")
    delimiter = str(AppConfig.model_config.get("env_nested_delimiter") or "__")
    present = {name.casefold(): name for name in os.environ}
    parts = path.split(".")
    for cut in range(len(parts), 0, -1):
        candidate = f"{prefix}{delimiter.join(parts[:cut])}"
        match = present.get(candidate.casefold())
        if match is not None:
            return match
    return f"{prefix}{delimiter.join(parts)}".upper()


def _display(path: str, value: Any) -> Any:
    if value is _MISSING:
        return None
    leaf = path.rsplit(".", 1)[-1].casefold()
    if any(marker in leaf for marker in _REDACTED_PATH_MARKERS):
        return "***" if value not in (None, "") else value
    return value
