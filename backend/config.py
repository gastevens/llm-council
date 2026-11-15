"""Configuration for the LLM Council (war-room branch).

By default the council runs on your existing subscriptions through locally
installed, signed-in coding CLIs:

    ChatGPT -> Codex CLI   (`codex login`, ChatGPT plan)
    Claude  -> Claude Code (`claude` /login, Claude plan)
    Grok    -> Grok Build  (`grok login`, grok.com plan)

Override with environment variables (or a .env file):

    COUNCIL_MEMBERS   comma-separated member specs (default: "chatgpt,claude,grok")
    COUNCIL_CHAIRMAN  member spec for the chairman  (default: "claude")
    COUNCIL_TITLE_MEMBER  member spec used for conversation titles (default: "grok")
    COUNCIL_TIMEOUT   per-call timeout in seconds   (default: 300)
    OPENROUTER_API_KEY  only needed for openrouter members

A member spec is one of the presets below, optionally with a model override
("claude:claude-opus-5"), or "openrouter:<model id>" (e.g.
"openrouter:openai/gpt-5.1") to use the original OpenRouter path.
"""

import os
from dotenv import load_dotenv

from .providers import Member

load_dotenv()

# OpenRouter API key (optional fallback provider)
OPENROUTER_API_KEY = os.getenv("OPENROUTER_API_KEY")

# OpenRouter API endpoint
OPENROUTER_API_URL = "https://openrouter.ai/api/v1/chat/completions"

# Built-in presets: spec name -> (display name, provider)
PRESETS = {
    "chatgpt": ("ChatGPT", "codex"),
    "codex": ("ChatGPT", "codex"),
    "gpt": ("ChatGPT", "codex"),
    "claude": ("Claude", "claude"),
    "grok": ("Grok", "grok"),
}


def parse_member(spec: str) -> Member:
    spec = spec.strip()
    name, _, model = spec.partition(":")
    key = name.lower()
    if key == "openrouter":
        if not model:
            raise ValueError("openrouter member needs a model id, e.g. openrouter:openai/gpt-5.1")
        return Member(name=model, provider="openrouter", model=model)
    if key not in PRESETS:
        raise ValueError(f"Unknown council member '{spec}'. Use one of {sorted(PRESETS)} or openrouter:<model>")
    display, provider = PRESETS[key]
    return Member(name=display, provider=provider, model=model or None)


COUNCIL_MEMBERS = [
    parse_member(s)
    for s in os.getenv("COUNCIL_MEMBERS", "chatgpt,claude,grok").split(",")
    if s.strip()
]
CHAIRMAN = parse_member(os.getenv("COUNCIL_CHAIRMAN", "claude"))
TITLE_MEMBER = parse_member(os.getenv("COUNCIL_TITLE_MEMBER", "grok"))
TIMEOUT = float(os.getenv("COUNCIL_TIMEOUT", "300"))

# Back-compat names (model display names)
COUNCIL_MODELS = [m.name for m in COUNCIL_MEMBERS]
CHAIRMAN_MODEL = CHAIRMAN.name

# Data directory for conversation storage
DATA_DIR = "data/conversations"
