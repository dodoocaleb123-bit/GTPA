"""Final decision authority (blueprint 14.5/14.6). An UNAVAILABLE check can never become a success."""
from __future__ import annotations

from dataclasses import dataclass

from .types import (DATA_UNAVAILABLE, FAIL, PASS, QUALIFIED, REJECTED, UNAVAILABLE, CheckResult)

REQUIRED = ["h1_direction", "m15_direction", "alignment", "valid_zone", "valid_sweep", "confirmed_mss",
            "valid_entry_retest", "valid_volatility", "valid_stop_loss", "valid_target", "minimum_reward_risk",
            "valid_spread", "valid_news", "fresh_data", "no_conflicting_setup", "no_invalidation"]


@dataclass
class Qualification:
    result: str
    checks: list[CheckResult]

    @property
    def rejection_codes(self) -> list[str]:
        return [f"{c.name}:{c.code}" for c in self.checks if c.status != PASS]

    def as_dict(self) -> dict:
        return {"result": self.result, "rejection_codes": self.rejection_codes,
                "checks": [c.as_dict() for c in self.checks]}


def qualify(checks: list[CheckResult]) -> Qualification:
    by = {c.name: c for c in checks}
    full = [by.get(n) or CheckResult(n, UNAVAILABLE, "CHECK_NOT_RUN") for n in REQUIRED]
    if any(c.status == UNAVAILABLE for c in full):
        # an unavailable safety check outranks an ordinary failure in the label, but both block publication
        res = DATA_UNAVAILABLE if not any(c.status == FAIL for c in full) else REJECTED
    elif any(c.status == FAIL for c in full):
        res = REJECTED
    else:
        res = QUALIFIED
    return Qualification(res, full)
