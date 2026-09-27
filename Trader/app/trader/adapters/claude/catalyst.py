"""Claude catalyst classification (SPEC §4.3, BR-03, BR-05), its store, and the service strategies call.

The Anthropic client is injected (tests never reach the network). Every call's cost is stored, and the daily
budget (claude.daily_budget_usd, per session) stops further calls: the name is stored as `unknown` and an
error event is logged. Replay (P5) passes classifier=None so it never calls Claude.
"""

import asyncio
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass
from datetime import date
from decimal import ROUND_HALF_UP, Decimal
from typing import Any, Literal, Protocol

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator
from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.orm import Session, sessionmaker

from trader.adapters.finviz.parser import Headline
from trader.adapters.finviz.scraper import FinvizError
from trader.db import models as m
from trader.db.session import session_scope
from trader.events import log_event
from trader.market.clock import Clock, et_date
from trader.settings_store import RuntimeSettings

CatalystType = Literal[
    "earnings_beat",
    "earnings_miss",
    "guidance",
    "analyst_action",
    "m_and_a",
    "regulatory",
    "contract",
    "offering",
    "rumour",
    "none",
]
Direction = Literal["bullish", "bearish", "neutral"]
CATALYST_TYPES: tuple[str, ...] = (
    "earnings_beat",
    "earnings_miss",
    "guidance",
    "analyst_action",
    "m_and_a",
    "regulatory",
    "contract",
    "offering",
    "rumour",
    "none",
)
DIRECTIONS: tuple[str, ...] = ("bullish", "bearish", "neutral")
PRICES_PER_MTOK: dict[str, tuple[Decimal, Decimal]] = {
    "claude-sonnet-5": (Decimal("2"), Decimal("10")),
    "claude-haiku-4-5": (Decimal("1"), Decimal("5")),
}
MAX_HEADLINES = 10
MAX_REASON_WORDS = 30
MAX_TOKENS = 1024
OVER_CAP = "not classified (over cap)"
NOT_CONFIGURED = "claude not configured"
SOURCE = "claude.catalyst"
Q6 = Decimal("0.000001")


class CatalystResult(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    catalyst_type: CatalystType
    direction: Direction
    quality: int = Field(ge=0, le=100)
    is_confirmed: bool
    reason: str

    @field_validator("reason")
    @classmethod
    def _thirty_words(cls, v: str) -> str:
        return " ".join(v.split()[:MAX_REASON_WORDS])


CATALYST_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "catalyst_type": {"type": "string", "enum": list(CATALYST_TYPES)},
        "direction": {"type": "string", "enum": list(DIRECTIONS)},
        "quality": {
            "type": "integer",
            "description": "0-100: how strong, specific and confirmed the catalyst is",
        },
        "is_confirmed": {"type": "boolean"},
        "reason": {"type": "string", "description": "At most 30 words"},
    },
    "required": ["catalyst_type", "direction", "quality", "is_confirmed", "reason"],
    "additionalProperties": False,
}

SYSTEM_PROMPT = (
    "You classify the news catalyst behind a US stock's pre-market move, for a day-trading simulator. "
    "Use only the headlines given; some may be about other companies, so ignore those. "
    "catalyst_type is 'none' when no headline explains a move. direction is the likely price impact. "
    "quality is 0-100: 80 or more for a confirmed, company-specific, material event; 50-79 for plausible but "
    "weaker news; below 50 for vague, stale, or unconfirmed news. is_confirmed is true only when a headline "
    "states the event as fact. reason is at most 30 words and must not invent facts or numbers."
)


@dataclass(frozen=True, slots=True)
class CatalystInput:
    ticker: str
    company: str
    headlines: tuple[Headline, ...]
    gap_pct: Decimal | None
    earnings_date: date | None


def build_prompt(inp: CatalystInput) -> str:
    gap = f"{inp.gap_pct * 100:+.2f}%" if inp.gap_pct is not None else "unknown"
    lines = [
        f"Ticker: {inp.ticker}",
        f"Company: {inp.company or 'unknown'}",
        f"Pre-market gap: {gap}",
        f"Earnings date: {inp.earnings_date.isoformat() if inp.earnings_date else 'none known'}",
        "Headlines (newest first, UTC):",
    ]
    newest = sorted(inp.headlines, key=lambda h: h.ts, reverse=True)[:MAX_HEADLINES]
    lines += [f"- {h.ts.isoformat()} [{h.source}] {h.title}" for h in newest] or ["- (none)"]
    return "\n".join(lines)


@dataclass(frozen=True, slots=True)
class Classification:
    status: Literal["classified", "budget_exceeded", "error"]
    result: CatalystResult | None
    model: str
    input_tokens: int = 0
    output_tokens: int = 0
    cost_usd: Decimal = Decimal(0)
    error: str | None = None


def cost_usd(model: str, input_tokens: int, output_tokens: int) -> Decimal:
    price_in, price_out = PRICES_PER_MTOK[model]
    return ((input_tokens * price_in + output_tokens * price_out) / Decimal(1_000_000)).quantize(
        Q6, ROUND_HALF_UP
    )


class CatalystClassifier:
    def __init__(self, client: Any, settings: Callable[[], RuntimeSettings]) -> None:
        self._client = client
        self._settings = settings

    async def classify(self, inp: CatalystInput, spent_usd: Decimal) -> Classification:
        s = self._settings()
        model = s.claude_model
        if spent_usd >= s.claude_daily_budget_usd:
            return Classification(
                "budget_exceeded", None, model, error=f"daily budget US${s.claude_daily_budget_usd} reached"
            )
        try:
            resp = await self._client.messages.create(
                model=model,
                max_tokens=MAX_TOKENS,
                thinking={"type": "disabled"},
                system=SYSTEM_PROMPT,
                messages=[{"role": "user", "content": build_prompt(inp)}],
                output_config={"format": {"type": "json_schema", "schema": CATALYST_SCHEMA}},
            )
        except Exception as exc:  # the SDK raises many error types; any failure means "unknown"
            return Classification("error", None, model, error=f"{type(exc).__name__}: {exc}"[:300])
        usage = getattr(resp, "usage", None)
        tin = int(getattr(usage, "input_tokens", 0) or 0)
        tout = int(getattr(usage, "output_tokens", 0) or 0)
        cost = cost_usd(model, tin, tout)
        stop = getattr(resp, "stop_reason", None)
        if stop != "end_turn":
            return Classification("error", None, model, tin, tout, cost, f"stop_reason={stop}")
        text = next((b.text for b in resp.content if getattr(b, "type", None) == "text"), None)
        try:
            result = CatalystResult.model_validate_json(text or "")
        except ValidationError as exc:
            return Classification(
                "error", None, model, tin, tout, cost, f"invalid output: {exc.error_count()} errors"
            )
        return Classification("classified", result, model, tin, tout, cost)


@dataclass(frozen=True, slots=True)
class StoredCatalyst:
    symbol_id: int
    catalyst_type: str
    direction: str
    quality: int | None
    confirmed: bool | None
    reason: str | None
    model: str | None
    cost_usd: Decimal
    classified: bool


def _stored(row: m.Catalyst) -> StoredCatalyst:
    return StoredCatalyst(
        row.symbol_id,
        row.catalyst_type,
        row.direction,
        row.quality,
        row.confirmed,
        row.reason,
        row.model,
        row.cost_usd,
        row.classified_at is not None,
    )


def _headlines_json(headlines: Iterable[Headline]) -> list[dict[str, str]]:
    return [{"ts": h.ts.isoformat(), "title": h.title, "source": h.source, "url": h.url} for h in headlines]


class CatalystStore:
    def __init__(self, factory: sessionmaker[Session], clock: Clock) -> None:
        self._factory = factory
        self._clock = clock

    def spent(self, session_date: date) -> Decimal:
        with self._factory() as s:
            total = s.execute(
                select(func.coalesce(func.sum(m.Catalyst.cost_usd), 0)).where(
                    m.Catalyst.session_date == session_date
                )
            ).scalar_one()
        return Decimal(total)

    def get(self, symbol_ids: Sequence[int], session_date: date) -> dict[int, StoredCatalyst]:
        with self._factory() as s:
            rows = s.execute(
                select(m.Catalyst).where(
                    m.Catalyst.session_date == session_date, m.Catalyst.symbol_id.in_(list(symbol_ids))
                )
            ).scalars()
            return {r.symbol_id: _stored(r) for r in rows}

    def save(
        self,
        symbol_id: int,
        session_date: date,
        *,
        headlines: Iterable[Headline],
        gap_pct: Decimal | None,
        earnings_date: date | None,
        classification: Classification | None,
        note: str | None = None,
    ) -> StoredCatalyst:
        now = self._clock.now()
        res = classification.result if classification else None
        # Core insert: keys are column names. The ORM attribute catalyst_type maps to the column "type".
        values: dict[str, Any] = {
            "symbol_id": symbol_id,
            "session_date": session_date,
            "headlines": _headlines_json(headlines),
            "gap_pct": gap_pct,
            "earnings_date": earnings_date,
            "type": res.catalyst_type if res else "unknown",
            "direction": res.direction if res else "neutral",
            "quality": res.quality if res else None,
            "confirmed": res.is_confirmed if res else None,
            "reason": res.reason if res else (note or (classification.error if classification else None)),
            "model": classification.model if classification else None,
            "cost_usd": classification.cost_usd if classification else Decimal(0),
            "input_tokens": classification.input_tokens if classification else 0,
            "output_tokens": classification.output_tokens if classification else 0,
            "classified_at": now if res else None,
            "created_at": now,
        }
        stmt = pg_insert(m.Catalyst).values(**values)
        replace = (
            "headlines",
            "gap_pct",
            "earnings_date",
            "type",
            "direction",
            "quality",
            "confirmed",
            "reason",
            "model",
            "classified_at",
        )
        set_: dict[str, Any] = {k: stmt.excluded[k] for k in replace}
        set_["cost_usd"] = m.Catalyst.cost_usd + stmt.excluded["cost_usd"]
        set_["input_tokens"] = func.coalesce(m.Catalyst.input_tokens, 0) + stmt.excluded["input_tokens"]
        set_["output_tokens"] = func.coalesce(m.Catalyst.output_tokens, 0) + stmt.excluded["output_tokens"]
        with session_scope(self._factory) as s:
            s.execute(
                stmt.on_conflict_do_update(
                    constraint="uq_catalysts_symbol_session",
                    set_=set_,
                    where=m.Catalyst.classified_at.is_(None),  # a classified row is never overwritten
                )
            )
        return self.get([symbol_id], session_date)[symbol_id]


@dataclass(frozen=True, slots=True)
class CatalystRequest:
    symbol_id: int
    ticker: str
    company: str = ""
    gap_pct: Decimal | None = None
    earnings_date: date | None = None


class HeadlineSource(Protocol):
    def news(self, ticker: str, today_et: date) -> list[Headline]: ...


class CatalystService:
    def __init__(
        self,
        factory: sessionmaker[Session],
        clock: Clock,
        store: CatalystStore,
        classifier: CatalystClassifier | None,
        headlines: HeadlineSource | None,
        *,
        max_concurrency: int = 4,
    ) -> None:
        self._factory = factory
        self._clock = clock
        self._store = store
        self._classifier = classifier
        self._headlines = headlines
        self._max = max_concurrency

    async def get(self, symbol_ids: Sequence[int], session_date: date) -> dict[int, StoredCatalyst]:
        wanted = list(dict.fromkeys(symbol_ids))
        stored = self._store.get(wanted, session_date)
        todo = [sid for sid in wanted if sid not in stored or not stored[sid].classified]
        if todo:
            stored.update(await self.classify_many(self._requests(todo, session_date), session_date))
        return {sid: stored[sid] for sid in wanted if sid in stored}

    def _requests(self, symbol_ids: Sequence[int], session_date: date) -> list[CatalystRequest]:
        with self._factory() as s:
            names = {
                sid: (t, n)
                for sid, t, n in s.execute(
                    select(m.Symbol.id, m.Symbol.ticker, m.Symbol.name).where(
                        m.Symbol.id.in_(list(symbol_ids))
                    )
                ).all()
            }
            known = {
                sid: (g, e)
                for sid, g, e in s.execute(
                    select(m.Catalyst.symbol_id, m.Catalyst.gap_pct, m.Catalyst.earnings_date).where(
                        m.Catalyst.session_date == session_date, m.Catalyst.symbol_id.in_(list(symbol_ids))
                    )
                ).all()
            }
        return [
            CatalystRequest(sid, names[sid][0], names[sid][1] or "", *known.get(sid, (None, None)))
            for sid in symbol_ids
            if sid in names
        ]

    def mark_unclassified(
        self, requests: Sequence[CatalystRequest], session_date: date, note: str
    ) -> dict[int, StoredCatalyst]:
        return {
            r.symbol_id: self._store.save(
                r.symbol_id,
                session_date,
                headlines=(),
                gap_pct=r.gap_pct,
                earnings_date=r.earnings_date,
                classification=None,
                note=note,
            )
            for r in requests
        }

    async def classify_many(
        self, requests: Sequence[CatalystRequest], session_date: date
    ) -> dict[int, StoredCatalyst]:
        existing = self._store.get([r.symbol_id for r in requests], session_date)
        out = {sid: c for sid, c in existing.items() if c.classified}
        pending = [r for r in requests if r.symbol_id not in out]
        if not pending:
            return out
        if self._classifier is None or self._headlines is None:
            out.update(self.mark_unclassified(pending, session_date, NOT_CONFIGURED))
            return out
        classifier, source = self._classifier, self._headlines
        today = et_date(self._clock.now())
        inputs: list[tuple[CatalystRequest, tuple[Headline, ...]]] = []
        for r in pending:  # one at a time: FinViz politeness, and the scraper isn't thread-safe
            try:
                found = await asyncio.to_thread(source.news, r.ticker, today)
            except FinvizError as exc:
                out.update(self.mark_unclassified([r], session_date, f"headlines unavailable: {exc}"))
                continue
            inputs.append((r, tuple(sorted(found, key=lambda h: h.ts, reverse=True)[:MAX_HEADLINES])))
        gate = asyncio.Semaphore(self._max)
        budget_lock = asyncio.Lock()

        async def one(r: CatalystRequest, heads: tuple[Headline, ...]) -> None:
            async with gate:
                async with budget_lock:
                    spent = self._store.spent(session_date)
                c = await classifier.classify(
                    CatalystInput(r.ticker, r.company, heads, r.gap_pct, r.earnings_date), spent
                )
                out[r.symbol_id] = self._store.save(
                    r.symbol_id,
                    session_date,
                    headlines=heads,
                    gap_pct=r.gap_pct,
                    earnings_date=r.earnings_date,
                    classification=c,
                )
                if c.status != "classified":
                    self._event(
                        "error" if c.status == "budget_exceeded" else "warning",
                        f"Claude daily budget reached: {r.ticker} not classified"
                        if c.status == "budget_exceeded"
                        else f"catalyst for {r.ticker} not classified: {c.error}",
                        {"ticker": r.ticker, "status": c.status, "spent_usd": str(spent)},
                    )

        await asyncio.gather(*(one(r, heads) for r, heads in inputs))
        return out

    def _event(self, level: str, message: str, data: dict[str, Any]) -> None:
        with session_scope(self._factory) as s:
            log_event(s, self._clock, level, SOURCE, message, data)
