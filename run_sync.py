# nbext — translation-sync prototype (v1: Transfers)

What this is: a working first version of the auto-translation engine, built
on top of your existing `TravelCompositorAPI` client (untouched — only new
methods were appended). It implements one inventory type end to end
(Transfers, the simplest one — no sub-options) so you can validate the whole
GET → translate → merge → PUT loop against real data before we extend it to
Transport, Hotels, Tickets, and Closed Tours.

## Files

- `travelcompositor_api.py` — your original client, unchanged, plus new
  methods for Transfer / Transport / Hotel / Holiday Package endpoints.
- `translator.py` — the AI translation engine. Two providers behind the
  same interface: **Gemini 2.5 Flash (default, cheapest)** and Claude
  Haiku (fallback — switch back any time via one env var). Either way,
  one call per entity translates ALL requested languages at once.
- `state_store.py` — a local SQLite file (`nbext_state.db`) tracking what's
  already translated, so re-running is safe and cheap (only new/changed
  content gets re-translated).
- `sync_engine.py` — the actual fetch → translate → merge logic for
  Transfers, written to auto-detect whatever the real `datasheets` field
  shape turns out to be (see "First run" below).
- `run_sync.py` — the command you actually run.

## Step 1 — Install

```bash
cd nbext_prototype
python3 -m venv venv && source venv/bin/activate   # optional but recommended
pip install -r requirements.txt
```

## Step 2 — Get a Gemini API key (the translation engine)

Gemini 2.5 Flash is the default translation provider — cheapest option for
this batched, many-languages-per-entity job (see the cost comparison we
worked through). It's pay-as-you-go, no subscription.

1. Go to https://aistudio.google.com/apikey and sign in with a Google account.
2. Click **Create API key**. Copy it somewhere safe — you'll paste it into
   `.env` (or later, Streamlit's Secrets manager), never into a chat with me
   or anyone else.
3. For reliable rate limits at real usage volumes, link a billing account
   under **Get API key → set up billing** — Google AI Studio will prompt you
   if you hit free-tier limits.

Want to switch to Claude Haiku instead later? Set `TRANSLATION_PROVIDER=claude`
in `.env` and fill in `ANTHROPIC_API_KEY` (get one at console.anthropic.com →
Settings → API Keys) — no code changes needed, it's a one-line swap.

## Step 3 — Configure

```bash
cp .env.example .env
```

Edit `.env` and fill in:
- `TRAVELC_USERNAME` / `TRAVELC_PASSWORD` — same credentials your working
  project already uses.
- `GEMINI_API_KEY` — the key from Step 2.

Everything else has sensible defaults (including `TRANSLATION_PROVIDER=gemini`).

## Step 4 — First run (dry run, ONE transfer, 2 test languages)

Find a real transfer ID to test with (you can get one from
`api.get_transfers(supplier_id)` in a Python shell, or from your Travel
Compositor back office):

```bash
python run_sync.py --supplier-id YOUR_SUPPLIER_ID --transfer-id YOUR_TRANSFER_ID --dry-run
```

This does NOT write anything. Read the printed output carefully — it shows
you exactly which fields it found inside `datasheets` and what it would
translate them to for French and German. This is also how we finally
confirm the real shape of `datasheets` (the one open item left over from
the design doc) — whatever the script prints here IS the answer, no more
guessing from Swagger.

If the output looks wrong (e.g. it picked up a field that shouldn't be
translated, or missed one), tell me what it printed and I'll adjust
`EXCLUDED_KEYS` / the field-detection logic in `sync_engine.py` — that's a
small, fast fix once we can see real data.

## Step 5 — Full language dry run, still one transfer

```bash
python run_sync.py --supplier-id YOUR_SUPPLIER_ID --transfer-id YOUR_TRANSFER_ID --dry-run --all-languages
```

## Step 6 — Go live, one transfer

Only once Step 5's preview looks right:

```bash
python run_sync.py --supplier-id YOUR_SUPPLIER_ID --transfer-id YOUR_TRANSFER_ID --all-languages
```

Then check that transfer in your Travel Compositor back office by eye.

## Step 7 — Full backfill for one supplier

```bash
python run_sync.py --supplier-id YOUR_SUPPLIER_ID --all-languages
```

Add `--limit 5` first if you want to sanity-check a handful before running
the whole supplier.

## Where to run this (hosting)

You can start entirely on your own machine (Steps 4–7 above need nothing
else). Once you're ready to stop typing CLI commands, here's the concrete
path — GitHub + Streamlit, exactly as you suggested — split into the two
things you actually need:

### Part 1 — GitHub (put the code somewhere Streamlit/Actions can read it)

1. Create a **private** GitHub repository (private matters — this code
   references your Travel Compositor microsite; keep it non-public).
2. Push everything in this folder to it. `.gitignore` is already set up to
   exclude `.env` and `nbext_state.db` — **never commit those**, they're
   either secrets or local state.
3. That's it for this part — no code changes needed, this repo is what
   both Streamlit Cloud and GitHub Actions will read from below.

### Part 2 — Streamlit (the "Translate now" button, Mode A)

`streamlit_app.py` is already built — it's a thin wrapper around the same
`sync_holiday_package.py` functions the CLI uses, with a button, a
dry-run toggle, a language-scope toggle (test set vs. all 30), and a
package-ID field.

1. Go to https://share.streamlit.io, sign in with GitHub, click
   **"New app"**, and point it at your repo + `streamlit_app.py`.
2. In that app's **Settings → Secrets**, paste (in TOML format):
   ```
   TRAVELC_USERNAME = "your_username"
   TRAVELC_PASSWORD = "your_password"
   TRAVELC_MICROSITE_ID = "momiratravel"
   TRANSLATION_PROVIDER = "gemini"
   GEMINI_API_KEY = "your_gemini_key"
   ```
   This is the ONLY place your real credentials need to live. Streamlit's
   Secrets manager is private to your account, never shows up in the repo,
   and I (or anyone reading the code) never see the values — the code
   only ever reads them via `os.getenv(...)`. **Don't paste your actual
   credentials into this chat** — I don't need them and can't use them
   even if you did; everything I've built reads them from your `.env`
   file locally or from Streamlit's Secrets manager, never from me.
3. Deploy. You'll get a URL you (or whoever clicks "Translate now") can
   open any time — leave "Dry run" checked for the first several clicks
   until you trust the output.

### Part 3 — GitHub Actions (the daily autonomous check, Mode B)

`.github/workflows/daily_package_sync.yml` is already built — it runs the
full live sync once a day and commits the updated state file back so
tomorrow's run remembers what's already translated.

1. In your repo: **Settings → Secrets and variables → Actions → New
   repository secret** — add `TRAVELC_USERNAME`, `TRAVELC_PASSWORD`,
   `GEMINI_API_KEY`, and optionally `TRAVELC_MICROSITE_ID` /
   `TRAVELC_BASE_URL`. Same rule as above: these live in GitHub's
   encrypted secrets store, not in any file in the repo.
2. That's it — the workflow is already scheduled for 03:00 UTC daily and
   also has a manual "Run workflow" button under the repo's **Actions**
   tab, so you can trigger a one-off run without waiting for the schedule.
3. Because it only translates packages that are `active: true` and
   new/changed/missing a language (via the state store), a normal daily
   run after the first backfill should be fast and cheap — most days,
   most packages will have nothing to do.

An always-on VPS with a plain cron entry is still a fine alternative to
GitHub Actions if you'd rather not rely on Actions' scheduling — same
`run_sync_packages.py --all-languages` command, just triggered by
system cron instead. GitHub Actions is simply free and needs no server of
your own, which is why it's the default recommendation here.

## Do I need to give Claude (me) your credentials?

No — and please don't paste them into this chat. Every piece of this
prototype reads credentials from either a local `.env` file (never
committed) or a hosting platform's own secrets manager (Streamlit Cloud's
Secrets, or GitHub Actions' encrypted repo secrets). I never need to see
`TRAVELC_PASSWORD`, your `GEMINI_API_KEY`, or an `ANTHROPIC_API_KEY` to
help you build, debug, or extend this — if something goes wrong, paste
me the error message Travel Compositor, Gemini, or Streamlit returns,
not the secret values themselves.

## What's NOT in this v1 (on purpose)

- Transport, Hotels, Tickets, Closed Tours — same pattern, not yet wired
  up. Once Holiday Packages is validated end to end, each one is a short
  `sync_<type>` function following `sync_transfer`'s / `sync_holiday_package`'s
  shape.
- Any UI polish beyond a single button + a JSON results dump — this is a
  working v1, not a finished admin panel.
