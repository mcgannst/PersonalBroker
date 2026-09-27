# Trader — To-Do

*Last updated: 2026-09-26. The design docs are in [`docs/BRD.md`](docs/BRD.md) and [`docs/SPEC.md`](docs/SPEC.md) (v1.0, approved 2026-09-26).*

## Open decisions and questions (Stephen)

- [x] **PostgreSQL version** on `192.168.68.86`: 14.24 (Ubuntu 22.04), checked 2026-09-26. The design needs version 13 or later.
- [x] **Hostnames:** `trader-dev.sunspinner.ca` (dev) and `trader.sunspinner.ca` (prod), confirmed 2026-09-26.
- [x] **Claude API budget:** US$1/day for dev, confirmed 2026-09-26 (estimated normal use US$0.20–0.60 per trading day).
- [x] **Pre-market candidate cap:** classify only the top 50 by gap %, adjustable in Settings. Decided 2026-09-26 (SPEC §4.3).
- [x] **Anthropic key:** Trader gets its own key, separate from FinanceTracker's, confirmed 2026-09-26.
- [x] **Questrade:** one login can have more than one API personal app, confirmed by Stephen 2026-09-26. Questrade's getting-started docs list apps under "personal applications", and each app gets its own consumer key (`client_id`) and its own manual authorization token. Checked 2026-09-26. S1 still has to prove that refreshing one app's token doesn't invalidate another's.
- [x] **Sign off** on the BRD and SPEC: approved as v1.0 on 2026-09-26.

## Setup tasks before Phase 0 (Stephen)

- [x] Register a **Questrade API personal app "Trader-dev"** in the API Centre and generate a manual refresh token. Done 2026-09-26; the token is in `docker/.env.dev` (git-ignored) and hasn't been used yet. Use it in S1 before it expires.
- [x] Create an **Anthropic API key "trader-dev"** in the Anthropic Console. Don't use FinanceTracker's. Done 2026-09-26 in a separate "Trader" workspace; verified, and saved in `docker/.env.dev` (git-ignored). Optional: set a monthly spend limit on the workspace as a backstop.
- [ ] Create a **Telegram bot for dev** with @BotFather, and get your chat ID.
- [x] Create the **`trader_dev` database** and roles `trader_dev_owner` / `trader_dev_app` on `192.168.68.86`. Done 2026-09-26: schema `trader` owned by `trader_dev_owner`; the app role gets read/write on new tables through default privileges and can't create or drop tables. Connection URLs are in `docker/.env.dev` (git-ignored).
- [x] Add a **Pi-hole v6 Local DNS record**: `trader-dev.sunspinner.ca` → `192.168.68.73`. Done 2026-09-26 on Pi-hole (Proxmox LXC 102, `192.168.68.84`); resolves correctly.
- [x] Add an **NPM proxy host** for `trader-dev.sunspinner.ca`, with an Access List allowing only `192.168.68.0/22` (the home LAN is a /22, not a /24). Done 2026-09-26: proxy host 4 → `http://trader-dev:8000`, access list 1 "Home LAN only", websockets on, block exploits on. Returns 502 until the container exists.
- [ ] Add the **TLS certificate** for `trader-dev.sunspinner.ca` in NPM (edit proxy host 4 → SSL → request new → DNS challenge, Cloudflare), then turn on Force SSL and HTTP/2. The NPM API doesn't expose the stored Cloudflare token, so this needs the Cloudflare API token or doing it in the NPM UI.
- [x] Confirm the existing Postgres **backup** will include the new `trader_dev` database. Yes: the Postgres host is backed up as a whole Proxmox VM (per Stephen, 2026-08-31, recorded in RetirementPlanner's TODO), so every database on it is included.

## Separate security note (FinanceTracker, not Trader)

- [x] Checked 2026-09-26: FinanceTracker's production `SECRET_KEY` is a 64-character hex key with no placeholder text, so it looks like a real random key and nothing needs changing. Original item: check whether FinanceTracker's **production** `backend/.env.prod` still has the placeholder `SECRET_KEY`. That key protects the stored Questrade and Anthropic tokens. If it does, change it, then paste a new Questrade refresh token into FinanceTracker, because changing the key makes the stored tokens unreadable.

## Phase 0 spikes (SPEC §17)

- [ ] S1: Questrade token refresh and rotation, with an independent token chain per app
- [ ] S2: Quote freshness (are quotes real-time or delayed?)
- [ ] S3: Candle limit per request, and how far back history goes
- [ ] S4: Universe scan at 9:35 finishes in under 60 s
- [ ] S5: FinViz scrape (parsing works; not blocked)
- [ ] S6: Telegram approve-button round-trip in under 2 s
