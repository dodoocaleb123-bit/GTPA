from __future__ import annotations

from dataclasses import dataclass, field

PASS, FAIL, UNAVAILABLE = "PASS", "FAIL", "UNAVAILABLE"
QUALIFIED, REJECTED, WAITING, DATA_UNAVAILABLE, INVALIDATED = (
    "QUALIFIED", "REJECTED", "WAITING", "DATA_UNAVAILABLE", "INVALIDATED")


@dataclass
class CheckResult:
    name: str
    status: str                 # PASS | FAIL | UNAVAILABLE
    code: str = "OK"            # machine-readable reason
    detail: dict = field(default_factory=dict)

    def as_dict(self) -> dict:
        return {"name": self.name, "status": self.status, "code": self.code, "detail": self.detail}


def ok(name: str, **d) -> CheckResult:
    return CheckResult(name, PASS, "OK", d)


def fail(name: str, code: str, **d) -> CheckResult:
    return CheckResult(name, FAIL, code, d)


def unavailable(name: str, code: str, **d) -> CheckResult:
    return CheckResult(name, UNAVAILABLE, code, d)
