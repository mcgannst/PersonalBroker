# Trader — To-Do

*Last updated: 2026-09-26. The design docs are in [`docs/BRD.md`](docs/BRD.md) and [`docs/SPEC.md`](docs/SPEC.md) (v0.4).*

## Open decisions and questions (Stephen)

- [x] **PostgreSQL version** on `192.168.68.86`: 14.24 (Ubuntu 22.04), checked 2026-09-26. The design needs version 13 or later.
- [ ] **Hostnames:** confirm `trader-dev.sunspinner.ca` (dev) and `trader.sunspinner.ca` (prod).
- [ ] **Claude API budget:** confirm US$1/day for dev.
- [ ] **Anthropic key:** reuse FinanceTracker's key, or create a separate key for Trader so costs are tracked separately?
- [ ] **Questrade:** confirm that one login can have more than one API personal app (FinanceTracker, Trader-dev, and later Trader).
- [ ] **Sign off** on the BRD and SPEC.

## Setup tasks before Phase 0 (Stephen)

- [ ] Register a **Questrade API personal app "Trader-dev"** in the API Centre and generate a manual refresh token. Don't use FinanceTracker's.
- [ ] Create a **Telegram bot for dev** with @BotFather, and get your chat ID.
- [ ] Create the **`trader_dev` database** and roles `trader_dev_owner` / `trader_dev_app` on `192.168.68.86`.
- [ ] Add a **Pi-hole v6 Local DNS record**: `trader-dev.sunspinner.ca` → `192.168.68.73`.
- [ ] Add an **NPM proxy host** for `trader-dev.sunspinner.ca`, with an Access List allowing only `192.168.68.0/24`.
- [ ] Confirm the existing Postgres **backup** will include the new `trader_dev` database.

## Separate security note (FinanceTracker, not Trader)

- [ ] Check whether FinanceTracker's **production** `backend/.env.prod` still has the placeholder `SECRET_KEY`. That key protects the stored Questrade and Anthropic tokens. If it does, change it, then paste a new Questrade refresh token into FinanceTracker, because changing the key makes the stored tokens unreadable.

## Phase 0 spikes (SPEC §17)

- [ ] S1: Questrade token refresh and rotation, with an independent token chain per app
- [ ] S2: Quote freshness (are quotes real-time or delayed?)
- [ ] S3: Candle limit per request, and how far back history goes
- [ ] S4: Universe scan at 9:35 finishes in under 60 s
- [ ] S5: FinViz scrape (parsing works; not blocked)
- [ ] S6: Telegram approve-button round-trip in under 2 s
