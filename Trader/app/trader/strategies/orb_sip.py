"""orb_sip 1.0.0: the 5-minute Opening Range Breakout on Stocks in Play (SPEC §5.2, BR-04, BR-11, BR-13)."""

from dataclasses import dataclass
from decimal import ROUND_HALF_UP, Decimal
from typing import Any, Literal, Self

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from trader.broker.types import Q4, Fill, dec_str
from trader.market.calendar import SessionCalendar
from trader.market.indicators import is_bearish, is_doji, rvol
from trader.market.types import Candle, UniverseMember
from trader.strategies.base import (
    Cancel,
    CandidateRecord,
    CatalystInfo,
    EnterLong,
    Exit,
    Intent,
    ScheduledEvent,
    SessionOffset,
    StrategyContext,
)

ORB_EVENT = "orb_open"
CANCEL_EVENT = "entry_cancel"
FLATTEN_EVENT = "flatten"
ORB_AT = "open+5m5s"  # 9:35:05 ET: five seconds after the opening bar closes
ORB_AT_SECONDS = SessionOffset.parse(ORB_AT).seconds
NO_CATALYST = frozenset({"none", "unknown"})


def _candle_json(c: Candle) -> dict[str, Any]:
    return {
        "start": c.start.isoformat(),
        "open": str(c.open),
        "high": str(c.high),
        "low": str(c.low),
        "close": str(c.close),
        "volume": c.volume,
    }


def _catalyst_json(cat: CatalystInfo | None) -> dict[str, Any] | None:
    if cat is None:
        return None
    return {
        "type": cat.catalyst_type,
        "direction": cat.direction,
        "quality": cat.quality,
        "classified": cat.classified,
    }


@dataclass(frozen=True, slots=True)
class _Scored:
    """One opening bar that met rvol_min, with its typed inputs (no re-parsing of evidence strings)."""

    rvol: Decimal
    ticker: str
    symbol_id: int
    candle: Candle
    atr14: Decimal | None
    atr_source: str | None  # open_bar_stats | universe | None


class OrbSipParams(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    price_min: Decimal = Field(Decimal("5"), gt=0, allow_inf_nan=False)
    price_max: Decimal = Field(Decimal("50"), gt=0, allow_inf_nan=False)
    min_avg_volume: int = Field(1_000_000, ge=0)
    min_atr: Decimal = Field(Decimal("0.50"), ge=0, allow_inf_nan=False)
    rvol_min: Decimal = Field(Decimal("1.00"), ge=0, allow_inf_nan=False)
    top_n: int = Field(20, ge=1, le=200)
    max_positions: int = Field(1, ge=1, le=10)
    require_catalyst: bool = True
    catalyst_min_quality: int = Field(50, ge=0, le=100)
    stop_atr_fraction: Decimal = Field(Decimal("0.10"), gt=0, le=Decimal("1"), allow_inf_nan=False)
    entry_offset: Decimal = Field(Decimal("0.01"), ge=0, le=Decimal("5"), allow_inf_nan=False)
    entry_cancel_at: str | None = "open+120m"
    exit_at: str = "close-10m"
    doji_body_pct_max: Decimal = Field(Decimal("0.10"), ge=0, le=Decimal("1"), allow_inf_nan=False)
    stale_universe: Literal["skip", "trade"] = "skip"

    @field_validator("entry_cancel_at")
    @classmethod
    def _cancel_after_orb(cls, v: str | None) -> str | None:
        if v is None:
            return v
        off = SessionOffset.parse(v)
        if off.anchor == "open" and off.seconds <= ORB_AT_SECONDS:
            raise ValueError(f"entry_cancel_at must be later than the ORB event ({ORB_AT})")
        if off.anchor == "close" and off.seconds >= 0:
            raise ValueError("entry_cancel_at must be before the close (e.g. 'close-60m')")
        return v

    @field_validator("exit_at")
    @classmethod
    def _exit_before_close(cls, v: str) -> str:
        off = SessionOffset.parse(v)
        if off.anchor != "close" or off.seconds >= 0:
            raise ValueError("exit_at must be a negative offset from the close (e.g. 'close-10m')")
        return v

    @model_validator(mode="after")
    def _price_band(self) -> Self:
        if self.price_min >= self.price_max:
            raise ValueError("price_min must be below price_max")
        return self


class OrbSip:
    key = "orb_sip"
    version = "1.0.0"
    kind: Literal["entry", "overlay"] = "entry"
    params_model = OrbSipParams

    def __init__(self, params: OrbSipParams | None = None) -> None:
        self.params = params or OrbSipParams()

    def schedule(self, cal: SessionCalendar) -> list[ScheduledEvent]:
        events = [ScheduledEvent(ORB_EVENT, SessionOffset.parse(ORB_AT))]
        if self.params.entry_cancel_at is not None:
            events.append(ScheduledEvent(CANCEL_EVENT, SessionOffset.parse(self.params.entry_cancel_at)))
        events.append(ScheduledEvent(FLATTEN_EVENT, SessionOffset.parse(self.params.exit_at)))
        return events

    async def on_event(self, ctx: StrategyContext, event: ScheduledEvent) -> list[Intent]:
        if event.key == ORB_EVENT:
            return await self._orb(ctx)
        entries = [o for o in ctx.working_orders if o.purpose == "entry"]
        if event.key == CANCEL_EVENT:
            return [Cancel(o.id, "entry_cancel_at") for o in entries]
        if event.key == FLATTEN_EVENT:
            cancels: list[Intent] = [Cancel(o.id, "flatten_close") for o in entries]
            exits: list[Intent] = [Exit(p.id, "market", None, "flatten_close") for p in ctx.positions]
            return cancels + exits
        return []

    async def on_fill(self, ctx: StrategyContext, fill: Fill) -> list[Intent]:
        if fill.purpose != "entry":
            return []
        if fill.stop_loss is None:
            ctx.note(
                "orb: entry fill has no stop_loss; no protective stop placed",
                level="error",
                position_id=fill.position_id,
                order_id=fill.order_id,
                symbol_id=fill.symbol_id,
            )
            return []
        return [Exit(fill.position_id, "stop", fill.stop_loss, "protective_stop")]

    # --- the 9:35 scan ----------------------------------------------------------------------------------
    async def _orb(self, ctx: StrategyContext) -> list[Intent]:
        p = self.params
        held = {x.symbol_id for x in ctx.positions}
        entry_orders = [o for o in ctx.working_orders if o.purpose == "entry"]
        working = {o.symbol_id for o in entry_orders}
        # entries_today counts today's entry proposals, which already cover today's open positions and
        # working entries; the larger of the two never double counts and still sees a carried position.
        used = max(ctx.entries_today, len(ctx.positions) + len(entry_orders))
        slots = p.max_positions - used
        if slots <= 0:
            ctx.note(
                "orb: max_positions already used today",
                entries_today=ctx.entries_today,
                open_positions=len(ctx.positions),
                working_entries=len(entry_orders),
            )
            return []
        status = await ctx.data.universe_status(ctx.session_date)
        if status.stale and p.stale_universe == "skip":
            ctx.note(
                "orb: the universe is a stale fallback; no entries today",
                level="error",
                fallback_from=str(status.fallback_from),
                age_sessions=status.age_sessions,
            )
            return []
        if status.source == "fallback":
            ctx.note(
                "orb: trading on a fallback universe",
                level="warning",
                fallback_from=str(status.fallback_from),
                stale=status.stale,
            )
        universe = await ctx.data.universe(ctx.session_date)
        if not universe:
            ctx.note("orb: no universe for this session", level="error")
            return []
        members = {u.symbol_id: u for u in universe}
        stats = await ctx.data.open_bar_stats(ctx.session_date)
        opening = await ctx.data.opening_bars(ctx.session_date, list(members))
        if opening.missing:
            ctx.note(
                f"orb: {len(opening.missing)} symbols have no opening bar",
                level="warning",
                missing={str(k): v for k, v in sorted(opening.missing.items())},
            )
        scored: list[_Scored] = []
        no_baseline: list[int] = []
        for sid, candle in opening.bars.items():
            st = stats.get(sid)
            r = rvol(candle.volume, st.avg_open_vol_14d if st else None)
            if r is None:
                no_baseline.append(sid)
            elif r >= p.rvol_min:
                member = members[sid]
                if st is not None and st.atr14 is not None:
                    atr14, atr_source = st.atr14, "open_bar_stats"
                elif member.atr14 is not None:
                    atr14, atr_source = member.atr14, "universe"
                else:
                    atr14, atr_source = None, None
                scored.append(_Scored(r, member.ticker, sid, candle, atr14, atr_source))
        if no_baseline:
            ctx.note("orb: no opening-volume baseline", symbol_ids=sorted(no_baseline))
        scored.sort(key=lambda t: (-t.rvol, t.ticker))

        records: list[CandidateRecord] = []
        survivors: list[CandidateRecord] = []
        levels: dict[int, tuple[Decimal, Decimal]] = {}  # symbol_id -> (entry, stop_loss)
        for rank, sc in enumerate(scored[: p.top_n], start=1):
            member = members[sc.symbol_id]
            st = stats.get(sc.symbol_id)
            rec = CandidateRecord(
                symbol_id=sc.symbol_id,
                rvol=sc.rvol,
                rank=rank,
                candle=_candle_json(sc.candle),
                data={
                    "ticker": sc.ticker,
                    "rvol": str(sc.rvol),
                    "rank": rank,
                    "direction": self._direction(sc.candle),
                    "atr14": dec_str(sc.atr14),
                    "atr_source": sc.atr_source,
                    "price": str(sc.candle.close),
                    "avg_volume": member.avg_volume,
                    "avg_open_vol_14d": dec_str(st.avg_open_vol_14d if st else None),
                    "universe_source": member.source,
                },
            )
            bar_source = opening.sources.get(sc.symbol_id)
            if bar_source is not None:  # QUOTEBAR: the live scan says where the bar came from
                rec.data["bar_source"] = bar_source
            if sc.symbol_id in held:
                rec.reject_reason = "already_held"
            elif sc.symbol_id in working:
                rec.reject_reason = "entry_working"
            else:
                rec.reject_reason = self._screen(sc.candle, sc.atr14, member)
            if rec.reject_reason is None and sc.atr14 is not None:
                entry = (sc.candle.high + p.entry_offset).quantize(Q4, ROUND_HALF_UP)
                stop_loss = (entry - p.stop_atr_fraction * sc.atr14).quantize(Q4, ROUND_HALF_UP)
                if stop_loss <= 0 or stop_loss >= entry:
                    rec.reject_reason = "stop_invalid"
                    rec.data.update(entry=str(entry), stop_loss=str(stop_loss))
                else:
                    levels[sc.symbol_id] = (entry, stop_loss)
            records.append(rec)
            if rec.reject_reason is None:
                survivors.append(rec)

        catalysts = (
            await ctx.catalysts.get([x.symbol_id for x in survivors], ctx.session_date) if survivors else {}
        )
        intents: list[Intent] = []
        for rec in survivors:
            cat = catalysts.get(rec.symbol_id)
            rec.data["catalyst"] = _catalyst_json(cat)
            reason = self._catalyst_reason(cat)
            if reason is None and len(intents) >= slots:
                reason = "lower_rank"
            if reason is not None:
                rec.reject_reason = reason
                continue
            entry, stop_loss = levels[rec.symbol_id]
            rec.passed = True
            rec.data.update(entry=str(entry), stop_loss=str(stop_loss))
            evidence = {**rec.data, "candle": rec.candle}
            intents.append(EnterLong(rec.symbol_id, "stop", entry, None, stop_loss, "orb_breakout", evidence))
        ctx.candidates.extend(records)
        ctx.note(
            "orb: ranked",
            ranked=len(records),
            selected=[x.data["ticker"] for x in records if x.passed],
        )
        return intents

    def _direction(self, c: Candle) -> str:
        try:
            if is_bearish(c):
                return "bearish"
            return "doji" if is_doji(c, self.params.doji_body_pct_max) else "bullish"
        except ValueError:
            return "malformed"

    def _screen(self, c: Candle, atr14: Decimal | None, member: UniverseMember) -> str | None:
        p = self.params
        try:
            if is_bearish(c):
                return "bearish_candle"
            if is_doji(c, p.doji_body_pct_max):
                return "doji"
        except ValueError:
            return "malformed_bar"
        if not p.price_min <= c.close <= p.price_max:
            return "price_out_of_range"
        if atr14 is None:
            return "atr_missing"
        if atr14 < p.min_atr:
            return "atr_below_min"
        if member.avg_volume is None or member.avg_volume < p.min_avg_volume:
            return "avg_volume_below_min"
        return None

    def _catalyst_reason(self, cat: CatalystInfo | None) -> str | None:
        p = self.params
        if not p.require_catalyst:
            return None
        if cat is None or not cat.classified or cat.catalyst_type in NO_CATALYST:
            return "catalyst_missing"
        if cat.direction == "bearish":
            return "catalyst_bearish"
        if cat.quality is None or cat.quality < p.catalyst_min_quality:
            return "catalyst_low_quality"
        return None
