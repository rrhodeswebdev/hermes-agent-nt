"""The brain-health classifier: a DOWN brain (re-auth needed) or a THROTTLED one
(subscription usage cap) must be distinguishable from a routine TRANSIENT blip, so the
dashboard can tell an outage from ordinary caution. See brain_health.py."""

from hermes_bridge.brain_health import (
    DOWN,
    OK,
    THROTTLED,
    TRANSIENT,
    classify_brain_error,
)
from hermes_bridge.models import BrainTimeout


def test_braintimeout_is_transient():
    # Our own time budget, not a fault in the brain — never alarm the operator.
    assert classify_brain_error(BrainTimeout(150.0)) == TRANSIENT


def test_auth_expiry_is_down():
    # The claude CLI stderr on an expired login — operator must re-authenticate.
    exc = RuntimeError("claude CLI exited 1: Invalid API key · Please run /login")
    assert classify_brain_error(exc) == DOWN


def test_oauth_token_revoked_is_down():
    exc = RuntimeError("claude CLI exited 1: OAuth token revoked")
    assert classify_brain_error(exc) == DOWN


def test_bad_flag_is_down():
    # A renamed/removed CLI flag is operator-fix-now, not a transient blip.
    exc = RuntimeError("claude CLI exited 2: unknown option '--json-schema'")
    assert classify_brain_error(exc) == DOWN


def test_usage_limit_is_throttled():
    exc = RuntimeError(
        "claude CLI exited 1: Claude usage limit reached. Your limit will reset at 3pm")
    assert classify_brain_error(exc) == THROTTLED


def test_rate_limit_is_throttled():
    exc = RuntimeError("claude CLI exited 1: 429 too many requests")
    assert classify_brain_error(exc) == THROTTLED


def test_generic_runtime_error_is_transient():
    # An EOF / unclassified failure degrades quietly — assume it self-recovers.
    exc = RuntimeError("claude session closed mid-turn (EOF)")
    assert classify_brain_error(exc) == TRANSIENT


def test_overloaded_is_transient():
    exc = RuntimeError("claude CLI exited 1: server overloaded, please try again")
    assert classify_brain_error(exc) == TRANSIENT


def test_statuses_are_distinct():
    assert len({OK, TRANSIENT, THROTTLED, DOWN}) == 4
