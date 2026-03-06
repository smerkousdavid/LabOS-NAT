"""Protocol state management.

Central dataclass that holds ALL protocol-related state including enriched
step details, STELLA observations, error history, and display timing.
Every component (protocol tools, STELLA loop, UI manager, context manager)
reads/writes this single object.
"""

import time
from collections import defaultdict
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional


@dataclass
class StepDetail:
    """One step in a protocol with LLM-generated enrichments."""
    text: str
    description: str = ""
    common_errors: List[str] = field(default_factory=list)
    status: str = "pending"  # pending | in_progress | completed | error
    error_detail: Optional[str] = None
    robot_protocol: Optional[str] = None  # when set, auto-triggers this robot protocol on step entry


@dataclass
class ProtocolState:
    """Complete protocol lifecycle state."""

    is_active: bool = False
    mode: str = "idle"  # idle | listing | running | completed
    protocol_name: str = ""
    steps: List[StepDetail] = field(default_factory=list)
    current_step: int = 0  # 1-based index
    completed_steps: List[int] = field(default_factory=list)
    error_history: List[Dict[str, Any]] = field(default_factory=list)
    start_time: float = 0.0
    stella_vision_text: str = ""
    extra_context: str = ""
    experiment_data: Dict[str, Any] = field(default_factory=dict)
    completed_runs: List[Dict[str, Any]] = field(default_factory=list)
    data_capture_hashes: List[str] = field(default_factory=list)

    # Error display timing
    error_display_until: float = 0.0
    error_cooldown_until: float = 0.0

    # STELLA-VLM hierarchical observation memory (synced from stella.py)
    monitoring_granular: List[str] = field(default_factory=list)
    monitoring_medium: List[str] = field(default_factory=list)
    monitoring_high: List[str] = field(default_factory=list)

    def reset(self, clear_completed_runs: bool = False):
        self.is_active = False
        self.mode = "idle"
        self.protocol_name = ""
        self.steps = []
        self.current_step = 0
        self.completed_steps = []
        self.error_history = []
        self.start_time = 0.0
        self.stella_vision_text = ""
        self.extra_context = ""
        self.experiment_data = {}
        self.data_capture_hashes = []
        self.error_display_until = 0.0
        self.error_cooldown_until = 0.0
        self.monitoring_granular = []
        self.monitoring_medium = []
        self.monitoring_high = []
        if clear_completed_runs:
            self.completed_runs = []

    def elapsed_str(self) -> str:
        if self.start_time <= 0:
            return "0m 0s"
        elapsed = time.time() - self.start_time
        m, s = divmod(int(elapsed), 60)
        return f"{m}m {s}s"

    def step_texts(self) -> List[str]:
        """Return bare step text strings (for prompt builders)."""
        return [s.text for s in self.steps]

    def current_step_detail(self) -> Optional[StepDetail]:
        if 1 <= self.current_step <= len(self.steps):
            return self.steps[self.current_step - 1]
        return None

    def is_error_on_cooldown(self) -> bool:
        return time.time() < self.error_cooldown_until

    def experiment_data_xml(self) -> str:
        """Render structured experiment data into compact XML-like context."""
        if not self.experiment_data:
            return "<experiment_data>\n(none)\n</experiment_data>"

        lines: List[str] = ["<experiment_data>"]
        sections = self.experiment_data.get("sections", {})
        if not isinstance(sections, dict) or not sections:
            lines.append("(none)")
            lines.append("</experiment_data>")
            return "\n".join(lines)

        for section_name, payload in sections.items():
            tag = str(section_name or "data").replace(" ", "_")
            rows = payload.get("rows", []) if isinstance(payload, dict) else []
            headers = payload.get("headers", []) if isinstance(payload, dict) else []
            lines.append(f"<{tag}>")
            if headers and rows:
                header_row = ", ".join(str(h) for h in headers)
                lines.append(f"{header_row}")
                for row in rows:
                    vals = [str(row.get(h, "")) for h in headers] if isinstance(row, dict) else [str(row)]
                    lines.append(", ".join(vals))
            elif rows:
                for row in rows:
                    lines.append(str(row))
            lines.append(f"</{tag}>")
        lines.append("</experiment_data>")
        return "\n".join(lines)


# ---------------------------------------------------------------------------
# Per-session protocol state
# ---------------------------------------------------------------------------

_protocol_states: Dict[str, ProtocolState] = defaultdict(ProtocolState)


def get_protocol_state(session_id: str | None = None) -> ProtocolState:
    if session_id is None:
        from config import _current_session_id
        session_id = _current_session_id.get("default-xr-session")
    return _protocol_states[session_id]
