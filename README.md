# FinCL

FinCL is a local, evidence-backed financial statement ledger. The implemented
vertical slices import Revolut CSV exports, DBS consolidated deposit statements,
DBS PayLah! statements, MariBank savings statements, Mari credit-card
statements, and Trust Bank Singapore savings statements without modifying the
source files or sending financial data to external services.

The authoritative design is [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md).
Superseded drafts are kept under [`docs/history/`](docs/history/) for reference
only. The completed immediate recovery and application design is in
[`docs/DESIGN-recovery-and-application.md`](docs/DESIGN-recovery-and-application.md).
Coverage intervals, journal association, verified reconstruction, and backup/restore
are implemented. The migration runner carries registered, checksummed upgrades
for every schema step from 8 to the current 23; shared application operations
(one ledger writer lock, request-id idempotency) and query generations are
implemented. Current rebuild requires the source database's
evidence inventory and FX inputs as well as the evidence vault and journal.

The completed design set is indexed in [`docs/DESIGN-INDEX.md`](docs/DESIGN-INDEX.md),
covering the dashboard, application/API, advanced models and operations. These
contracts do not imply those features are implemented.

## Primary implementation plan

[`docs/PLAN.md`](docs/PLAN.md) is the single list of what is done and what
remains, in priority order, with links to each item's detailed specification.

[The cash flow framework plan](docs/PLAN-cashflow-framework.md) keeps the
financial contract all product work follows. Deliver basic coverage of the complete personal-finance framework
first, using imported actual data: evidence readiness and baseline discovery,
cash position and connected obligations, six-month forecast, sinking funds and
spending controls, variance and tracked net worth. Do not require the user to
guess starting cash, salary or baseline spending. Add depth afterward.
The plan contains stage dependencies, acceptance cases and implementation handoffs.
[The MVP plan](docs/PLAN-mvp.md) retains historical delivery evidence.

## Implemented

- Byte-identical, SHA-256-addressed local evidence copies with verification
  before extraction.
- Exact integer-coefficient monetary storage; money never uses binary floating
  point or SQLite `REAL`.
- One `ledger_entries` table for every processed flow regardless of source (CSV
  export or parsed statement), with a `source_family` discriminator, exactly one
  raw provenance link, a rebuild-stable `natural_anchor`, and typed nullable
  source-specific columns. `transfer_links` carry real foreign keys into it.
- Revolut lifecycle preservation and per-currency-pocket running balances.
- DBS multi-currency PDF extraction with geometric anchors, rotated-text
  filtering, multiline descriptions, and exact balance walks.
- PayLah extraction for both observed layouts, missing-year inference bounded by
  the statement period, row coverage, references, and exact statement balances.
- Narrow deterministic DBS-to-PayLah, branded DBS-to-Revolut, DBS-to-MariBank
  (PayNow reference token + amount), and MariBank-savings-to-Mari-card repayment
  transfer links. DBS debit-card Revolut top-ups match on the real
  card-transaction date parsed from the merchant line (DBS posts the debit
  days later), not the posting date. Ambiguous matches are `PROPOSED` for
  review rather than guessed, except a balanced same-day group of identical
  DBS-to-PayLah top-ups (no shared reference, both legs owner-owned, every
  pairing nets alike), which is auto-confirmed on one deterministic bijection.
- MariBank savings extraction with two frozen interest profiles, daily-interest
  rows kept supporting, the credited-interest line at `MONTH` precision, and
  exact ending/summary/interest reconciliation.
- Trust Bank Singapore savings extraction: the printed statement period, the
  `Main Account` and `Savings` sub-account ledgers, balance-boundary rows kept
  supporting, foreign-currency purchases with their FCY amount and applied
  rate, and exact per-sub-account and printed-summary reconciliation.
- Mari credit-card extraction: posted and transaction dates kept separate,
  foreign amount/currency preserved with a magnitude-derived implied rate,
  installment occurrences recorded as facts, and two-layer reconciliation
  (section totals → the statement's own printed equation → current outstanding
  → statement due).
- An owner-only, fsync-durable hash-chained decision journal beside the database,
  with rebuilt SQLite projections for reviewed links, repairs, invariant
  escapes, inbound classifications, and categorization rules / aliases /
  overrides. The kind vocabulary is closed — replay refuses an unrecognized
  `decision_kind` rather than dropping it. Decisions whose anchor no longer
  resolves are surfaced as `dangling_decisions` in `report` / `coverage` and as
  a non-zero `rebuild` exit code.
- One analytically selected extraction run per evidence blob
  (`extraction_runs.selection_state`, a partial unique index), so a
  parser-version bump cannot double-count the ledger or split an override's
  binding; stale-only evidence is reported as `needs_reextraction`.
- Deterministic categorization (`fincl categorize`): a frozen `taxonomy-2026-v1`
  code constant, a versioned per-source field projection and merchant
  normalization, journaled `fincl rules` / `review categorize` authoring, a
  six-tier precedence engine (override → confirmed link / inbound →
  direct then alias-resolved merchant rule → description / PayNow rule →
  unclassified) with exact currency-qualified amount bands and an asymmetric
  direction check, and a reproducible `categorization_runs` manifest whose id is
  a hash of every input version. Unclassified / ambiguous / direction-conflict /
  migrated / missing-description counts surface as warnings, never hard failures.
- A local Categorization Inbox groups active unclassified records by merchant,
  PayNow recipient, description, or missing-merchant fallback. It exposes
  generation-pinned coverage, exact rule/alias impact previews, explicit
  one-off/selected/reusable scopes, and journal-backed rule history.
- `review escape-invariant` only downgrades a check with a live blocking
  violation of that exact code; `review confirm-link` requires an explicit
  `--link-kind` to record a pair automation never proposed.
- Per-currency exact economic-flow reporting: six inbound buckets (income,
  reimbursements, non-income inflow, other, pending review, self-transfer
  excluded), confirmed internal links excluded, baselines filtered to the
  fully cross-source-covered window, optional explicit `--start`/`--end` bounds
  inside that window, and a consolidated SGD total once reference FX rates are
  loaded (otherwise `null` with a status naming why).
- Receipt-period refunds net positively in a separate bucket; explicit inbound
  classifications and confirmed internal links take precedence. Purchase attribution
  remains deferred.
- Reference FX rates loaded explicitly (`import-fx-rates`), carried forward
  verbatim by `rebuild`, and applied month by month with a single final
  rounding to the cent. Rate versions are immutable; identical reimports write
  nothing and retain selection timestamps. Changes require a new version. Reports
  include exact selected FX inputs and their provenance.
- Typer/Rich CLI output, plus stable `--plain` and machine-readable `--json`
  modes.

PDF parsing, evidence storage, SQLite, reports, and transfer matching all run
locally. Encryption at rest remains deferred. The current evidence vault and
database are plaintext stores protected by owner-only filesystem permissions;
full-disk encryption is recommended.

## Setup

The project is managed with uv and pinned to Python 3.13 (`requires-python =
">=3.13"`):

```sh
uv sync --locked
uv run fincl --help
uv run pytest
```

The committed `uv.lock` provides reproducible dependency versions. `.venv/` is
generated locally and ignored.

### Checks

```sh
uv run ruff check .
uv run mypy
uv run pytest
```

CI (`.github/workflows/ci.yml`) runs the same three on every push and pull
request.

### Database schema

`fincl` refuses to open a database whose recorded `schema_version` differs from
the running build's; normal startup never migrates. The current schema is v23
(9 added `projection_generation` and `command_receipts`; 10–12 the analysis
component, relationship and bulk-action projections; 13 personal debts; 14–17
card statement facts, facilities/limits, installments and manual bill
allocation; 18 the v0.6 planning tables; 19 `balance_observations`; 20
`component_provenance`; 21 the sinking-fund tables; 22 the forecast-snapshot and
match tables; 23 the `reporting_policy` singleton). Each step from 19 on adds
projection tables only, with no data rewrite: they stay empty until the next
journal replay or refresh populates them. `fincl migrate
--database PATH --evidence-dir PATH --backup-output NEW` captures and verifies a
pre-upgrade backup and then applies each registered, checksummed step in one
transaction. Historical reconstruction supports the current schema only; an
older database needs a tested reader or the migration before recovery. Rebuild
preserves the database inventory and FX inputs, re-imports verified evidence,
and replays the journal.

## CLI

**The CLI is deprecated for new features.** New functionality is built in the
graphical app only — a FastAPI route plus a feature view (see "Graphical app"
below). The CLI is feature-frozen, not removed: every command documented here
keeps working and keeps being fixed, but it will not grow to match new UI
capabilities, so the app is the more complete surface for day-to-day use.

Seven commands are not deprecated and remain the supported operations and
recovery surface, because the app cannot perform them: `serve` (the only way to
launch the app), `migrate` (the app refuses to open a ledger at a different
schema), `backup`, `restore`, `rebuild`, `adopt-ledger` and `repair`.

Use a database and evidence directory outside the read-only source folder:

```sh
uv run fincl import-revolut path/to/export.csv \
  --database var/fincl.sqlite3 --evidence-dir var/evidence

uv run fincl import-dbs path/to/dbs-statement.pdf \
  --database var/fincl.sqlite3 --evidence-dir var/evidence

uv run fincl import-paylah path/to/paylah-statement.pdf \
  --database var/fincl.sqlite3 --evidence-dir var/evidence

uv run fincl import-maribank path/to/maribank-savings.pdf \
  --database var/fincl.sqlite3 --evidence-dir var/evidence

uv run fincl import-mari-card path/to/mari-card-statement.pdf \
  --database var/fincl.sqlite3 --evidence-dir var/evidence

uv run fincl import-trust path/to/trust-savings-statement.pdf \
  --database var/fincl.sqlite3 --evidence-dir var/evidence

# Rebuild the read projection and publish a new generation token. Query
# commands below run this first unless given --no-refresh.
uv run fincl refresh --database var/fincl.sqlite3

uv run fincl coverage --database var/fincl.sqlite3
uv run fincl profile --database var/fincl.sqlite3
uv run fincl report --database var/fincl.sqlite3
uv run fincl economic-flow --database var/fincl.sqlite3
# Deliberately report a fully covered analysis period:
uv run fincl economic-flow --database var/fincl.sqlite3 \
  --start 2026-01-01 --end 2026-03-31
uv run fincl link-transfers --database var/fincl.sqlite3

# Load reference FX rates so economic-flow can produce a consolidated SGD total.
# JSON (format in docs/ARCHITECTURE.md §9):
uv run fincl import-fx-rates --file path/to/rates.json \
  --database var/fincl.sqlite3
# ...or MAS's own "Exchange Rates (Weekly)" CSV, reduced to each month's last
# end-of-period value:
uv run fincl import-fx-rates --file "raw/MAS_fx/Exchange Rates.csv" \
  --mas-weekly-csv --source-version mas-weekly-2026-09 \
  --database var/fincl.sqlite3

# Inspect and decide proposed links. Anchors are printed by `review list`.
uv run fincl review list --database var/fincl.sqlite3 --json
uv run fincl review confirm-link --database var/fincl.sqlite3 \
  --left '<anchor>' --right '<anchor>' --reason "same transfer"
uv run fincl review reject-link --database var/fincl.sqlite3 \
  --left '<anchor>' --right '<anchor>' --reason "not an internal transfer"
# A manual pair automation never proposed needs an explicit kind:
uv run fincl review confirm-link --database var/fincl.sqlite3 \
  --left '<anchor>' --right '<anchor>' --link-kind CURRENCY_EXCHANGE \
  --reason "matched by hand from the statements"

# Classify an inbound entry, or downgrade a live invariant violation.
uv run fincl review classify-inbound --database var/fincl.sqlite3 \
  --anchor '<anchor>' --as income --reason "monthly salary"
uv run fincl review escape-invariant --database var/fincl.sqlite3 \
  --code EXCHANGE_PAIRING_INCOMPLETE --anchors '<anchor>' \
  --reason "second leg is in the next export"

# Categorize every entry, author rules, and review what stayed uncategorized.
uv run fincl categorize --database var/fincl.sqlite3
uv run fincl rules add --database var/fincl.sqlite3 \
  --merchant "EXAMPLE GROCER" --category "Food & Dining / Groceries" \
  --reason "recurring grocer"
uv run fincl rules add --database var/fincl.sqlite3 \
  --paynow-to "EXAMPLE LANDLORD" --currency SGD --min-amount 500.00 \
  --category "Housing / Rent" --reason "rent, not the utilities"
uv run fincl rules alias "Corner Mart" --database var/fincl.sqlite3 \
  --pattern exact:CORNER-MART --pattern "prefix:CORNER-MART-"
uv run fincl rules list --database var/fincl.sqlite3 --json
uv run fincl review categorize '<anchor>' --database var/fincl.sqlite3 \
  --as "Shopping / Electronics" --reason "one-off, receipt attached"
uv run fincl review list --kind uncategorized --database var/fincl.sqlite3 --json

# Recreate a separate database from preserved inventory, FX, evidence, and journal. The output must
# not already exist; this never overwrites the active ledger.
uv run fincl rebuild --database var/fincl.sqlite3 --evidence-dir var/evidence \
  --output var/fincl-rebuilt.sqlite3

# Only after inspecting a FAILED run, or a crashed RUNNING run:
uv run fincl repair ext_... --database var/fincl.sqlite3 \
  --reason "retry after interrupted local import"

# One-time for a ledger created before the journal association (F24):
uv run fincl adopt-ledger --database var/fincl.sqlite3

# Capture a verified backup bundle, then restore it into a new directory:
uv run fincl backup --database var/fincl.sqlite3 --evidence-dir var/evidence \
  --output var/backup-2026-09-06
uv run fincl restore --backup var/backup-2026-09-06 --output var/restored

# Upgrade the ledger schema (captures and verifies a pre-upgrade backup first):
uv run fincl migrate --database var/fincl.sqlite3 --evidence-dir var/evidence \
  --backup-output var/premigration-2026-09-06
```

Append `--plain` for stable text or `--json` for structured output where the
command provides those modes. A quarantined import exits with status 2; a
`rebuild` that leaves journaled decisions unbound exits with status 3; an
operational failure exits with status 1.

`repair` first records an `EXTRACTION_REPAIR` decision in the durable journal.
`rebuild` replays that journal into its SQLite projection and reports bound and
dangling decisions. The legacy `recover-extraction` command remains available
for existing scripts.

Each ledger database is bound to its decision journal by a `ledger_association`
row and a `decision-journal.identity.json` file, with a sequence/hash checkpoint
that detects removal of a complete journal tail (F24). A new ledger is bound on
its first import; a ledger created before this must be bound once with
`fincl adopt-ledger`. Opening a ledger whose journal is missing, shorter than
its checkpoint, or from a different ledger is refused with a restore
instruction. `rebuild` accepts only a same-directory output: it produces a
replacement candidate that keeps the source identity and shares the one
journal; relocation uses the verified backup/restore bundle.

Reconstruction inventories every stored evidence blob independent of analytical
selection, groups extraction history by blob and source family, and refuses a
blob that dispatches to more than one family or has no supported extraction
history — without deleting the bytes. It verifies every evidence object before
staging, checks the candidate for evidence-set and exact FX-row equality, and
reopens it through the normal read path before a no-clobber publish. A source
database whose schema version this build has no reader for is refused with the
list of versions it does support.

`fincl backup` captures a `backup-v1` bundle — the database (via SQLite's backup
API), the journal and identity file at the checkpoint, every inventoried
evidence object, and a manifest of checksums, FX versions, and a decision
summary — under a backup writer lock, verified and published with no
overwrite. `fincl restore` rejects symlinks and traversal, verifies every
checksum, reopens the candidate through the normal read path, compares it to
the manifest, and publishes a new ledger directory without switching the active
one. Every mutating command — CLI writes, in-app writes, imports, refresh and
migration — now takes that same ledger writer lock, so a backup serializes
against concurrent imports and edits rather than racing them. Schema upgrades
are explicit: `apply_pending_migrations` requires a verified backup, runs each
step in one transaction with an integrity check, and
stamps the new version only at commit; normal startup never migrates.

## Current limits

MariBank savings and Mari credit-card statements are extracted and reconciled
(`import-maribank`, `import-mari-card`), and a card repayment now links back
through the savings account to its funding DBS PayNow debit. Trust Bank
Singapore savings statements are extracted and reconciled (`import-trust`),
including foreign-currency purchases with their applied rate. Deterministic
categorization is implemented (`fincl categorize`, `fincl rules`). Refund
allocation (v0.2), personal borrowing and lending (v0.3), and credit cards with
installment plan chaining (v0.4) have since landed. Country and trip dimensions,
generalized transfer matching, and encryption at rest are not implemented yet.

Forward-looking cash planning has since landed: evidenced liquid balances and
manual balance observations (`position/`, Cash), expense-flexibility tiers
(`financial_roles.py`, Baseline), minimum-reserve policy and spending controls
(`controls/`, Controls), sinking funds (`funds/`, Annual funds), a six-month
forecast over a dated future-event ledger (`forecast/`, Forecast), and issued
snapshots scored against actuals (`variance/`, Variance). Its acceptance
evidence stays in
[`docs/PLAN-cashflow-framework.md`](docs/PLAN-cashflow-framework.md); remaining
work and its order are in [`docs/PLAN.md`](docs/PLAN.md).

The graphical MVP is implemented and tested
(see "Graphical app" below). MAS weekly-CSV reference rates can be loaded and month-by-month SGD
consolidation is built. The conservative default withholds a total when entries
fall outside the global covered intersection; an explicit `economic-flow`
analysis window wholly inside that intersection produces the consolidated
total. W4 is complete; per-flow coverage remains a future F22 refinement.


## Graphical app

The local shadcn interface presents 25 destinations in six navigation-rail
sections (`frontend/src/lib/tabs.ts` is the single source for the rail and the
command palette): Overview; Money in & out (Transactions, Analysis, Income,
Categorization, Categories, Rules, Review); Commitments (Obligations, Debts,
Cards, Installments); Planning (Planning, Budgets, Recurring, Annual funds,
Controls); Position & forecast (Baseline, Cash, Forecast, Net worth, Variance);
and Setup & data (Accounts, Setup, Maintenance). `Cash Flow` and `Money Owed`
are aliases for Analysis and Obligations — reachable by URL and the command
palette, but not rail rows of their own. It supports per-entry category
overrides and inflow classification, confirm/reject of proposed same-currency
transfers, category creation, monthly budgets, manually tracked savings goals,
verified source downloads, and decision history. The transaction list is paged and
filtered by the backend (`GET /api/v1/transactions`, keyset cursor,
generation-pinned). Every write goes through the M1b application writer (one
ledger lock, generation-checked preview, request-id idempotency); the CLI sees
the same journal decisions.

```bash
uv sync
npm ci --prefix frontend
npm run build --prefix frontend
uv run fincl serve --database var/fincl.sqlite3 --evidence-dir var/evidence --open
```

Serving is currently key-free by default. Add `--require-access-key` to re-enable
the preserved one-use launch key and session gate.

Use an existing current-schema ledger; startup does not migrate or create a
replacement. See [frontend setup and scope](frontend/README.md) for an isolated
synthetic demo, implementation boundaries, and launch/session behavior.

### Supported versions and the MVP release gate

- Python 3.13, the committed `uv.lock`, and `frontend/package-lock.json` are the
  supported toolchain; other versions are unsupported for the release.
- The app opens only a ledger at the current schema (v23). It refreshes an
  existing supported ledger and refuses an absent or older one; upgrade with
  `fincl migrate`, which captures and verifies a pre-upgrade backup first.
- A backup taken after edits made in the app restores to a separate directory
  with its decisions and report values intact — covered by
  `tests/test_mvp_backup_workflow.py`.
- Checks: `uv run pytest`, `uv run ruff check src tests`, `uv run mypy`, and in
  `frontend/`: `npm run build` (TypeScript), `npm test` (Vitest), and
  `npm run test:browser` (Playwright, desktop + 1024px, needs `npm run build`
  and `npx playwright install chromium` first).
