"""Loads non-secret LLM settings from config/llm.toml (stdlib tomllib)."""
import os
import tomllib
from pathlib import Path

DEFAULT_PATH = Path(__file__).resolve().parent.parent / "config" / "llm.toml"
DEFAULTS = {
    "base_url": "https://openrouter.ai/api/v1",
    "model": "anthropic/claude-opus-5.5",
    "fallback_models": [],
    "pdf_engine": "native",
    "timeout_seconds": 120,
    "max_tokens": 16000,
    "temperature": 0,
    "app_title": "InsureRemind",
    "app_url": "",
}


def load_llm_config(environ=None):
    environ = os.environ if environ is None else environ
    path = Path(environ.get("LLM_CONFIG_PATH") or DEFAULT_PATH)
    with open(path, "rb") as f:
        cfg = {**DEFAULTS, **tomllib.load(f).get("openrouter", {})}
    if environ.get("OPENROUTER_MODEL"):
        cfg["model"] = environ["OPENROUTER_MODEL"]
    return cfg
