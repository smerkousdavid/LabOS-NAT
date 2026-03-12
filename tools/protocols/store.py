"""File-based protocol database.

Scans a directory for plain-text protocol files (.txt) and provides
listing, lookup, fuzzy search, and rich-text formatting for the Viture
XR glasses display.

Protocol files can be plain numbered lists, markdown with tables/sections,
or other text-oriented formats. A lightweight parser extracts candidate
steps for display and fallback use.
"""

import difflib
import re
import time
from pathlib import Path
from typing import Dict, List, Optional

from loguru import logger

_REFRESH_TTL_SECONDS = 5.0

ALLOWED_EXTENSIONS = {".txt", ".md", ".markdown", ".csv", ".tsv", ".json", ".yaml", ".yml"}


def _is_table_header_row(line: str) -> bool:
    """Best-effort filter for markdown table header rows."""
    if "|" not in line:
        return False
    stripped = line.strip().strip("|")
    cells = [c.strip().lower() for c in stripped.split("|") if c.strip()]
    if not cells:
        return False
    header_words = {
        "item",
        "items",
        "material",
        "materials",
        "description",
        "reagent",
        "reagents",
        "volume",
        "temperature",
        "time",
        "notes",
        "step",
    }
    if any(cell in header_words for cell in cells):
        return True
    # If all cells are short alpha labels, this is likely a header row.
    alpha_cells = [c for c in cells if re.match(r"^[a-z][a-z0-9 _/-]{0,24}$", c)]
    return len(alpha_cells) == len(cells) and len(cells) >= 2


def _parse_steps(text: str) -> List[str]:
    # XML <step> extraction -- handles structured protocol formats
    xml_steps = re.findall(
        r'<step[^>]*?title="([^"]*)"[^>]*>(.*?)</step>',
        text, re.DOTALL,
    )
    if xml_steps:
        _INTRO_TITLES = {"introduction", "welcome", "overview"}
        result = []
        for title, body in xml_steps:
            if not title.strip():
                continue
            clean_body = " ".join(body.split())
            if title.strip().lower() in _INTRO_TITLES:
                result.append(clean_body)
            else:
                result.append(f"{title}: {clean_body}")
        return result

    steps: List[str] = []
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        if set(line) <= {"|", "-", ":", " "}:
            continue
        if line.startswith("#"):
            continue
        if line.lower().startswith(("**goal", "goal:", "materials", "reaction mix", "note:", "**note")):
            continue
        # Skip XML-like tags that aren't steps
        if re.match(r"^\s*</?[a-zA-Z_]", line):
            continue

        if line.startswith("|") and _is_table_header_row(line):
            continue

        table_match = re.match(r"^\|\s*(\d+)\s*\|\s*(.+?)\s*\|(?:.*\|)?\s*$", line)
        if table_match:
            desc = table_match.group(2).strip()
            if desc and desc.lower() != "description":
                steps.append(desc)
            continue

        if line.startswith("|"):
            continue

        m = re.match(r"^\d+[\.\)]\s*", line)
        if m:
            steps.append(line[m.end():].strip() or line)
        elif line.startswith(("- ", "* ")):
            bullet = line[2:].strip()
            if bullet:
                steps.append(bullet)
        elif steps:
            steps[-1] += " " + line
        else:
            steps.append(line)
    return steps


def _pretty_name(filename: str) -> str:
    stem = Path(filename).stem
    # Preserve human-authored capitalization and punctuation (e.g., PCR, STELLA, "Pilot - PCR")
    # while still making underscore-separated filenames readable.
    pretty = stem.replace("_", " ")
    return re.sub(r"\s+", " ", pretty).strip()


class ProtocolStore:
    """Manage protocol text files in a local directory."""

    def __init__(self, directory: str = "protocols"):
        self.directory = Path(directory)
        self.directory.mkdir(parents=True, exist_ok=True)
        self._cache: Dict[str, dict] = {}
        self._last_refresh: float = 0.0
        self._refresh()

    def list_protocols(self) -> List[dict]:
        self._refresh()
        return [
            {
                "name": info["name"],
                "pretty_name": info["pretty_name"],
                "steps": info["steps"],
                "step_count": len(info["steps"]),
            }
            for info in self._cache.values()
        ]

    def get_protocol(self, name: str) -> Optional[dict]:
        self._refresh()
        key = name if name in self._cache else f"{name}.txt"
        return self._cache.get(key)

    def find_protocol(self, query: str) -> Optional[dict]:
        """Fuzzy-match a protocol by user query string."""
        self._refresh()
        if not self._cache:
            return None

        query_lower = query.lower().strip()

        for key, info in self._cache.items():
            if query_lower == key.lower() or query_lower == Path(key).stem.lower():
                return info

        for info in self._cache.values():
            if query_lower in info["pretty_name"].lower():
                return info

        pretty_names = {info["pretty_name"].lower(): info for info in self._cache.values()}
        matches = difflib.get_close_matches(query_lower, pretty_names.keys(), n=1, cutoff=0.4)
        if matches:
            return pretty_names[matches[0]]

        for info in self._cache.values():
            full_text = " ".join(info["steps"]).lower()
            if query_lower in full_text:
                return info

        return None

    def format_protocol_list_for_display(self) -> List[Dict[str, str]]:
        """Build rich-text messages list for the Viture XR panel."""
        protocols = self.list_protocols()
        if not protocols:
            return [{"type": "rich-text", "content": "<size=20>No protocols available.</size>"}]

        lines = ["<size=25><b>Available Protocols</b></size><br><br>"]
        for i, proto in enumerate(protocols, 1):
            lines.append(
                f'<size=20><color=#59D2FF>{i}.</color> '
                f'{proto["pretty_name"]} '
                f'<color=#AAAAAA>({proto["step_count"]} steps)</color></size><br>'
            )
        lines.append("<br><size=18><color=#D9D8FF>Say the protocol name to start.</color></size>")
        return [{"type": "rich-text", "content": "".join(lines)}]

    def _refresh(self, force: bool = False):
        now = time.monotonic()
        if not force and self._cache and (now - self._last_refresh) < _REFRESH_TTL_SECONDS:
            return
        self._cache.clear()
        if not self.directory.exists():
            return
        for f in sorted(self.directory.iterdir()):
            if f.is_file() and f.suffix.lower() in ALLOWED_EXTENSIONS:
                try:
                    text = f.read_text(errors="replace")
                    steps = _parse_steps(text)
                    self._cache[f.name] = {
                        "name": f.name,
                        "pretty_name": _pretty_name(f.name),
                        "steps": steps,
                        "raw": text,
                    }
                except Exception as exc:
                    logger.warning(f"Failed to read protocol {f}: {exc}")
        self._last_refresh = now


# ---------------------------------------------------------------------------
# Global singleton
# ---------------------------------------------------------------------------

_protocol_store: Optional[ProtocolStore] = None


def get_protocol_store(protocols_dir: str = "protocols") -> ProtocolStore:
    global _protocol_store
    if _protocol_store is None:
        _protocol_store = ProtocolStore(protocols_dir)
    return _protocol_store


def init_protocol_store(config: dict) -> ProtocolStore:
    """Create and cache the ProtocolStore from config."""
    global _protocol_store
    if _protocol_store is None:
        protocols_dir = config.get("vsop_provider", {}).get("protocols_dir", "protocols")
        _protocol_store = ProtocolStore(protocols_dir)
    return _protocol_store


# ---------------------------------------------------------------------------
# Session-scoped protocol helpers (merge disk + in-memory)
# ---------------------------------------------------------------------------

def build_protocol_entry(name: str, steps: List[str], raw: str) -> Dict:
    """Build a protocol dict compatible with ProtocolStore cache entries."""
    return {
        "name": name,
        "pretty_name": name,
        "steps": steps,
        "step_count": len(steps),
        "raw": raw,
    }


def list_available_protocols(store: ProtocolStore, state) -> List[dict]:
    """Merge disk-backed protocols with session-scoped ones."""
    disk = store.list_protocols()
    session = []
    for key, entry in getattr(state, "session_protocols", {}).items():
        merged = dict(entry)
        merged.setdefault("name", key)
        merged.setdefault("pretty_name", key)
        merged.setdefault("step_count", len(entry.get("steps", [])))
        session.append(merged)
    return disk + session


def find_available_protocol(query: str, store: ProtocolStore, state) -> Optional[dict]:
    """Fuzzy-match across disk and session protocols."""
    found = store.find_protocol(query)
    if found:
        return found
    query_lower = query.lower().strip()
    for key, entry in getattr(state, "session_protocols", {}).items():
        pn = entry.get("pretty_name", key).lower()
        if query_lower == pn or query_lower in pn:
            return entry
    candidates = {
        entry.get("pretty_name", key).lower(): entry
        for key, entry in getattr(state, "session_protocols", {}).items()
    }
    if candidates:
        matches = difflib.get_close_matches(query_lower, candidates.keys(), n=1, cutoff=0.4)
        if matches:
            return candidates[matches[0]]
    return None


def format_protocols_for_display(store: ProtocolStore, state) -> List[Dict[str, str]]:
    """Build rich-text list merging disk + session protocols."""
    protocols = list_available_protocols(store, state)
    if not protocols:
        return [{"type": "rich-text", "content": "<size=20>No protocols available.</size>"}]
    lines = ["<size=25><b>Available Protocols</b></size><br><br>"]
    for i, proto in enumerate(protocols, 1):
        lines.append(
            f'<size=20><color=#59D2FF>{i}.</color> '
            f'{proto["pretty_name"]} '
            f'<color=#AAAAAA>({proto.get("step_count", len(proto.get("steps", [])))} steps)</color></size><br>'
        )
    lines.append("<br><size=18><color=#D9D8FF>Say the protocol name to start.</color></size>")
    return [{"type": "rich-text", "content": "".join(lines)}]
