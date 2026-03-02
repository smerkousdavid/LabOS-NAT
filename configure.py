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


def generate_nat_config(cfg: dict, secrets: dict) -> dict:
    lm = cfg.get("models", {})
    nat = cfg.get("nat", {})
    session = nat.get("session", {})
    vsop = nat.get("vsop", {})
    tools = nat.get("tools", {})
    video = nat.get("video", {})

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
            "vlm": {
                "base_url": lm.get("vlm", {}).get("base_url", "http://localhost:8500/v1"),
                "model": lm.get("vlm", {}).get("model", "Zaixi/STELLA-VLM-32b"),
                "api_key": lm.get("vlm", {}).get("api_key", "not-needed"),
                "max_model_len": lm.get("vlm", {}).get("max_model_len", 8096),
            },
        },
        "vsop_provider": {
            "provider": vsop.get("provider", "stella"),
            "polling_interval": vsop.get("polling_interval", 3),
            "multi_frame": vsop.get("multi_frame", {
                "count": 8,
                "window_seconds": 10,
                "resolution": 512,
                "jpeg_quality": 70,
            }),
        },
        "video": {
            "mode": video.get("mode", "websocket"),
            "mediamtx_url": video.get("mediamtx_url", "rtsp://mediamtx:8554"),
        },
        "tools": tools,
        "secrets": {
            "serpapi_key": secrets.get("SERPAPI_KEY", ""),
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
