from __future__ import annotations

import re
import tomllib
from pathlib import Path
from typing import Any

import httpx

from ..domain import Assessment, LABELS, Post, digest
from ..errors import AppError, ProviderError
from .http import read_request

URL = "https://api.typesafe.ai/v1/systemone"
REQUEST_VERSION = "publication-choice-v1"
DEFAULT_MODEL = "jev-1.13.0"


def load_rules(path: Path) -> dict[str, Any]:
    try:
        with path.open("rb") as handle:
            rules = tomllib.load(handle)
        if (set(rules) != {"version", "instructions", "criteria"}
                or set(rules["criteria"]) != LABELS
                or not all(isinstance(v, str) and v.strip() for v in rules["criteria"].values())
                or not isinstance(rules["instructions"], str) or not rules["instructions"].strip()
                or not isinstance(rules["version"], str)):
            raise ValueError
        return rules
    except (OSError, ValueError, TypeError, KeyError):
        raise AppError("invalid_rules_toml") from None


def profile_config(rules: dict[str, Any], model: str, input_mode: str, max_chars: int,
                   translation_revision: str = "azure-v3-auto-en-cache-v1") -> dict[str, Any]:
    if not re.fullmatch(r"jev-\d+\.\d+\.\d+", model):
        raise AppError("use_versioned_jev_model_not_alias")
    if input_mode not in {"original", "english", "both"}:
        raise AppError("invalid_input_mode")
    if max_chars <= 0:
        raise AppError("invalid_max_input_chars")
    return {"provider": "jev", "request_version": REQUEST_VERSION, "model": model,
            "input_mode": input_mode, "rules": rules, "max_input_chars": max_chars,
            "translation_revision": translation_revision if input_mode != "original" else None}


class JevClassifier:
    def __init__(self, client: httpx.AsyncClient, key: str, config: dict[str, Any]):
        if not key:
            raise AppError("missing_typesafe_api_key")
        self.client, self.key, self.config = client, key, config

    async def classify(self, post: Post, translation: str | None = None) -> Assessment:
        mode = self.config["input_mode"]
        if mode != "original" and translation is None:
            raise AppError("translation_required_no_fallback")
        state: dict[str, Any] = {"content_type": "untrusted_post_text",
                                 "context_warnings": list(post.flags)}
        if mode in {"original", "both"}:
            state["original"] = post.text
        if mode in {"english", "both"}:
            state["english_translation"] = translation
        body = {"model": self.config["model"], "state": state, "questions": {
            "publication": {"type": "choice", "instructions": self.config["rules"]["instructions"],
                            "criteria": self.config["rules"]["criteria"]}}}
        response = await read_request(self.client, "POST", URL, service="jev", json=body,
                                      headers={"Authorization": f"Bearer {self.key}"})
        try:
            obj = response.json()
            model = obj["model"]
            answer = obj["answers"]["publication"]
            if model != self.config["model"] or answer["type"] != "choice":
                raise ValueError
            usage = obj.get("usage", {})
            if not isinstance(usage, dict):
                raise ValueError
            usage = {k: v for k, v in usage.items() if k in {"input_tokens", "output_tokens"}
                     and isinstance(v, int) and not isinstance(v, bool) and v >= 0}
            return Assessment.validate(answer, model=model, input_hash=digest(body),
                                       translation=translation, usage=usage)
        except (ValueError, KeyError, TypeError, AppError):
            raise ProviderError("jev_invalid_response") from None
