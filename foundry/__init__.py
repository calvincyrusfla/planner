"""Foundry: the Floor Manager / Shop Foreman factory orchestrator.

Claude (headless Claude Code) plans, builds and reviews; a local Gemma model
(the Shop Foreman, served by Ollama) sizes work, prunes context and raises
escalations.  Work flows through a SQLite (WAL) queue and is reported to the
existing Agent Department dashboard.  This module stays import-light: import
the submodules you need (``foundry.config``, ``foundry.schemas`` ...).
"""

from __future__ import annotations

__all__ = ["__version__"]

__version__ = "0.1.0"
