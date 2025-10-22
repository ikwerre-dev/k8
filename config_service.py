import json
import os
from typing import Any, Dict, Optional

DEFAULT_CONFIG_PATH = os.environ.get("CONFIG_PATH", os.path.join(os.getcwd(), "config.json"))

_cache: Optional[Dict[str, Any]] = None


def load_config(path: Optional[str] = None) -> Dict[str, Any]:
    global _cache
    if _cache is not None:
        return _cache
    cfg_path = path or DEFAULT_CONFIG_PATH
    if not os.path.exists(cfg_path):
        _cache = {}
        return _cache
    try:
        with open(cfg_path, "r") as f:
            _cache = json.load(f)
    except Exception:
        _cache = {}
    return _cache or {}


def get_log_endpoint() -> Optional[str]:
    cfg = load_config()
    # Expected key in config: { "log_stream_endpoint": "https://api.example.com/logs" }
    return cfg.get("log_stream_endpoint")


def get_auth_header() -> Dict[str, str]:
    cfg = load_config()
    # Optional: { "auth_header": {"Authorization": "Bearer <token>"} }
    hdr = cfg.get("auth_header") or {}
    if not isinstance(hdr, dict):
        return {}
    return hdr