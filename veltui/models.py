"""The models duck.ai offers, and the names veltui knows them by."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Model:
    id: str                       # what duck.ai puts in its request body
    name: str                     # what veltui shows
    ui: str                       # the card's label in duck.ai's model picker
    kind: str                     # "think" (reasons first, slower) or "fast"
    aliases: tuple[str, ...] = ()


MODELS: tuple[Model, ...] = (
    Model("gpt-5-mini", "GPT-5 Mini", "GPT-5 mini", "think", ("gpt5", "gpt-5")),
    Model("gpt-4o-mini", "GPT-4o Mini", "GPT-4o mini", "fast", ("gpt4", "gpt-4", "4o")),
    Model("tinfoil/gpt-oss-120b", "GPT-OSS 120B", "gpt-oss 120B", "think",
          ("oss", "gpt-oss", "120b", "tinfoil")),
    Model("claude-haiku-4-5", "Claude Haiku 4.5", "Claude Haiku 4.5", "fast",
          ("claude", "haiku")),
    Model("meta-llama/Llama-4-Scout-17B-16E-Instruct", "Llama 4 Scout", "Llama 4 Scout",
          "fast", ("llama", "llama4", "meta", "scout")),
    Model("mistral-small-2603", "Mistral Small 4", "Mistral Small 4", "fast", ("mistral",)),
)

DEFAULT = MODELS[0]
# what a fresh duck.ai page has selected before anyone touches the picker
PAGE_DEFAULT = MODELS[0]


def by_id(model_id: str) -> Model | None:
    return next((m for m in MODELS if m.id == model_id), None)


def name_of(model_id: str) -> str:
    """Display name — or the raw id when duck.ai answers with a model we don't list."""
    m = by_id(model_id)
    return m.name if m else model_id


def find_model(query: str) -> Model | None:
    """A model by number (1-based), id, alias or name; a prefix works if it's unique."""
    q = query.strip().lower()
    if not q:
        return None
    if q.isdigit():
        i = int(q) - 1
        return MODELS[i] if 0 <= i < len(MODELS) else None
    for m in MODELS:
        if q in (m.id.lower(), m.name.lower(), m.ui.lower(), *m.aliases):
            return m
    hits = [m for m in MODELS
            if any(k.startswith(q) for k in (m.id.lower(), m.name.lower(), *m.aliases))]
    return hits[0] if len(hits) == 1 else None
