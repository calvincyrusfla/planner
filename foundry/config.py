"""Runtime configuration for the Foundry orchestrator.

``FoundryConfig`` holds every tunable the factory needs: where the SQLite
queue lives, how to reach the Gemma Shop Foreman, how to launch headless
Claude Code, the rate-limit budget, the anomaly thresholds the Foreman uses
for escalations, and the team layout.

All values have safe defaults, so ``FoundryConfig()`` works out of the box.
``FoundryConfig.from_env()`` overlays environment variables:

=================================  ==========================================
Environment variable               Field (default)
=================================  ==========================================
``FOUNDRY_DB_PATH``                ``db_path`` (``foundry/data/foundry.db``)
``FOUNDRY_GEMMA_BASE_URL``         ``gemma_base_url`` (``http://localhost:11434/v1``)
``FOUNDRY_GEMMA_MODEL``            ``gemma_model`` (``gemma4:e4b``); falls back
                                   to ``FOREMAN_MODEL`` (used by the setup
                                   scripts) when unset
``FOUNDRY_CLAUDE_BIN``             ``claude_bin`` (``claude``)
``FOUNDRY_RPM``                    ``rpm`` (``20``)
``FOUNDRY_TPM``                    ``tpm`` (``100000``)
``FOUNDRY_MAX_CONCURRENT``         ``max_concurrent`` (``3``)
``FOUNDRY_REVIEW_DWELL_S``         ``thresholds.review_dwell_s`` (``600``)
``FOUNDRY_STALL_S``                ``thresholds.stall_s`` (``900``)
``FOUNDRY_GATE_FAIL_QUARANTINE``   ``thresholds.gate_fail_quarantine`` (``2``)
``FOUNDRY_TPM_SPIKE_RATIO``        ``thresholds.tpm_spike_ratio`` (``0.9``)
``FOUNDRY_TEAMS``                  ``teams`` (``alpha,bravo,charlie``; comma separated)
``FOUNDRY_BUILDERS_PER_TEAM``      ``builders_per_team`` (``5``)
``FOUNDRY_BOARD_BRIDGE_MODE``      ``board_bridge_mode`` (``auto``; ``auto|off|dry``)
=================================  ==========================================

Empty environment values are treated as unset.  The rate-limit defaults are
deliberately conservative for a Claude subscription (not an API key): a few
headless sessions at once, well below the plan's usage ceiling.
"""

from __future__ import annotations

import os
from collections.abc import Mapping
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

__all__ = [
    "AnomalyThresholds",
    "BoardBridgeMode",
    "DEFAULT_DB_PATH",
    "ENV_PREFIX",
    "FoundryConfig",
]

#: Prefix shared by every Foundry environment variable.
ENV_PREFIX = "FOUNDRY_"

#: Default queue database: ``foundry/data/foundry.db`` inside the package
#: directory (absolute, so it does not depend on the current directory).
DEFAULT_DB_PATH: Path = Path(__file__).resolve().parent / "data" / "foundry.db"

BoardBridgeMode = Literal["auto", "off", "dry"]


class AnomalyThresholds(BaseModel):
    """Limits the Shop Foreman uses to detect line anomalies.

    Attributes:
        review_dwell_s: Seconds a task may wait in ``reviewing`` before a
            REVIEW_BOTTLENECK escalation (PROMOTE_PEER_REVIEWER).
        stall_s: Seconds a ticket may stay claimed with no new event before
            an AGENT_STALLED escalation (RESET_BUILDER_CONTEXT).
        gate_fail_quarantine: Deterministic gate failures on one task that
            trigger a LINE_JAM escalation (QUARANTINE_TASK).
        tpm_spike_ratio: Fraction of the configured TPM cap above which a
            TOKEN_VELOCITY_SPIKE escalation (THROTTLE_DISPATCH) fires.
    """

    model_config = ConfigDict(extra="forbid", validate_assignment=True)

    review_dwell_s: float = Field(default=600.0, gt=0)
    stall_s: float = Field(default=900.0, gt=0)
    gate_fail_quarantine: int = Field(default=2, ge=1)
    tpm_spike_ratio: float = Field(default=0.9, gt=0, le=1)


class FoundryConfig(BaseModel):
    """Top-level Foundry settings (see the module docstring for env names)."""

    model_config = ConfigDict(extra="forbid", validate_assignment=True)

    db_path: Path = Field(default=DEFAULT_DB_PATH, description="SQLite (WAL) queue file.")
    gemma_base_url: str = Field(
        default="http://localhost:11434/v1",
        description="Ollama OpenAI-compatible API base URL for the Shop Foreman.",
    )
    gemma_model: str = Field(default="gemma4:e4b", min_length=1)
    claude_bin: str = Field(
        default="claude", min_length=1, description="Headless Claude Code executable."
    )
    rpm: int = Field(default=20, ge=1, description="Claude requests per minute budget.")
    tpm: int = Field(default=100_000, ge=1, description="Claude tokens per minute budget.")
    max_concurrent: int = Field(default=3, ge=1, description="Claude calls in flight at once.")
    thresholds: AnomalyThresholds = Field(default_factory=AnomalyThresholds)
    teams: list[str] = Field(default_factory=lambda: ["alpha", "bravo", "charlie"])
    builders_per_team: int = Field(default=5, ge=1, le=5)
    board_bridge_mode: BoardBridgeMode = "auto"

    @field_validator("gemma_base_url")
    @classmethod
    def _check_base_url(cls, value: str) -> str:
        """Require an http(s) URL and drop any trailing slash."""
        value = value.strip()
        if not value.startswith(("http://", "https://")):
            raise ValueError("gemma_base_url must start with http:// or https://")
        return value.rstrip("/")

    @field_validator("teams")
    @classmethod
    def _check_teams(cls, value: list[str]) -> list[str]:
        """Teams must be a non-empty list of unique, non-blank ids."""
        cleaned = [t.strip() for t in value]
        if not cleaned:
            raise ValueError("teams must not be empty")
        if any(not t for t in cleaned):
            raise ValueError("team ids must not be blank")
        if len(set(cleaned)) != len(cleaned):
            raise ValueError("team ids must be unique")
        return cleaned

    def ensure_data_dir(self) -> Path:
        """Create the directory holding ``db_path`` if needed and return it."""
        parent = self.db_path.parent
        parent.mkdir(parents=True, exist_ok=True)
        return parent

    def builder_ids(self, team_id: str) -> list[str]:
        """Return the builder agent ids for a team, e.g. ``alpha-1`` .. ``alpha-5``."""
        return [f"{team_id}-{n}" for n in range(1, self.builders_per_team + 1)]

    def reviewer_id(self, team_id: str) -> str:
        """Return the reviewer agent id for a team (always ``<team>-6``)."""
        return f"{team_id}-6"

    @classmethod
    def from_env(
        cls, environ: Mapping[str, str] | None = None, **overrides: Any
    ) -> FoundryConfig:
        """Build a config from ``FOUNDRY_*`` environment variables.

        Args:
            environ: Mapping to read instead of ``os.environ`` (for tests).
            **overrides: Field values that win over the environment.

        Raises:
            pydantic.ValidationError: if a variable holds an invalid value
                (the error names the field).
        """
        env = os.environ if environ is None else environ

        def get(name: str) -> str | None:
            raw = env.get(ENV_PREFIX + name)
            if raw is None or not raw.strip():
                return None
            return raw.strip()

        data: dict[str, Any] = {}
        simple = {
            "DB_PATH": "db_path",
            "GEMMA_BASE_URL": "gemma_base_url",
            "GEMMA_MODEL": "gemma_model",
            "CLAUDE_BIN": "claude_bin",
            "RPM": "rpm",
            "TPM": "tpm",
            "MAX_CONCURRENT": "max_concurrent",
            "BUILDERS_PER_TEAM": "builders_per_team",
            "BOARD_BRIDGE_MODE": "board_bridge_mode",
        }
        for var, field in simple.items():
            value = get(var)
            if value is not None:
                data[field] = value.lower() if field == "board_bridge_mode" else value

        if "gemma_model" not in data:
            foreman_model = env.get("FOREMAN_MODEL", "").strip()
            if foreman_model:
                data["gemma_model"] = foreman_model

        teams = get("TEAMS")
        if teams is not None:
            data["teams"] = [t for t in (p.strip() for p in teams.split(",")) if t]

        thresholds: dict[str, Any] = {}
        for var, field in {
            "REVIEW_DWELL_S": "review_dwell_s",
            "STALL_S": "stall_s",
            "GATE_FAIL_QUARANTINE": "gate_fail_quarantine",
            "TPM_SPIKE_RATIO": "tpm_spike_ratio",
        }.items():
            value = get(var)
            if value is not None:
                thresholds[field] = value
        if thresholds:
            data["thresholds"] = thresholds

        data.update(overrides)
        return cls.model_validate(data)
