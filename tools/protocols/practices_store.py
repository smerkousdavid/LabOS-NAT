"""Wetlab practices data store.

Loads practice/reagent data from data/wetlab-practices.json and provides
fuzzy lookup by name, alias, or keyword.
"""

import difflib
import json
import os
from pathlib import Path
from typing import Any, Dict, List, Optional

from loguru import logger

_DATA_DIR = Path(__file__).resolve().parent.parent.parent / "data"
_PRACTICES: List[Dict[str, Any]] = []
_NAME_TO_ITEM: Dict[str, Dict[str, Any]] = {}
_NAME_CHOICES: List[str] = []
_loaded = False


def _ensure_loaded():
    global _loaded
    if _loaded:
        return
    _loaded = True
    fpath = _DATA_DIR / "wetlab-practices.json"
    if not fpath.exists():
        logger.warning(f"[Practices] Data file not found: {fpath}")
        return
    try:
        with open(fpath, "r", encoding="utf-8") as f:
            data = json.loads(f.read())
        if isinstance(data, list):
            _PRACTICES.extend(data)
        elif isinstance(data, dict):
            _PRACTICES.extend(data.get("practices", data.get("items", [data])))
        for item in _PRACTICES:
            _add_choice(item.get("name", ""), item)
            for a in item.get("aliases", []):
                _add_choice(a, item)
            for kw in item.get("keywords", []):
                _add_choice(kw, item)
        logger.info(f"[Practices] Loaded {len(_PRACTICES)} items, {len(_NAME_CHOICES)} lookup keys")
    except Exception as exc:
        logger.warning(f"[Practices] Failed to load data: {exc}")


def _add_choice(label: str, item: Dict[str, Any]):
    k = (label or "").strip().lower()
    if not k:
        return
    if k not in _NAME_TO_ITEM:
        _NAME_TO_ITEM[k] = item
    _NAME_CHOICES.append(label)


def best_match(query: str, cutoff: float = 0.6) -> Optional[str]:
    _ensure_loaded()
    q = (query or "").strip().lower()
    if not q:
        return None
    if q in _NAME_TO_ITEM:
        return q
    matches = difflib.get_close_matches(q, list(_NAME_TO_ITEM.keys()), n=1, cutoff=cutoff)
    return matches[0] if matches else None


def get_practice_steps(name: str) -> Dict[str, Any]:
    """Return procedure steps + safety + media for a lab practice."""
    _ensure_loaded()
    matched = best_match(name)
    if not matched:
        return {"found": False, "query": name}
    item = _NAME_TO_ITEM[matched]
    procedure = item.get("procedure") or {}
    safety = item.get("safety") or {}
    return {
        "found": True,
        "match": matched,
        "name": item.get("name", matched),
        "goal": procedure.get("goal", ""),
        "steps": procedure.get("steps", []),
        "ppe": safety.get("ppe", []),
        "safety_notes": safety.get("notes", ""),
        "hazards": item.get("hazards", []),
        "media": item.get("media", []),
    }


def list_practices() -> List[str]:
    """Return names of all available practices."""
    _ensure_loaded()
    return [item.get("name", "") for item in _PRACTICES if item.get("name")]
