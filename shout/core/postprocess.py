"""Optional LLM post-processing via OpenAI-compatible HTTP endpoint."""
from __future__ import annotations

import logging

import requests

from ..config import LLMConfig

log = logging.getLogger(__name__)


def postprocess(text: str, cfg: LLMConfig) -> str:
    """Send `text` to the LLM with the configured prompt; return cleaned text.

    Returns the original text on any failure, so dictation never breaks because
    of a flaky local LLM server.
    """
    if not cfg.enabled or not text.strip():
        return text

    url = cfg.base_url.rstrip("/") + "/chat/completions"
    payload = {
        "model": cfg.model,
        "messages": [
            {"role": "system", "content": cfg.prompt},
            {"role": "user", "content": text},
        ],
        "temperature": 0.2,
        "stream": False,
    }
    headers = {"Content-Type": "application/json"}
    if cfg.api_key:
        headers["Authorization"] = f"Bearer {cfg.api_key}"

    try:
        log.debug("LLM postprocess request to %s model=%s", url, cfg.model)
        resp = requests.post(url, json=payload, headers=headers, timeout=cfg.timeout_seconds)
        resp.raise_for_status()
        data = resp.json()
        choice = data["choices"][0]["message"]["content"]
        return str(choice).strip()
    except Exception as exc:  # noqa: BLE001
        log.warning("LLM postprocess failed: %s — using raw text", exc)
        return text
