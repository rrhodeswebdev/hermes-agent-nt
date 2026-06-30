# Commission tracking + NET P&L — implementation report

## Files changed

| File | Change |
|---|---|
| `bridge/hermes_bridge/config.py` | Added `commission_per_contract: float = Field(default=0.0, ge=0.0)` to `ExecutionConfig` |
| `bridge/hermes_bridge/session.py` | `SessionState.__init__`: new `commission_per_contract` param + `commission_total` field; `apply_fill`: accrues on every fill; `realized_net` property; `_persist`/`maybe_roll_day` restore: round-trips `commission_total`; `account_state`: populates new model fields |
| `bridge/hermes_bridge/models.py` | `AccountState`: added `realized_net: float = 0.0` and `commission: float = 0.0` |
| `bridge/hermes_bridge/server.py` | `AppState.__init__` `SessionState(...)` call: added `commission_per_contract=config.execution.commission_per_contract` |
| `bridge/hermes_bridge/dashboard.py` | `render_panel`: added `realized_net=` and `commission=` key=value lines after `realized=`; HTML JS: `rpnl` card shows `gross (net $Y)` when commission > 0 |
| `bridge/tests/test_commission.py` | **New file** — 11 TDD tests (all written first, then implementation made them pass) |

## Test evidence

### Commission tests (written first — TDD)

```
Push-Location bridge; python -m pytest tests/test_commission.py -v
============================= test session starts =============================
collected 11 items
tests\test_commission.py ...........   [100%]
11 passed in 0.05s
```

### Full suite (no regressions)

```
Push-Location bridge; python -m pytest tests
539 passed, 1 warning in 45.49s
Exit: 0
```

### Ruff

```
Push-Location bridge; python -m ruff check hermes_bridge tests
All checks passed!
Exit: 0
```

## New /panel.txt keys

```
realized=<gross realized P&L, 2dp>
realized_net=<net realized P&L after commission, 2dp>
commission=<total commission paid this session, 2dp>
unrealized=<unrealized P&L, 2dp>
```

Both `realized_net` and `commission` appear immediately after the existing `realized=` line. With `commission_per_contract=0.0` (the neutral default), `realized_net == realized` and `commission=0.00` so C# wiring reading the new keys degrades cleanly.

## Commit

See git log for short hash after commit is made.

## Concerns / notes

- None. The neutral 0.0 default means every existing test and live session is unchanged until `trading.local.yaml` sets `execution.commission_per_contract: 0.65`.
- The daily-goal halt check (`check_daily_goal`) still compares against GROSS `realized_pnl`. This is by design — the spec did not change halt semantics, only surface NET on the dashboards.
