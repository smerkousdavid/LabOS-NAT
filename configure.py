#!/usr/bin/env python3
"""Generate configs/config.yml for the NAT server from config.yaml + .env.secrets.

Usage: python configure.py
"""

import sys
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent
CONFIG_FILE = ROOT / "config.yaml"


def load_secrets(path: Path) -> dict:
    secrets = {}
    if not path.exists():
        print(f"  WARNING: {path} not found -- copy .env.secrets.example to .env.secrets")
        return secrets
    with open(path) as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            if "=" in line:
                k, _, v = line.partition("=")
                secrets[k.strip()] = v.strip()
    return secrets


def _resolve_secret(value: str, secrets: dict) -> str:
    """Replace ${VAR} in a config value with the matching secret."""
    import re
    if not isinstance(value, str):
        return value
    def _sub(m):
        return secrets.get(m.group(1), m.group(0))
    return re.sub(r"\$\{(\w+)\}", _sub, value)


def generate_nat_config(cfg: dict, secrets: dict) -> dict:
    lm = cfg.get("models", {})
    nat = cfg.get("nat", {})
    session = nat.get("session", {})
    vsop = nat.get("vsop", {})
    tools = nat.get("tools", {})
    video = nat.get("video", {})
    gemini_cm = nat.get("gemini_custom_manage", {})
    labos_live = nat.get("labos_live", {})

    fast_llm = lm.get("fast_llm", lm.get("llm", {}))
    reason_llm = lm.get("reason_llm", lm.get("llm", {}))

    return {
        "session": {
            "context_window_tokens": session.get("context_window_tokens", 6000),
            "max_turns": session.get("max_turns", 10),
            "history_limit": session.get("history_limit", 40),
            "summarize_trigger_tokens": session.get("summarize_trigger_tokens", 7000),
            "summary_target_tokens": session.get("summary_target_tokens", 600),
        },
        "llms": {
            "router": {
                "base_url": lm.get("llm", {}).get("base_url", "http://localhost:8001/v1"),
                "model": lm.get("llm", {}).get("model", "Qwen/Qwen3-32B-AWQ"),
                "api_key": lm.get("llm", {}).get("api_key", "not-needed"),
                "max_model_len": lm.get("llm", {}).get("max_model_len", 8096),
            },
            "fast_llm": {
                "base_url": fast_llm.get("base_url", "http://llm:8001/v1"),
                "model": fast_llm.get("model", "Qwen/Qwen3-32B-AWQ"),
                "api_key": _resolve_secret(fast_llm.get("api_key", "not-needed"), secrets),
            },
            "reason_llm": {
                "base_url": reason_llm.get("base_url", "https://generativelanguage.googleapis.com/v1beta/openai/"),
                "model": reason_llm.get("model", "gemini-2.5-flash"),
                "api_key": _resolve_secret(reason_llm.get("api_key", "not-needed"), secrets),
            },
            "vlm": {
                "base_url": lm.get("vlm", {}).get("base_url", "http://localhost:8500/v1"),
                "model": lm.get("vlm", {}).get("model", "Zaixi/STELLA-VLM-32b"),
                "api_key": lm.get("vlm", {}).get("api_key", "not-needed"),
                "max_model_len": lm.get("vlm", {}).get("max_model_len", 8096),
            },
        },
        "vsop_provider": {
            "provider": vsop.get("provider", "stella"),
            "protocols_dir": vsop.get("protocols_dir", "protocols"),
            "polling_interval": vsop.get("polling_interval", 5),
            "multi_frame": vsop.get("multi_frame", {
                "count": 5,
                "window_seconds": 10,
                "resolution": 512,
                "jpeg_quality": 70,
            }),
        },
        "video": {
            "mode": video.get("mode", "websocket"),
            "mediamtx_url": video.get("mediamtx_url", "rtsp://mediamtx:8554"),
        },
        "gemini_custom_manage": {
            "enabled": gemini_cm.get("enabled", True),
            "mode": gemini_cm.get("mode", "full"),
            "model": gemini_cm.get("model", "gemini-3.1-flash-lite-preview"),
            "api_key": _resolve_secret(gemini_cm.get("api_key", "${GOOGLE_API_KEY}"), secrets),
            "monitoring_frames": gemini_cm.get("monitoring_frames", 20),
            "monitoring_window_seconds": gemini_cm.get("monitoring_window_seconds", 20),
            "monitoring_interval": gemini_cm.get("monitoring_interval", 15),
            "chat_frames": gemini_cm.get("chat_frames", 5),
        },
        "labos_live": {
            "enabled": labos_live.get("enabled", False),
            "initial_qr_code": labos_live.get("initial_qr_code", False),
            "website_base_url": labos_live.get("website_base_url", ""),
        },
        "tools": tools,
        "secrets": {
            "serpapi_key": secrets.get("SERPAPI_KEY", ""),
            "google_api_key": secrets.get("GOOGLE_API_KEY", ""),
        },
        "server": {
            "host": nat.get("host", "0.0.0.0"),
            "port": nat.get("port", 8002),
            "cors_enabled": True,
        },
    }


def main():
    if not CONFIG_FILE.exists():
        print(f"Error: {CONFIG_FILE} not found")
        sys.exit(1)

    print("Loading config.yaml ...")
    with open(CONFIG_FILE) as f:
        cfg = yaml.safe_load(f)

    secrets_file = cfg.get("secrets_file", ".env.secrets")
    secrets = load_secrets(ROOT / secrets_file)

    config = generate_nat_config(cfg, secrets)
    out_path = ROOT / "configs" / "config.yml"
    out_path.parent.mkdir(parents=True, exist_ok=True)
    with open(out_path, "w") as f:
        yaml.dump(config, f, default_flow_style=False, sort_keys=False)
    print(f"  Generated {out_path}")
    print("Done.")


if __name__ == "__main__":
    main()
