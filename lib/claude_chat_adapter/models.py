"""Explicit Anthropic IDs supported by the installed Claude Code 2.1.280.

Sources checked 2026-09-24 (Sonnet 5.5 added 2026-09-29): code.claude.com/docs/en/model-config and
platform.claude.com/docs/en/models/overview, cross-checked with installed CLI.
Fable 5.1 added 2026-10-25: the installed CLI 2.1.284 bundle lists claude-fable-5-1 and the
same Max account already runs it in construction Claude Code; a count_tokens probe on the
native OAuth login succeeded before publication.
Catalog support is NOT an account entitlement receipt. Pins match exactly.
"""
BASE_EFFORTS = ("low", "medium", "high")
PINNED = {
    "claude-fable-5-1": ("Claude Fable 5.1", (*BASE_EFFORTS, "xhigh", "max")),
    "claude-opus-5-5": ("Claude Opus 5.5", (*BASE_EFFORTS, "xhigh", "max")),
    "claude-opus-5": ("Claude Opus 5", (*BASE_EFFORTS, "xhigh", "max")),
    "claude-opus-4-8": ("Claude Opus 4.8", (*BASE_EFFORTS, "xhigh", "max")),
    "claude-opus-4-7": ("Claude Opus 4.7", (*BASE_EFFORTS, "xhigh", "max")),
    "claude-opus-4-6": ("Claude Opus 4.6", (*BASE_EFFORTS, "max")),
    "claude-sonnet-5-5": ("Claude Sonnet 5.5", (*BASE_EFFORTS, "xhigh", "max")),
    "claude-sonnet-5": ("Claude Sonnet 5", (*BASE_EFFORTS, "xhigh", "max")),
    "claude-sonnet-4-6": ("Claude Sonnet 4.6", (*BASE_EFFORTS, "max")),
    "claude-haiku-4-5-20251001": ("Claude Haiku 4.5", ()),
}
# Alias choices retain the existing conservative effort set and medium default.
# No assertion that a moving alias resolves to a particular version.
EFFORTS = {"opus": BASE_EFFORTS, "sonnet": BASE_EFFORTS, "haiku": (),
           **{key: value[1] for key, value in PINNED.items()}}
KNOWN_RECEIPTS = {*PINNED, "claude-opus-4-5-20251101", "claude-opus-4-1-20250805",
                  "claude-opus-4-20250514", "claude-sonnet-4-5-20250929", "claude-sonnet-4-20250514"}
# Opus 5.5 can run on its 1M-context variant: the CLI flag and the CLI's own
# init/modelUsage name carry "[1m]", while assistant messages report the canonical
# ID. Records keep the canonical ID; only the CLI-facing name carries the suffix.
LONG_CONTEXT = frozenset(("claude-opus-5-5",))
LONG_SUFFIX = "[1m]"


def cli_model(model):
    return model + LONG_SUFFIX if model in LONG_CONTEXT else model


def default_effort(model):
    # Let pinned models use their native default, rather than imposing medium.
    return "medium" if model in ("opus", "sonnet") else None


def model_matches(requested, observed):
    if not isinstance(observed, str) or requested not in EFFORTS:
        return False
    if requested in PINNED:
        return observed == requested or (requested in LONG_CONTEXT and observed == requested + LONG_SUFFIX)
    return observed == requested or (observed in KNOWN_RECEIPTS and observed.startswith("claude-" + requested + "-"))


def catalog(cli_available, default_model):
    return {"models": [{"id": key, "label": PINNED[key][0] if key in PINNED else "Claude " + key.title() + "（默认／跟随推荐版）",
                        "available": cli_available, "account_entitlement_verified": False,
                        "reasoning_efforts": list(efforts), "default_reasoning_effort": default_effort(key)}
                       for key, efforts in EFFORTS.items()],
            "default_model": default_model, "provider": "claude",
            "availability_source": "documented_ids_cli_2_1_280_account_unverified", "account_entitlement_verified": False}
