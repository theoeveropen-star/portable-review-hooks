"""Claude native accounting; never mix cost estimates with subscription quota.

Claude Code >= 2.1.277 restores cumulative cost/modelUsage on --resume. The
result's usage is turn-local/main-loop-only; modelUsage also includes helpers.
"""
import math

FIELDS = ("inputTokens", "outputTokens", "cacheReadInputTokens", "cacheCreationInputTokens")


def count(value):
    return type(value) is int and 0 <= value <= 9007199254740991


def money(value):
    return type(value) in (int, float) and math.isfinite(value) and value >= 0


def normalized(raw):
    if not isinstance(raw, dict) or any(not count(raw.get(k)) for k in FIELDS):
        return None
    input_tokens = raw["inputTokens"] + raw["cacheReadInputTokens"] + raw["cacheCreationInputTokens"]
    if not count(input_tokens + raw["outputTokens"]):
        return None
    return {"input_tokens": input_tokens, "output_tokens": raw["outputTokens"],
            "total_tokens": input_tokens + raw["outputTokens"],
            "cached_input_tokens": raw["cacheReadInputTokens"],
            "cache_creation_input_tokens": raw["cacheCreationInputTokens"],
            "uncached_input_tokens": raw["inputTokens"]}


def model_totals(value):
    if not isinstance(value, dict) or not value:
        return None
    total = dict.fromkeys(FIELDS, 0)
    for model, usage in value.items():
        if not isinstance(model, str) or normalized(usage) is None:
            return None
        for key in FIELDS:
            total[key] += usage[key]
    return total


def settle(result, prior_models, prior_cost):
    """Return whole-tree turn delta and estimated cost, or uncertainty.

No subtraction of result.usage, no sum of assistant snapshots plus result.
Crash results may be zeroed; they must never release a held reservation.
"""
    if result.get("subtype") == "error_during_execution":
        return None, None
    totals = model_totals(result.get("modelUsage"))
    prior = model_totals(prior_models) if prior_models else dict.fromkeys(FIELDS, 0)
    cost = result.get("total_cost_usd")
    if totals is None or prior is None or not money(cost) or cost < prior_cost:
        return None, None
    # Check each model too: a disappearing model or regressing native counter
    # is a reset/missing baseline, not a negative-cost discount.
    for model, usage in prior_models.items():
        current = result["modelUsage"].get(model)
        if not isinstance(current, dict) or any(current[k] < usage[k] for k in FIELDS):
            return None, None
    delta = {key: totals[key] - prior[key] for key in FIELDS}
    usage = normalized(delta)
    return usage, max(0.0, float(cost) - prior_cost) if usage else None


def context_tokens(usage):
    """One request's context only; outputs and other requests are excluded."""
    if not isinstance(usage, dict):
        return None
    fields = ("input_tokens", "cache_read_input_tokens", "cache_creation_input_tokens")
    if any(not count(usage.get(key, 0)) for key in fields) or "input_tokens" not in usage:
        return None
    return sum(usage.get(key, 0) for key in fields)


# Re-export for existing accounting callers.
from .models import model_matches


def receipt(requested, observed, effort, retried=False, failed=False):
    """Snow's evidence-based trust colors, adapted to the CLI boundary.

CLI init proves its selected model, not the upstream transport route. Effort
is requested but has no independent provider receipt; we expose that limit.
"""
    mismatch = observed is not None and not model_matches(requested, observed)
    level = "red" if failed or mismatch else "yellow" if retried else "gray"
    return {"level": level, "requested_model": requested, "observed_model": observed,
            "requested_effort": effort, "effort_verified": False,
            "transport_verified": False, "model_matches": not mismatch if observed else None,
            "memory_hold": level in ("red", "orange"), "context_hold": level == "red",
            "reason": "provider_failure" if failed else "model_mismatch" if mismatch else "provider_retry" if retried else "cli_receipt_only"}
