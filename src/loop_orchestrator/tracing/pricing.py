"""What a call cost.

One table, keyed by model id, overridable through `LOOP_MODEL_PRICES` so a price
change is a restart rather than a deploy. A model we do not know the price of is
reported as unpriced — never guessed at, because a wrong number in a cost view is
worse than a missing one.
"""
import json
from dataclasses import dataclass

USAGE_KEYS = ("input_tokens", "cache_creation_input_tokens",
              "cache_read_input_tokens", "output_tokens")


@dataclass(frozen=True)
class Price:
    """USD per million tokens."""
    input: float
    cache_write: float  # 5-minute TTL writes, 1.25x input
    cache_read: float   # 0.1x input
    output: float


# Anthropic list prices, 2026-06-24. Fable 5 is TWICE Opus 5 per token — the
# reviewer stage is the expensive one, which is the opposite of what its name
# suggests and the reason this table is spelled out rather than assumed.
PRICES: dict[str, Price] = {
    "claude-opus-5": Price(5.0, 6.25, 0.5, 25.0),
    "claude-opus-4-8": Price(5.0, 6.25, 0.5, 25.0),
    "claude-fable-5": Price(10.0, 12.5, 1.0, 50.0),
    "claude-sonnet-5": Price(3.0, 3.75, 0.3, 15.0),
    "claude-sonnet-4-6": Price(3.0, 3.75, 0.3, 15.0),
    "claude-haiku-4-5": Price(1.0, 1.25, 0.1, 5.0),
}


def load_overrides(raw: str) -> dict[str, Price]:
    """Merge `{"model": {"input": 1, "cache_write": 2, ...}}` over the table.

    Bad JSON is ignored rather than raised: a typo in an env var must not stop
    the orchestrator from starting, and the cost of ignoring it is a cost figure
    computed at list price.
    """
    merged = dict(PRICES)
    if not raw:
        return merged
    try:
        data = json.loads(raw)
    except (ValueError, TypeError):
        return merged
    if not isinstance(data, dict):
        return merged
    for model, p in data.items():
        if not isinstance(p, dict):
            continue
        try:
            merged[str(model)] = Price(
                float(p["input"]), float(p["cache_write"]),
                float(p["cache_read"]), float(p["output"]))
        except (KeyError, TypeError, ValueError):
            continue
    return merged


def cost_usd(model: str, usage: dict,
             prices: dict[str, Price] | None = None) -> tuple[float, bool]:
    """Returns (dollars, priced). `priced=False` means the model is not in the
    table and the caller should mark the span rather than show a zero."""
    table = PRICES if prices is None else prices
    p = table.get(model or "")
    if p is None:
        return 0.0, False
    total = (
        (usage.get("input_tokens") or 0) * p.input
        + (usage.get("cache_creation_input_tokens") or 0) * p.cache_write
        + (usage.get("cache_read_input_tokens") or 0) * p.cache_read
        + (usage.get("output_tokens") or 0) * p.output
    ) / 1e6
    return total, True
