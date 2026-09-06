# FinCL interface

React/TypeScript, Vite, Tailwind, project-owned shadcn/ui primitives, and Recharts.
The app is served by FastAPI from the Python package; no Node server is needed at runtime.

From the repository root:

```bash
uv sync
npm ci --prefix frontend
npm run build --prefix frontend
uv run fincl serve --database var/fincl.sqlite3 --evidence-dir var/evidence --open
```

To open the UI from a phone on the same trusted Wi-Fi network, use this computer's
private IPv4 address (for example, the `192.168.x.x` value shown by `hostname -I`):

```bash
uv run fincl serve --database var/fincl.sqlite3 --evidence-dir var/evidence --host 192.168.0.10
```

Open the printed website address on the phone. This is plain local-network
HTTP, so use it only on a private network you trust; do not use `0.0.0.0`.

The ledger must already use the current schema. `serve` refuses an absent or old
ledger, refreshes an existing supported ledger, and prints a normal website address.
The access-key gate is currently disabled by default; bookmarks and multiple devices
can open the site without a key. To re-enable the preserved one-use key and session
gate, add `--require-access-key` to `fincl serve`. Same-origin and CSRF protections
remain enabled in both modes. Stop the server with Ctrl-C.
For an old ledger, follow the supported backup-and-migrate process in the root README.

To explore without using personal data, choose a new directory:

```bash
uv run python scripts/create_demo.py /tmp/fincl-demo
uv run fincl serve --database /tmp/fincl-demo/demo.sqlite3 --demo --open
```

The demo is an actual isolated ledger populated by synthetic statement imports.
Edits persist there. It is not substituted when a real ledger is empty or unavailable.

## Main workspaces

- **Overview:** exact spending, classified income, recorded net flow, category donut,
  latest outgoings, review queue shortcuts, and inflow interpretation.
- **Analysis:** six complete calendar months of income versus purchases/fees,
  exact chart-value table, category and merchant rankings, and drill-down links.
- **Transactions:** literal search, category/account/type filters, exact amount
  sorting, and 50-row **server-paged** results (`GET /api/v1/transactions`,
  keyset cursor, generation-pinned) — filtering, sorting and paging are SQL, not
  client-side over a full-period dump. Original descriptions, source locators and
  verified downloads. Category/inflow edits require a reason and preview before
  durable save.
- **Review:** proposed same-currency transfer pairs that automatic matching could
  not confirm uniquely. Confirming records both legs as one internal transfer
  (they leave spending and income); rejecting keeps them as ordinary
  transactions. Both need a reason and preview. Currency exchanges and manual
  links stay in the CLI.
- **Recurring:** inspect similar weekly/monthly charges with at least three
  occurrences. These are candidates, not a subscription registry or bill-payment service.
- **Plans:** create/edit reusable monthly category/all-spending budgets and manual
  savings goals. Progress comparisons require a full covered month in the plan currency.
- **Categories:** built-in groups plus journal-backed custom groups/categories.
- **History:** the last 50 originating decisions, including saves made from the UI.

## Data and implementation boundaries

No private financial records are kept in browser persistent storage. All financial
amounts arrive as decimal strings. Only chart coordinates and visual progress ratios
use JavaScript numbers; display formatting and amount sorting preserve exact digits.
Currencies stay separate. Coverage gaps withhold totals instead of displaying zero.
Net flow is not net worth, an account balance, or a savings rate.

`CATEGORY_CREATE` and `PLAN_SET` add journal vocabulary, without new SQL tables.
Older builds deliberately refuse journals containing unknown kinds; upgrade readers
before reusing these journals elsewhere. Custom category removal/renaming, automatic
subscription cancellation, bank syncing, and budget forecasts are not implemented.
The transaction list is served page-by-page from `GET /api/v1/transactions`:
every filter, the sort order, and forward/back paging are SQL against a
generation-pinned snapshot, so a large ledger never ships a whole period at
once. The dashboard payload carries only what Overview draws (a short `recent`
list and a `review_count`). Paging is a forward keyset cursor with a
client-held stack for "Previous"; a stale cursor or generation returns 409 and
the list reloads from page one. Currency-exchange links, manual links, import,
backup, migration and repair use the CLI.

Each Overview total and ranking row carries a `handle` (and the summary a
parallel `summary_handles` map). `GET /api/v1/contributors/{handle}` keyset-pages
the exact economic components behind that total from the shared schema-10
projection — same generation, same window; a stale handle is 409, a tampered one
422.

The **Review** tab's `src/features/review/RelationshipEditor` fetches
`GET /api/v1/relationships/review` — entry pairs that match across two different
imports. "Same transaction" (`DUPLICATE_CONFIRM`) suppresses the candidate from
every total; "Distinct purchases" (`DUPLICATE_REJECT`) records the rejection.
Coupled corrections go through `POST /api/v1/batch`, one atomic `DECISION_BATCH`.

The **Analysis** screen (`src/features/analysis/*`) fetches `GET /api/v1/analysis`
in one generation-pinned read: the spending / fees / refunds / net summary with
previous-period deltas and `(current − previous) / previous × 100` percentages
(null on a zero or uncovered comparison), the six-month purchases + fees + income
bars, the "what changed?" category increases and decreases as separate lists, the
ranked merchants, and an exact contributor table opened from any of them. The
comparison mode and the account / category / merchant narrowing are local to
Analysis; the category and merchant narrowing copy to Transactions on request.
Period and currency stay in the shared scope bar.

Build static assets **before** building a Python wheel. The package-data rule includes
`static/index.html` and `static/assets/*`. Generated DTOs come from `docs/openapi.json`,
which is regenerated from the FastAPI app whenever a request/response model changes:

```bash
uv run python scripts/dump_openapi.py   # from the repo root
npm run types --prefix frontend
```

## Tests

```bash
npm run build --prefix frontend          # TypeScript compile + bundle
npm test --prefix frontend               # Vitest: money/compare helpers, App state matrix, Review tab
npm run test:browser --prefix frontend   # Playwright, desktop 1280x800 and phone 390x844
```

`test:browser` needs `npm run build` first (the app is served from the built
assets) and `npx playwright install chromium` once. `e2e/serve.py` launches an
isolated synthetic ledger; the setup project opens the site without a launch key. The browser flow covers overview totals,
a category edit surviving preview → save → full page reload, and the Review tab.
CLI/app decision parity and the backup-after-a-UI-write workflow are covered by
the Python suite (`tests/test_api.py`, `tests/test_mvp_backup_workflow.py`).
