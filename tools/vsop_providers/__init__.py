"""VSOP Provider abstraction layer.

Defines the base interface for all VSOP (Visual Step-by-Step Guided Workflow)
providers, shared data types, a factory for instantiation, and the global
singleton accessor.

Supported providers:
  - stella               (StellaVSOPProvider)  -- STELLA-VLM based protocol monitoring
  - gemini_custom_manage (GeminiVLMProvider)   -- Gemini polling-based (generate_content + chat)
"""

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Callable, Coroutine, Dict, List, Optional

from loguru import logger


# ---------------------------------------------------------------------------
# Data types
# ---------------------------------------------------------------------------

class StepState(str, Enum):
    NOT_STARTED = "NOT_STARTED"
    STARTED = "STARTED"
    COMPLETED = "COMPLETED"
    ERROR = "ERROR"
    PAUSED = "PAUSED"


@dataclass
class StepEvent:
    """Emitted by a provider whenever a step changes state."""
    step_num: int
    total_steps: int
    state: StepState
    step_text: str
    message: str
    error_detail: Optional[str] = None

    def __str__(self) -> str:
        base = f"Step {self.step_num}/{self.total_steps} [{self.state.value}]: {self.message}"
        if self.error_detail:
            base += f" | ERROR: {self.error_detail}"
        return base


StepEventCallback = Callable[[StepEvent], Coroutine[Any, Any, None]]


# ---------------------------------------------------------------------------
# Abstract provider
# ---------------------------------------------------------------------------

class VSOPProvider(ABC):
    """Base class for all VSOP providers."""

    def __init__(self, config: Dict[str, Any]):
        self._config = config
        self._on_step_event: Optional[StepEventCallback] = None
        self._active = False
        self._protocol_name: Optional[str] = None
        self._steps: List[str] = []
        self._current_step: int = 0
        self._completed_steps: List[int] = []
        self._session_id: Optional[str] = None

    # -- lifecycle -----------------------------------------------------------

    @abstractmethod
    async def start(
        self,
        protocol_name: Optional[str] = None,
        protocol_steps: Optional[List[str]] = None,
        protocol_context: Optional[str] = None,
    ) -> str:
        """Start monitoring.  Returns a human-readable confirmation string."""

    @abstractmethod
    async def stop(self) -> str:
        """Stop monitoring.  Returns a human-readable confirmation string."""

    @abstractmethod
    async def get_status(self) -> Dict[str, Any]:
        """Return a status dict (active, current step, etc.)."""

    @abstractmethod
    async def get_current_step(self) -> str:
        """Return a human-readable description of the current step."""

    # -- ad-hoc query (optional) --------------------------------------------

    async def query(self, question: str, frames: Optional[List[str]] = None) -> str:
        return "Ad-hoc queries are not supported by this provider."

    # -- manual navigation ---------------------------------------------------

    async def manual_advance(self) -> str:
        if not self._active:
            return "No active protocol."
        if self._current_step > len(self._steps):
            return "Already completed all steps."
        self._completed_steps.append(self._current_step)
        old_text = self._steps[self._current_step - 1]
        await self._emit(StepEvent(
            step_num=self._current_step,
            total_steps=len(self._steps),
            state=StepState.COMPLETED,
            step_text=old_text,
            message=f"Completed step {self._current_step}",
        ))
        self._current_step += 1
        if self._current_step <= len(self._steps):
            new_text = self._steps[self._current_step - 1]
            await self._emit(StepEvent(
                step_num=self._current_step,
                total_steps=len(self._steps),
                state=StepState.STARTED,
                step_text=new_text,
                message=f"Step {self._current_step}: {new_text}",
            ))
            return f"Step {self._current_step}: {new_text}"
        else:
            await self._emit(StepEvent(
                step_num=len(self._steps),
                total_steps=len(self._steps),
                state=StepState.COMPLETED,
                step_text=old_text,
                message="All steps completed! Protocol finished.",
            ))
            self._active = False
            return "All steps completed! Protocol finished."

    async def manual_retreat(self) -> str:
        if not self._active:
            return "No active protocol."
        if self._current_step <= 1:
            return "Already on the first step."
        self._current_step -= 1
        if self._current_step in self._completed_steps:
            self._completed_steps.remove(self._current_step)
        step_text = self._steps[self._current_step - 1]
        await self._emit(StepEvent(
            step_num=self._current_step,
            total_steps=len(self._steps),
            state=StepState.STARTED,
            step_text=step_text,
            message=f"Step {self._current_step}: {step_text}",
        ))
        return f"Step {self._current_step}: {step_text}"

    async def manual_goto(self, step_num: int) -> str:
        if not self._active:
            return "No active protocol."
        if step_num < 1 or step_num > len(self._steps):
            return f"Invalid step number. Protocol has {len(self._steps)} steps."
        self._current_step = step_num
        self._completed_steps = [i for i in range(1, step_num)]
        step_text = self._steps[step_num - 1]
        await self._emit(StepEvent(
            step_num=step_num,
            total_steps=len(self._steps),
            state=StepState.STARTED,
            step_text=step_text,
            message=f"Step {step_num}: {step_text}",
        ))
        return f"Jumped to step {step_num}: {step_text}"

    async def manual_restart(self) -> str:
        if not self._active:
            return "No active protocol."
        self._current_step = 1
        self._completed_steps = []
        step_text = self._steps[0]
        await self._emit(StepEvent(
            step_num=1,
            total_steps=len(self._steps),
            state=StepState.STARTED,
            step_text=step_text,
            message=f"Restarting protocol from step 1: {step_text}",
        ))
        return f"Restarted protocol. Now on step 1: {step_text}"

    # -- event callback ------------------------------------------------------

    def set_on_step_event(self, callback: StepEventCallback):
        self._on_step_event = callback

    async def _emit(self, event: StepEvent):
        logger.info(f"VSOP event: {event}")
        if self._on_step_event:
            try:
                await self._on_step_event(event)
            except Exception as exc:
                logger.error(f"Error in step-event callback: {exc}")

    # -- helpers -------------------------------------------------------------

    @property
    def is_active(self) -> bool:
        return self._active

    @property
    def protocol_name(self) -> Optional[str]:
        return self._protocol_name

    def bind_session(self, session_id: str) -> None:
        """Bind this provider instance to a concrete NAT websocket session."""
        self._session_id = session_id

    def get_bound_session_id(self) -> str:
        """Return the bound session id, falling back to current context var."""
        if self._session_id:
            return self._session_id
        from config import _current_session_id
        return _current_session_id.get("default-xr-session")


# ---------------------------------------------------------------------------
# Factory
# ---------------------------------------------------------------------------

class VSOPProviderFactory:
    """Create the appropriate VSOPProvider from a config dict."""

    @staticmethod
    def create(config: Dict[str, Any]) -> VSOPProvider:
        gemini_cfg = config.get("gemini_custom_manage", {})
        if gemini_cfg.get("enabled", False):
            from tools.vsop_providers.gemini_vlm import GeminiVLMProvider
            return GeminiVLMProvider(config)

        vsop_cfg = config.get("vsop_provider", {})
        provider_name = vsop_cfg.get("provider", "stella")

        if provider_name == "stella":
            from tools.vsop_providers.stella import StellaVSOPProvider
            return StellaVSOPProvider(config)
        else:
            raise ValueError(f"Unknown VSOP provider: {provider_name}. Supported: 'stella', 'gemini_custom_manage'.")


# ---------------------------------------------------------------------------
# Per-session providers
# ---------------------------------------------------------------------------

_vsop_providers: Dict[str, VSOPProvider] = {}


def get_vsop_provider() -> Optional[VSOPProvider]:
    """Get the VSOP provider for the current session (via contextvar)."""
    from config import _current_session_id
    sid = _current_session_id.get("default-xr-session")
    return _vsop_providers.get(sid)


def get_vsop_provider_for_session(session_id: str) -> Optional[VSOPProvider]:
    return _vsop_providers.get(session_id)


def set_vsop_provider(provider: VSOPProvider, session_id: str | None = None):
    from config import _current_session_id
    sid = session_id or _current_session_id.get("default-xr-session")
    _vsop_providers[sid] = provider


def init_vsop_provider(config: Dict[str, Any]) -> VSOPProvider:
    """Create and return a VSOP provider for the current session."""
    from config import _current_session_id
    sid = _current_session_id.get("default-xr-session")
    return init_vsop_provider_for_session(sid, config)


def init_vsop_provider_for_session(session_id: str, config: Dict[str, Any]) -> VSOPProvider:
    """Create, store, and return a VSOP provider for a specific session."""
    if session_id not in _vsop_providers:
        provider = VSOPProviderFactory.create(config)
        provider.bind_session(session_id)
        _vsop_providers[session_id] = provider
    return _vsop_providers[session_id]
