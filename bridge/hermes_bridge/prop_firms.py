"""Prop-firm catalog: load the committed firm/account catalog, look up a selected account,
apply its hard numbers into the enforced config, and persist the selection locally.

The catalog (``config/prop-firms.yaml``) is committed reference data: firms -> account types
-> account sizes, each carrying the firm's documented hard numbers. Selecting an account does
two things elsewhere in the bridge:

* the firm's context *.md (``hermes/prop-firms/<context_file>``) is loaded into the brain's
  system prompt (judgment-level guidance — the brain trades within the firm's rules), and
* the account's numbers that map onto the bridge's EXISTING safety primitives are written into
  the live config the RiskGate reads (``apply_account_profile`` below).

Only the **daily loss limit** and the **contract ceiling** map cleanly today: the RiskGate
enforces a daily loss halt and a max-contracts cap, but has no cumulative high-water-mark, so a
firm's evaluation **profit target** and **trailing drawdown** are NOT enforced here — they are
surfaced to the brain (context file) and the dashboard instead. (Trailing-drawdown enforcement
is a deliberate follow-up; see the spec.) This module is pure data + small helpers so it can be
unit-tested without standing up the server.
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

import yaml
from pydantic import BaseModel, Field

from .config import BridgeConfig

if TYPE_CHECKING:
    from .session import SessionState


class AccountTier(BaseModel):
    """One selectable account size within a program, with the firm's documented numbers.

    ``max_daily_loss`` / ``max_contracts`` are the two that get ENFORCED (they map onto the
    RiskGate). ``profit_target`` (the cumulative evaluation target) and ``trailing_drawdown``
    (the cumulative high-water-mark limit) are informational for the brain + dashboard. A
    ``None`` field is simply omitted from the catalog and never overrides anything."""

    size: float = Field(gt=0)
    max_daily_loss: float | None = Field(default=None, gt=0)
    max_contracts: int | None = Field(default=None, ge=1)
    profit_target: float | None = Field(default=None, gt=0)
    trailing_drawdown: float | None = Field(default=None, gt=0)


class AccountProgram(BaseModel):
    """A named account program within a firm (e.g. "Trading Combine"), with its size tiers."""

    name: str
    accounts: list[AccountTier] = Field(default_factory=list)


class PropFirm(BaseModel):
    name: str
    # The firm's context file under AccountProfileConfig.context_dir (e.g. "topstep.md").
    context_file: str | None = None
    account_types: list[AccountProgram] = Field(default_factory=list)


class PropFirmCatalog(BaseModel):
    firms: list[PropFirm] = Field(default_factory=list)


def load_catalog(path: str | Path | None) -> PropFirmCatalog:
    """Load the firm catalog from YAML. A missing file (or ``None``) yields an empty catalog
    so the feature is simply inert when not set up; a malformed file raises (fail loud, like
    the rest of config validation)."""
    if path is None:
        return PropFirmCatalog()
    p = Path(path)
    if not p.exists():
        return PropFirmCatalog()
    data = yaml.safe_load(p.read_text(encoding="utf-8")) or {}
    return PropFirmCatalog.model_validate(data)


def _norm(s: str | None) -> str:
    return (s or "").strip().lower()


def find_firm(catalog: PropFirmCatalog, firm: str | None) -> PropFirm | None:
    key = _norm(firm)
    if not key:
        return None
    for f in catalog.firms:
        if _norm(f.name) == key:
            return f
    return None


def find_account(
    catalog: PropFirmCatalog, firm: str | None, account_type: str | None, size: float | None
) -> tuple[PropFirm, AccountProgram, AccountTier] | None:
    """Resolve a (firm, account type, size) selection to its catalog entries, or ``None`` if
    no exact match exists. Firm/type names match case-insensitively; size matches numerically."""
    f = find_firm(catalog, firm)
    if f is None or not account_type or size is None:
        return None
    tkey = _norm(account_type)
    for prog in f.account_types:
        if _norm(prog.name) != tkey:
            continue
        for tier in prog.accounts:
            if float(tier.size) == float(size):
                return f, prog, tier
    return None


def apply_account_profile(
    cfg: BridgeConfig, session: SessionState | None, tier: AccountTier
) -> dict:
    """Write the tier's enforceable numbers into the live config (and the running session).

    Sets ``cfg.risk.max_contracts`` and ``cfg.daily_goal.max_daily_loss`` from the tier's
    non-null values, and mirrors the daily loss onto ``session.max_daily_loss`` so the live
    daily-goal check uses it immediately (SessionState holds its own copy, seeded at startup).
    Returns the numbers that were applied (and the informational ones) for logging/UI."""
    if tier.max_contracts is not None:
        cfg.risk.max_contracts = int(tier.max_contracts)
    if tier.max_daily_loss is not None:
        cfg.daily_goal.max_daily_loss = float(tier.max_daily_loss)
        if session is not None:
            session.max_daily_loss = float(tier.max_daily_loss)
    return {
        "size": tier.size,
        "max_daily_loss": tier.max_daily_loss,   # enforced (when set)
        "max_contracts": tier.max_contracts,     # enforced (when set)
        "profit_target": tier.profit_target,     # guidance only (cumulative eval target)
        "trailing_drawdown": tier.trailing_drawdown,  # guidance only (no primitive yet)
    }


def persist_account_profile(
    base_config_path: str | Path,
    prop_firm: str,
    account_type: str,
    account_size: float,
) -> Path:
    """Write the selection into the sibling ``*.local.yaml`` (deep-merged on top of the base at
    startup), preserving every other key already in that file. Returns the path written.

    Mirrors where personal values already live: ``config/trading.local.yaml`` is gitignored and
    deep-merged over ``config/trading.yaml`` by ``load_config``."""
    p = Path(base_config_path)
    local = p.with_name(f"{p.stem}.local{p.suffix}")
    data: dict = {}
    if local.exists():
        data = yaml.safe_load(local.read_text(encoding="utf-8")) or {}
    section = dict(data.get("account_profile") or {})
    section.update(
        {"prop_firm": prop_firm, "account_type": account_type, "account_size": account_size}
    )
    data["account_profile"] = section
    local.parent.mkdir(parents=True, exist_ok=True)
    local.write_text(yaml.safe_dump(data, sort_keys=False), encoding="utf-8")
    return local
