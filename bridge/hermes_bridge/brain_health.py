"""Classify a brain-call failure so the dashboard can tell a DOWN brain from a cautious one.

The decision brain runs on the `claude` CLI on a subscription, and every failure degrades to
WAIT (the safe default). But a WAIT from a cautious brain and a WAIT from a brain whose login
expired look identical on the dashboard — so a real outage can masquerade as routine caution
for a whole session. This module buckets a failure into one operator-facing status so a
terminal outage (re-auth needed) or a subscription throttle (usage cap) is visible at a glance.

Inspired by nousresearch/hermes-agent's `agent/error_classifier.py` (its FailoverReason
taxonomy + stderr pattern lists), reduced to the three buckets that matter for a single
provider with no API key: we deliberately do NOT do multi-provider failover or credential
pools — there is one subscription, and the only recovery is the operator's.
"""

from __future__ import annotations

from .models import BrainTimeout

# Operator-facing brain statuses (display only — none of these gate trading).
# DOWN is terminal (login/credential expired, or a bad invocation): the operator must act.
OK = "OK"                # the brain answered
TRANSIENT = "TRANSIENT"  # a timeout / overload / transport blip — expected to self-recover
THROTTLED = "THROTTLED"  # subscription usage / rate cap hit — paused until it resets
DOWN = "DOWN"            # terminal — see note above; surfaced loudly so re-auth is obvious

# Substrings (matched case-insensitively in the CLI stderr / error text) that mark a TERMINAL
# failure: the operator must re-authenticate or fix the invocation. Kept specific — a false
# DOWN is noisy, so we match the human-readable wording the CLI actually prints (not raw HTTP
# codes, which could collide with prices/counts in the message).
_TERMINAL_PATTERNS = (
    "invalid api key", "invalid_api_key", "authentication", "unauthorized",
    "forbidden", "invalid token", "token expired", "token revoked",
    "token has expired", "access denied", "not authenticated", "please run",
    "/login", "claude login", "oauth",
    # A malformed invocation (renamed/removed flag) is also operator-fix-now, not transient.
    "unknown option", "unknown argument", "unrecognized", "no such option",
)

# Substrings that mark a THROTTLE: the subscription's usage / rate cap. Time-bounded (it
# resets) but operator-relevant — the brain is paused, not broken.
_THROTTLE_PATTERNS = (
    "usage limit", "rate limit", "rate_limit", "too many requests",
    "quota", "limit reached", "limit exceeded", "resets at", "reset at",
    "requests per", "tokens per", "throttl",
)


def classify_brain_error(exc: BaseException) -> str:
    """Bucket a brain-call exception into DOWN / THROTTLED / TRANSIENT.

    A bridge-side timeout (`BrainTimeout`) is always TRANSIENT — it is our own time budget,
    not a fault in the brain. Otherwise match the error text: terminal (auth / bad-flag) first,
    then throttle, else transient (network / overload / EOF / anything unrecognized)."""
    if isinstance(exc, BrainTimeout):
        return TRANSIENT
    msg = f"{type(exc).__name__}: {exc}".lower()
    if any(p in msg for p in _TERMINAL_PATTERNS):
        return DOWN
    if any(p in msg for p in _THROTTLE_PATTERNS):
        return THROTTLED
    return TRANSIENT
