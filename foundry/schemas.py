"""Shared data contracts for the Foundry orchestrator.

Every Foundry module imports its message types from here: sizing directives,
context slices, builder assignments and submissions, gate results, reviewer
verdicts, Shop Foreman escalations and Claude call results.

All models are Pydantic v2 ``BaseModel`` subclasses with ``extra="forbid"``,
so an unknown key in a payload (for example a malformed Gemma or Claude JSON
reply) is rejected instead of silently ignored.
"""

from __future__ import annotations

import re
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Literal

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    field_validator,
    model_validator,
)

__all__ = [
    # constants
    "BUILDERS_FOR",
    "MAX_BUILDERS",
    "MAX_RETRIES",
    "MAX_RETRIES_LIMIT",
    "MAX_SNIPPET_LINES",
    "MAX_DIAGNOSIS_SENTENCES",
    "MAX_OBJECTIVE_SENTENCES",
    "ALERT_ID_PATTERN",
    # literals
    "Complexity",
    "GateCheck",
    "SubmissionStatus",
    "Verdict",
    "AgentRole",
    "TokensSavedSource",
    "DurationPhase",
    # enums
    "TaskState",
    "Severity",
    "EscalationCategory",
    "PrescribedAction",
    # models
    "FoundryModel",
    "SubtaskAllocation",
    "TaskSizingDirective",
    "CodeSnippet",
    "ContextSlice",
    "TaskAssignment",
    "BuilderSubmission",
    "DeterministicGateResult",
    "LineDirective",
    "ReviewerVerdict",
    "TargetEntity",
    "MetricsSnapshot",
    "ForemanEscalation",
    "ClaudeResult",
    # helpers
    "new_alert_id",
    "utc_now_iso",
    "count_sentences",
    "builders_for",
]

# ---------------------------------------------------------------------------
# Constants and literals
# ---------------------------------------------------------------------------

Complexity = Literal["S", "M", "L", "XL"]
"""Task size as judged by the Shop Foreman."""

BUILDERS_FOR: dict[str, int] = {"S": 1, "M": 2, "L": 5, "XL": 5}
"""Sizing table: how many builders a task of each complexity gets (S=1, M=2, full team=5)."""

MAX_BUILDERS: int = 5
"""Builders per team; slots are numbered 1..MAX_BUILDERS."""

MAX_RETRIES: int = 3
"""Default retries after the first attempt (``FoundryConfig.max_retries``,
``FOUNDRY_MAX_RETRIES``); the failure after the last retry quarantines the
task.  With the default 3 a ticket gets 4 attempts, so the widest context
level (attempt 3 -> level 3) is always reached before the cap."""

MAX_RETRIES_LIMIT: int = 5
"""Upper bound for the configurable retry cap (``FoundryConfig.max_retries`` is 1..5)."""

MAX_SNIPPET_LINES: int = 50
"""Maximum number of text lines in a single ``CodeSnippet``."""

MAX_OBJECTIVE_SENTENCES: int = 2
"""Maximum sentences in ``ContextSlice.objective``."""

MAX_DIAGNOSIS_SENTENCES: int = 2
"""Maximum sentences in ``ForemanEscalation.diagnosis``."""

ALERT_ID_PATTERN: str = r"^ALT-\d{8}-\d{3}$"
"""Format of ``ForemanEscalation.alert_id``: ``ALT-YYYYMMDD-NNN``."""

GateCheck = Literal["syntax", "linter", "typecheck", "tests", "schema"]
"""Kinds of deterministic gate checks run before any reviewer."""

SubmissionStatus = Literal["ready_for_review", "blocked"]
"""Status a builder reports with its submission."""

Verdict = Literal["APPROVED", "REVISE", "REJECTED"]
"""Reviewer outcomes."""

AgentRole = Literal["builder", "reviewer", "manager"]
"""Role of a Claude agent call (used by ``ClaudeClient.run`` and telemetry)."""

TokensSavedSource = Literal["pruning", "cache", "release_draft", "triage"]
"""Where Telemetry attributes saved tokens."""

DurationPhase = Literal["build", "review"]
"""Phases Telemetry times."""


def builders_for(complexity: str) -> int:
    """Return the number of builders the sizing table gives ``complexity``.

    Raises ``KeyError`` for an unknown complexity.
    """
    return BUILDERS_FOR[complexity]


# ---------------------------------------------------------------------------
# Enums
# ---------------------------------------------------------------------------


class TaskState(str, Enum):
    """Lifecycle state of a task in the Foundry queue."""

    BACKLOG = "backlog"
    SIZING = "sizing"
    BUILDING = "building"
    VALIDATING = "validating"
    REVIEWING = "reviewing"
    COMPLETE = "complete"
    QUARANTINED = "quarantined"
    FAILED = "failed"

    def __str__(self) -> str:  # pragma: no cover - trivial
        return self.value


class Severity(str, Enum):
    """Severity of a Shop Foreman escalation."""

    LOW = "LOW"
    MEDIUM = "MEDIUM"
    HIGH = "HIGH"
    CRITICAL = "CRITICAL"

    def __str__(self) -> str:  # pragma: no cover - trivial
        return self.value


class EscalationCategory(str, Enum):
    """What kind of line anomaly the Shop Foreman detected."""

    LINE_JAM = "LINE_JAM"
    REVIEW_BOTTLENECK = "REVIEW_BOTTLENECK"
    TOKEN_VELOCITY_SPIKE = "TOKEN_VELOCITY_SPIKE"
    AGENT_STALLED = "AGENT_STALLED"
    CAPACITY_REBALANCE = "CAPACITY_REBALANCE"

    def __str__(self) -> str:  # pragma: no cover - trivial
        return self.value


class PrescribedAction(str, Enum):
    """Action the Floor Manager should take for an escalation."""

    RESET_BUILDER_CONTEXT = "RESET_BUILDER_CONTEXT"
    QUARANTINE_TASK = "QUARANTINE_TASK"
    PROMOTE_PEER_REVIEWER = "PROMOTE_PEER_REVIEWER"
    THROTTLE_DISPATCH = "THROTTLE_DISPATCH"
    RESIZE_ALLOCATION = "RESIZE_ALLOCATION"
    REASSIGN_TEAM = "REASSIGN_TEAM"
    RETRY_TASK = "RETRY_TASK"
    NONE = "NONE"

    def __str__(self) -> str:  # pragma: no cover - trivial
        return self.value


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_SENTENCE_END = re.compile(r"[.!?]+(?=\s|$)")


def count_sentences(text: str) -> int:
    """Count sentences in ``text``.

    A sentence ends with ``.``, ``!`` or ``?`` followed by whitespace or the end
    of the text; trailing text without a terminator counts as one more
    sentence. Empty or whitespace-only text has 0 sentences.
    """
    stripped = text.strip()
    if not stripped:
        return 0
    ends = list(_SENTENCE_END.finditer(stripped))
    count = len(ends)
    if not ends or ends[-1].end() < len(stripped):
        count += 1
    return count


def utc_now_iso() -> str:
    """Return the current UTC time as ISO-8601 with a ``Z`` suffix (seconds precision)."""
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def new_alert_id(seq: int, now: datetime | None = None) -> str:
    """Build a ``ForemanEscalation.alert_id`` such as ``ALT-20261107-001``.

    ``seq`` is the per-day sequence number (0..999). ``now`` defaults to the
    current UTC time; an aware datetime is converted to UTC first, a naive one
    is taken to already be UTC.
    """
    if not isinstance(seq, int) or isinstance(seq, bool) or not 0 <= seq <= 999:
        raise ValueError(f"alert sequence must be an int in 0..999, got {seq!r}")
    if now is None:
        now = datetime.now(timezone.utc)
    elif now.tzinfo is not None:
        now = now.astimezone(timezone.utc)
    return f"ALT-{now:%Y%m%d}-{seq:03d}"


def _parse_utc_iso(value: str) -> datetime:
    """Parse an ISO-8601 string and require it to carry a UTC offset."""
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError as exc:
        raise ValueError(f"timestamp is not ISO-8601: {value!r}") from exc
    offset = parsed.utcoffset()
    if offset is None:
        raise ValueError(f"timestamp must include a UTC offset ('Z' or '+00:00'): {value!r}")
    if offset.total_seconds() != 0:
        raise ValueError(f"timestamp must be UTC, got offset {offset}: {value!r}")
    return parsed


# ---------------------------------------------------------------------------
# Models
# ---------------------------------------------------------------------------


class FoundryModel(BaseModel):
    """Base for every Foundry contract: unknown fields are rejected."""

    model_config = ConfigDict(extra="forbid")


class SubtaskAllocation(FoundryModel):
    """One slice of a task, handed to exactly one builder slot."""

    subtask_id: str = Field(min_length=1, description="Unique within the task, e.g. 'T-1.1'.")
    title: str = Field(min_length=1)
    acceptance_criteria: list[str] = Field(default_factory=list)
    allowed_files: list[str] = Field(
        default_factory=list, description="Repo-relative paths the builder may modify."
    )


class TaskSizingDirective(FoundryModel):
    """The Shop Foreman's sizing decision for a task.

    ``allocated_builders`` must equal ``BUILDERS_FOR[complexity]`` unless an
    ``override_reason`` is given, and there must be exactly one subtask per
    allocated builder.
    """

    task_id: str = Field(min_length=1)
    team_id: str = Field(min_length=1)
    complexity: Complexity
    allocated_builders: int = Field(ge=1, le=MAX_BUILDERS)
    rationale: str = ""
    subtasks: list[SubtaskAllocation]
    override_reason: str | None = None

    @field_validator("override_reason")
    @classmethod
    def _blank_override_is_none(cls, value: str | None) -> str | None:
        """Treat an empty/whitespace override reason as no override."""
        if value is not None and not value.strip():
            return None
        return value

    @model_validator(mode="after")
    def _check_allocation(self) -> TaskSizingDirective:
        """Enforce the sizing table, the subtask count and unique subtask ids."""
        expected = BUILDERS_FOR[self.complexity]
        if self.allocated_builders != expected and self.override_reason is None:
            raise ValueError(
                f"complexity {self.complexity} allocates {expected} builder(s), got "
                f"{self.allocated_builders}; give override_reason to deviate"
            )
        if len(self.subtasks) != self.allocated_builders:
            raise ValueError(
                f"expected {self.allocated_builders} subtask(s) (one per builder), "
                f"got {len(self.subtasks)}"
            )
        ids = [s.subtask_id for s in self.subtasks]
        if len(set(ids)) != len(ids):
            raise ValueError(f"subtask ids must be unique, got {ids}")
        return self


class CodeSnippet(FoundryModel):
    """A pruned excerpt of one file (at most ``MAX_SNIPPET_LINES`` lines of text).

    ``start_line``/``end_line`` are 1-based, inclusive, and refer to the
    original file; ``text`` may be shorter than that range because function
    bodies are replaced by ``...``.
    """

    path: str = Field(min_length=1)
    start_line: int = Field(ge=1)
    end_line: int = Field(ge=1)
    text: str

    @property
    def line_count(self) -> int:
        """Number of text lines in the snippet."""
        return len(self.text.splitlines())

    @model_validator(mode="after")
    def _check_lines(self) -> CodeSnippet:
        """Require a valid line range and at most ``MAX_SNIPPET_LINES`` text lines."""
        if self.end_line < self.start_line:
            raise ValueError(
                f"end_line ({self.end_line}) is before start_line ({self.start_line})"
            )
        if self.line_count > MAX_SNIPPET_LINES:
            raise ValueError(
                f"snippet for {self.path} has {self.line_count} lines; "
                f"max is {MAX_SNIPPET_LINES}"
            )
        return self


class ContextSlice(FoundryModel):
    """The minimal context a builder receives instead of whole files."""

    objective: str = Field(min_length=1, description="At most two sentences.")
    acceptance_criteria: list[str] = Field(default_factory=list)
    snippets: list[CodeSnippet] = Field(default_factory=list)
    source_line_count: int = Field(
        default=0, ge=0, description="Original lines considered (for tokens-saved math)."
    )

    @field_validator("objective")
    @classmethod
    def _objective_short(cls, value: str) -> str:
        """Keep the objective to ``MAX_OBJECTIVE_SENTENCES`` sentences."""
        n = count_sentences(value)
        if n == 0:
            raise ValueError("objective must not be blank")
        if n > MAX_OBJECTIVE_SENTENCES:
            raise ValueError(
                f"objective has {n} sentences; max is {MAX_OBJECTIVE_SENTENCES}"
            )
        return value

    @property
    def snippet_line_count(self) -> int:
        """Total text lines across all snippets."""
        return sum(s.line_count for s in self.snippets)


class TaskAssignment(FoundryModel):
    """What a builder is told to do for one subtask (one attempt)."""

    task_id: str = Field(min_length=1)
    team_id: str = Field(min_length=1)
    builder_id: str = Field(min_length=1, description="e.g. 'alpha-2'.")
    subtask_id: str = Field(min_length=1)
    acceptance_criteria: list[str] = Field(default_factory=list)
    allowed_files: list[str] = Field(default_factory=list)
    context: ContextSlice
    attempt: int = Field(default=0, ge=0, le=MAX_RETRIES_LIMIT)
    fix_hint: str | None = None


class BuilderSubmission(FoundryModel):
    """A builder's result for one subtask: a unified diff plus self-checks."""

    task_id: str = Field(min_length=1)
    subtask_id: str = Field(min_length=1)
    builder_id: str = Field(min_length=1)
    status: SubmissionStatus
    modified_files: list[str] = Field(default_factory=list)
    diff: str = Field(default="", description="Unified diff.")
    self_check: dict[str, bool] = Field(default_factory=dict)
    blocker_notes: str | None = None


class DeterministicGateResult(FoundryModel):
    """Outcome of one deterministic gate check on a submission."""

    task_id: str = Field(min_length=1)
    subtask_id: str = Field(min_length=1)
    passed: bool
    check_type: GateCheck
    stdout: str = ""
    stderr: str = ""
    duration_seconds: float = Field(default=0.0, ge=0)
    note: str | None = Field(
        default=None, description="Optional remark, e.g. why a check was skipped."
    )


class LineDirective(FoundryModel):
    """A reviewer instruction pinned to one line of one file."""

    path: str = Field(min_length=1)
    line: int = Field(ge=0, description="1-based line; 0 means the whole file.")
    instruction: str = Field(min_length=1)


class ReviewerVerdict(FoundryModel):
    """A reviewer's decision on a submission."""

    task_id: str = Field(min_length=1)
    reviewer_id: str = Field(min_length=1)
    verdict: Verdict
    directives: list[LineDirective] = Field(default_factory=list)
    retry_count: int = Field(default=0, ge=0, le=MAX_RETRIES_LIMIT)


class TargetEntity(FoundryModel):
    """What an escalation is about; at least one field must be set."""

    team_id: str | None = None
    task_id: str | None = None
    agent_id: str | None = None

    @model_validator(mode="after")
    def _at_least_one(self) -> TargetEntity:
        """Reject a target with no team, task or agent."""
        if not any((self.team_id, self.task_id, self.agent_id)):
            raise ValueError("target_entity needs at least one of team_id, task_id, agent_id")
        return self


class MetricsSnapshot(BaseModel):
    """Line metrics at the time of an escalation.

    The four named metrics are required; additional numeric or descriptive
    keys are allowed (this is the one contract that accepts extra fields).
    """

    model_config = ConfigDict(extra="allow")

    queue_depth: int = Field(ge=0)
    dwell_seconds: float = Field(ge=0)
    gate_failure_count: int = Field(ge=0)
    current_tpm: float = Field(ge=0)


class ForemanEscalation(FoundryModel):
    """A structured alert raised by the Shop Foreman for the Floor Manager."""

    alert_id: str = Field(pattern=ALERT_ID_PATTERN, description="ALT-YYYYMMDD-NNN")
    timestamp: str = Field(description="ISO-8601 UTC, e.g. 2026-11-07T12:00:00Z.")
    severity: Severity
    category: EscalationCategory
    target_entity: TargetEntity
    metrics_snapshot: MetricsSnapshot
    diagnosis: str = Field(min_length=1, description="At most two sentences.")
    prescribed_action: PrescribedAction
    action_parameters: dict[str, Any] = Field(default_factory=dict)

    @field_validator("timestamp", mode="before")
    @classmethod
    def _timestamp_utc(cls, value: Any) -> str:
        """Accept an aware UTC datetime or an ISO-8601 UTC string; store a string."""
        if isinstance(value, datetime):
            offset = value.utcoffset()
            if offset is None or offset.total_seconds() != 0:
                raise ValueError("timestamp datetime must be timezone-aware UTC")
            return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")
        if not isinstance(value, str):
            raise ValueError("timestamp must be an ISO-8601 UTC string")
        _parse_utc_iso(value)
        return value

    @field_validator("diagnosis")
    @classmethod
    def _diagnosis_short(cls, value: str) -> str:
        """Keep the diagnosis to ``MAX_DIAGNOSIS_SENTENCES`` sentences."""
        n = count_sentences(value)
        if n == 0:
            raise ValueError("diagnosis must not be blank")
        if n > MAX_DIAGNOSIS_SENTENCES:
            raise ValueError(
                f"diagnosis has {n} sentences; max is {MAX_DIAGNOSIS_SENTENCES}"
            )
        return value

    @property
    def timestamp_dt(self) -> datetime:
        """The timestamp as an aware UTC ``datetime``."""
        return _parse_utc_iso(self.timestamp)


class ClaudeResult(FoundryModel):
    """Normalized result of one headless Claude Code call (``claude -p --output-format json``)."""

    text: str = ""
    input_tokens: int = Field(
        default=0,
        ge=0,
        description="Uncached input tokens (including cache-creation tokens); charged to the rate limiter.",
    )
    output_tokens: int = Field(default=0, ge=0)
    cache_read_tokens: int = Field(
        default=0,
        ge=0,
        description="Input tokens served from the prompt cache; tracked separately, not charged to TPM.",
    )
    duration_seconds: float = Field(default=0.0, ge=0)
    cost_usd: float | None = None
    is_error: bool = False
    error: str | None = None

    @property
    def total_tokens(self) -> int:
        """Uncached input plus output tokens (excludes ``cache_read_tokens``)."""
        return self.input_tokens + self.output_tokens
