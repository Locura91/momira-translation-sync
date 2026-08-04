"""
run_sync.py — command-line entrypoint for the nbext translation-sync prototype.

FIRST RUN — always start like this:

    python run_sync.py --supplier-id 12345 --transfer-id 67890 --dry-run

This fetches ONE real transfer, shows you exactly what it detected as
translatable text and what it would write for 2 test languages, and makes
NO changes to Travel Compositor. Read the printed preview carefully before
ever running without --dry-run.

Once that looks right, try the full target-language list on that one
transfer:

    python run_sync.py --supplier-id 12345 --transfer-id 67890 --dry-run --all-languages

Then, only once you trust the output, remove --dry-run to actually write:

    python run_sync.py --supplier-id 12345 --transfer-id 67890 --all-languages

And finally, run it across every transfer for that supplier:

    python run_sync.py --supplier-id 12345 --all-languages
"""

import os
import sys
import json
import argparse

from dotenv import load_dotenv
load_dotenv()

from travelcompositor_api import TravelCompositorAPI
from translator import get_translator, required_api_key_env_var
from state_store import StateStore
from sync_engine import sync_transfer, sync_all_transfers_for_supplier

# Reduced from 30 to 19 target languages per your instruction: removed
# Albanian (SQ), Arabic (AR), Azerbaijani (AZ), Georgian (KA), Japanese (JA),
# Croatian (HR), Malay (MS), Serbian (SR), Thai (TH), Uzbek (UZ), and
# Bulgarian (BG) — 11 languages dropped, same list shared across every
# entity type. Persian/Farsi (Iran) was already absent from the 30-language
# list before this change (dropped earlier in the original 35-language
# draft, along with PT_BR/ZH/CA/EU), so it wasn't removed again here.
DEFAULT_TARGET_LANGUAGES = [
    "FR", "SL", "PL", "DE", "SK", "HU", "NL", "ES", "TR",
    "RU", "NO", "SV", "RO", "CS", "EL", "FI",
    "PT", "DA", "IT",
]

TEST_LANGUAGES = ["FR", "DE"]  # small, cheap sample for a first --dry-run


def get_target_languages(all_languages: bool) -> list:
    if all_languages:
        env_override = os.getenv("TC_TARGET_LANGUAGES")
        if env_override:
            return [l.strip().upper() for l in env_override.split(",") if l.strip()]
        return DEFAULT_TARGET_LANGUAGES
    return TEST_LANGUAGES


def main():
    parser = argparse.ArgumentParser(description="nbext translation-sync prototype (Transfers only, v1)")
    parser.add_argument("--supplier-id", required=True, help="Travel Compositor supplier ID")
    parser.add_argument("--transfer-id", help="Sync a single transfer by ID (omit to sync ALL transfers for the supplier)")
    parser.add_argument("--all-languages", action="store_true", help="Use the full target-language list instead of the 2-language test set")
    parser.add_argument("--dry-run", action="store_true", help="Preview only — never calls PUT")
    parser.add_argument("--limit", type=int, default=None, help="When syncing all transfers, only process the first N (for testing)")
    args = parser.parse_args()

    required_key = required_api_key_env_var()
    if not os.getenv(required_key):
        print(f"❌ {required_key} is not set. Add it to your .env file or export it, then try again.")
        sys.exit(1)
    if not os.getenv("TRAVELC_USERNAME") or not os.getenv("TRAVELC_PASSWORD"):
        print("❌ TRAVELC_USERNAME / TRAVELC_PASSWORD are not set. Check your .env file.")
        sys.exit(1)

    target_languages = get_target_languages(args.all_languages)
    print(f"🎯 Target languages this run: {target_languages}")
    if args.dry_run:
        print("🧪 DRY RUN MODE — no data will be written to Travel Compositor.\n")

    api = TravelCompositorAPI()
    translator = get_translator()
    store = StateStore()

    if args.transfer_id:
        result = sync_transfer(api, translator, store, args.supplier_id, args.transfer_id, target_languages, dry_run=args.dry_run)
        print("\n=== RESULT ===")
        print(json.dumps(result, indent=2, ensure_ascii=False))
    else:
        results = sync_all_transfers_for_supplier(
            api, translator, store, args.supplier_id, target_languages, dry_run=args.dry_run, limit=args.limit
        )
        print("\n=== SUMMARY ===")
        by_status = {}
        for r in results:
            by_status.setdefault(r["status"], []).append(r)
        for status, items in by_status.items():
            print(f"  {status}: {len(items)}")
        print(json.dumps(results, indent=2, ensure_ascii=False))


if __name__ == "__main__":
    main()
