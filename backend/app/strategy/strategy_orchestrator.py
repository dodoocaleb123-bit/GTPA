"""Deterministic strategy cycle: data -> trend -> zones -> sweep -> MSS -> entry -> qualification.

One setup = one decision. Once an entry candle has been evaluated the setup is registered as seen and is
never re-evaluated on a later candle (prevents signal shopping and duplicate publication).
"""
from __future__ import annotations

import hashlib
from dataclasses import dataclass, field

import pandas as pd

from ..config import TF_SECONDS, Cfg
from ..indicators.core import atr as atr_fn, ema, warmup_ok
from ..qualification import risk
from ..qualification.qualification_engine import Qualification, qualify
from ..qualification.types import (CheckResult, DATA_UNAVAILABLE, INVALIDATED, PASS, QUALIFIED, REJECTED, WAITING,
                                   fail, ok, unavailable)
from ..qualification.volatility import check_volatility
from .entry_engine import find_entry
from .liquidity_engine import detect_sweeps
from .mss_engine import detect_mss
from .swing_engine import find_swings
from .trend_engine import BEARISH, BULLISH, alignment, classify
from .zone_engine import build_zones, valid_zones


@dataclass
class Decision:
    setup_id: str
    direction: str
    result: str                    # QUALIFIED / REJECTED / DATA_UNAVAILABLE / INVALIDATED / WAITING
    stage: str
    detail: dict = field(default_factory=dict)
    qualification: Qualification | None = None
    signal: dict | None = None     # populated only when QUALIFIED

    @property
    def terminal(self) -> bool:
        return self.result != WAITING

    def as_dict(self) -> dict:
        return {"setup_id": self.setup_id, "direction": self.direction, "result": self.result, "stage": self.stage,
                "detail": self.detail, "qualification": self.qualification.as_dict() if self.qualification else None,
                "signal": self.signal}


@dataclass
class CycleResult:
    now: pd.Timestamp
    trace: dict
    decisions: list[Decision]


class StrategyOrchestrator:
    def __init__(self, cfg: Cfg):
        self.cfg = cfg
        self._cache: dict = {}

    # cached per (timeframe, last candle, length): structural analysis only changes when a candle closes
    def _analysis(self, tf: str, df: pd.DataFrame):
        key = (tf, df["time"].iloc[-1], len(df))
        if key in self._cache:
            return self._cache[key]
        t = self.cfg.trend
        sw = find_swings(df, t.swing_left, t.swing_right)
        tr = classify(df, t.swing_left, t.swing_right, t.bos_atr_buffer, self.cfg.volatility.atr_period, sw)
        zones = build_zones(df, sw, tf, self.cfg.zones.zone_expiry_candles) if tf == self.cfg.zones.source_timeframe else []
        res = (sw, tr, zones, atr_fn(df, self.cfg.volatility.atr_period))
        if len(self._cache) > 64:
            self._cache.clear()
        self._cache[key] = res
        return res

    def evaluate(self, data: dict[str, pd.DataFrame], now: pd.Timestamp, *, spread_check, news_check: CheckResult,
                 data_health: dict[str, bool] | None = None, seen: set[str] | None = None,
                 active_signals: list[dict] | None = None, spread_value: float = 0.0) -> CycleResult:
        """`spread_check(stop_distance) -> CheckResult` and `news_check` are produced by the safety engines."""
        cfg, seen, active_signals = self.cfg, seen if seen is not None else set(), active_signals or []
        data_health = data_health or {tf: True for tf in TF_SECONDS}
        trace: dict = {"time": now.isoformat(), "stages": {}}
        decisions: list[Decision] = []

        need = {"H1": cfg.trend.ema_slow * 2, "M15": 120, "M5": 60, "M1": 30}
        for tf, n in need.items():
            if tf not in data or len(data[tf]) < n:
                trace["stages"]["warmup"] = {"ok": False, "timeframe": tf, "have": len(data.get(tf, [])), "need": n}
                return CycleResult(now, trace, decisions)
        trace["stages"]["warmup"] = {"ok": True}
        h1, m15, m5, m1 = data["H1"], data["M15"], data["M5"], data["M1"]

        _, h1_tr, _, _ = self._analysis("H1", h1)
        sw15, m15_tr, zones, atr15 = self._analysis("M15", m15)
        ek = ("EMA", h1["time"].iloc[-1], len(h1))
        if ek not in self._cache:
            self._cache[ek] = (ema(h1["close"], cfg.trend.ema_fast).iloc[-1], ema(h1["close"], cfg.trend.ema_slow).iloc[-1])
        e50, e200 = self._cache[ek]
        ema_state = None if (e50 != e50 or e200 != e200 or e50 == e200) else (BULLISH if e50 > e200 else BEARISH)
        trace["stages"]["trend"] = {"H1": h1_tr.as_dict(), "M15": m15_tr.as_dict(),
                                    "ema50": None if e50 != e50 else float(e50), "ema200": None if e200 != e200 else float(e200),
                                    "ema_state": ema_state}
        # The sweep itself lowers/raises M15 structure, so M15 direction is judged on the structure that existed
        # BEFORE the breach candle (blueprint 10.2 "H1 and M15 bullish alignment" is a precondition of the sweep).
        bias = "BUY" if h1_tr.state == BULLISH else "SELL" if h1_tr.state == BEARISH else None
        trace["stages"]["alignment"] = {"h1_side": bias or "NONE", "m15_now": m15_tr.state}
        trace["stages"]["zones"] = [z.as_dict() for z in zones if z.status in ("ACTIVE", "TESTED")][-12:]
        if bias is None:
            return CycleResult(now, trace, decisions)

        # ---- check 1 (H1 direction incl. EMA filter) ---------------------------------------
        c_h1 = ok("h1_direction", state=h1_tr.state)
        if cfg.trend.require_ema_confirmation:
            want = BULLISH if bias == "BUY" else BEARISH
            if ema_state is None:
                c_h1 = unavailable("h1_direction", "EMA_UNAVAILABLE")
            elif ema_state != want:
                c_h1 = fail("h1_direction", "EMA_DISAGREES", ema_state=ema_state)

        a15 = float(atr15.iloc[-1])
        sweeps = detect_sweeps(m15, sw15, zones, bias, cfg.liquidity.reclaim_window_candles,
                               cfg.liquidity.sweep_lookback_candles)
        trace["stages"]["sweeps"] = [s.as_dict() for s in sweeps]
        trace["stages"]["setups"] = []
        got_qualified = False
        for sw in sweeps:
            pre = self._analysis("M15@" + str(sw.breach_idx), m15.iloc[:sw.breach_idx])[1] if sw.breach_idx > 30 else None
            if pre is None or alignment(h1_tr.state, pre.state) != bias:
                trace["stages"].setdefault("rejected_sweeps", []).append({"sweep": sw.id, "m15_before_sweep": pre.state if pre else "N/A"})
                continue
            c_m15, c_al = ok("m15_direction", state=pre.state, as_of="before_sweep"), ok("alignment", bias=bias)
            setup_id = "S-" + hashlib.sha1(f"{bias}|{sw.id}".encode()).hexdigest()[:10]
            if setup_id in seen:
                continue
            mss, st = detect_mss(m5, sw, cfg.trend.swing_left, cfg.trend.swing_right, cfg.mss.atr_buffer,
                                 cfg.mss.minimum_body_ratio, cfg.volatility.atr_period)
            info = {"setup_id": setup_id, "sweep": sw.id, "mss_status": st}
            trace["stages"]["setups"].append(info)
            if st == "INVALIDATED":
                decisions.append(Decision(setup_id, bias, INVALIDATED, "mss", {"reason": "CLOSE_BEYOND_SWEEP_EXTREME"}))
                continue
            if mss is None:
                if st == "NO_INTERNAL_STRUCTURE":
                    decisions.append(Decision(setup_id, bias, REJECTED, "mss", {"reason": st}))
                continue

            def tp_proxy(zone, _b=bias):
                return risk.take_profit(m15, sw15, zone.upper if _b == "BUY" else zone.lower, _b)

            er = find_entry(m1, m5, mss, sw.extreme, now, cfg.signals.expiry_after_mss_minutes,
                            cfg.entry.displacement_lookback_candles,
                            tp_proxy if cfg.entry.cancel_if_target_reached else None)
            info["entry_status"] = er.status
            if er.status == "WAITING":
                decisions.append(Decision(setup_id, bias, WAITING, "entry", {"reason": er.reason, "zone": er.zone.as_dict() if er.zone else None}))
                continue
            if er.status != "ENTRY":
                res = INVALIDATED if er.status in ("INVALIDATED", "CANCELLED_TP_FIRST", "NO_ENTRY_ZONE") else REJECTED
                decisions.append(Decision(setup_id, bias, res if er.status != "EXPIRED" else "EXPIRED", "entry",
                                          {"reason": er.reason or er.status}))
                continue

            # ---- entry confirmed: full qualification ---------------------------------------
            entry = er.entry_price
            sl = risk.stop_loss(bias, sw.extreme, a15, cfg.risk.stop_buffer_atr)
            tp = risk.take_profit(m15, sw15, entry, bias)
            checks = [c_h1, c_m15, c_al,
                      ok("valid_zone", zone=sw.zone_id), ok("valid_sweep", sweep=sw.id), ok("confirmed_mss", mss=mss.id),
                      ok("valid_entry_retest", zone=er.zone.as_dict())]
            # entry must be fresh (latest or previous M1 candle) -- otherwise the signal is already history
            fresh = er.entry_time >= now - pd.Timedelta(seconds=2 * TF_SECONDS["M1"])
            healthy = all(data_health.get(tf, False) for tf in ("M1", "M5", "M15", "H1"))
            checks.append(ok("fresh_data") if fresh and healthy else
                          unavailable("fresh_data", "ENTRY_NOT_FRESH" if not fresh else "TIMEFRAME_UNHEALTHY",
                                      health=data_health))
            checks.append(check_volatility(atr15, cfg.volatility.lookback, cfg.volatility.lower_percentile,
                                           cfg.volatility.upper_percentile))
            stop_c = risk.check_stop(bias, entry, sl, a15, cfg.risk.maximum_stop_atr_multiple)
            checks += [stop_c, risk.check_target(tp)]
            stop_dist = abs(entry - sl)
            if tp is not None and stop_c.status == PASS:
                raw, adj = risk.reward_risk(bias, entry, sl, tp, spread_value)
                checks.append(risk.check_rr(raw, adj, cfg.risk.minimum_reward_risk))
            else:
                raw = adj = 0.0
                checks.append(unavailable("minimum_reward_risk", "NO_TP_OR_SL"))
            checks.append(spread_check(stop_dist))
            checks.append(news_check)
            conflict = [s for s in active_signals if s.get("direction") != bias] or \
                       (len(active_signals) >= cfg.risk.maximum_active_signals) or got_qualified
            checks.append(fail("no_conflicting_setup", "ACTIVE_SIGNAL_EXISTS") if conflict else ok("no_conflicting_setup"))
            checks.append(ok("no_invalidation"))
            q = qualify(checks)
            d = Decision(setup_id, bias, q.result, "qualification",
                         {"entry": entry, "sl": sl, "tp": tp, "rr": round(adj, 4), "raw_rr": round(raw, 4)}, q)
            if q.result == QUALIFIED:
                assert adj >= cfg.risk.minimum_reward_risk and tp is not None  # defence in depth (rule #4)
                got_qualified = True
                d.signal = {"setup_id": setup_id, "direction": bias, "entry": entry,
                            "entry_low": er.zone.lower, "entry_high": er.zone.upper, "sl": sl, "tp": tp,
                            "rr": round(adj, 2), "created_at": now.isoformat(), "zone_id": sw.zone_id,
                            "sweep_id": sw.id, "mss_id": mss.id,
                            "expiry": (mss.close_time + pd.Timedelta(minutes=cfg.signals.expiry_after_mss_minutes)).isoformat(),
                            "entry_candle_time": er.candle_time.isoformat(), "strategy_version": cfg.version_hash()}
            decisions.append(d)
        return CycleResult(now, trace, decisions)
