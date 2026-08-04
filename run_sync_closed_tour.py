#!/usr/bin/env python3
"""
run_sync_closed_tours.py — command-line entrypoint for Closed Tour translation sync.

IMPORTANT: unlike Tickets/Transfers/Transports/Hotels, Travel Compositor's
Closed Tour API has no bulk "list all closed tours for a supplier"
endpoint — so --supplier-id and --closed-tour-code are BOTH required.
There is no "sync all" mode here (see sync_closed_tour.py's module
docstring for the full explanation).

FIRST RUN — always start with a dry run on one real closed tour code:

    python run_sync_closed_tours.py --supplier-id 50370 --closed-tour-code TNR-03 --dry-run

Then, only once that looks right, go live:

    python run_sync_closed_tours.py --supplier-id 50370 --closed-tour-code TNR-03

If the code doesn't exist for that supplier, you'll get a clear
"not_found" status instead of a raw API error.
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
from sync_closed_tour import sync_closed_tour

# Reduced from 30 to 19 target languages per your instruction: removed
# Albanian (SQ), Arabic (AR), Azerbaijani (AZ), Georgian (KA), Japanese (JA),
# Croatian (HR), Malay (MS), Serbian (SR), Thai (TH), Uzbek (UZ), and
# Bulgarian (BG) — 11 languages dropped, same list shared across every
# entity type. Persian/Farsi (Iran) was already absent from the 30-language
# list before this change, so it wasn't removed again here.
DEFAULT_TARGET_LANGUAGES = [
    "FR", "SL", "PL", "DE", "SK", "HU", "NL", "ES", "TR",
    "RU", "NO", "SV", "RO", "CS", "EL", "FI",
    "PT", "DA", "IT",
]
TEST_LANGUAGES = ["FR", "DE"]


def get_target_languages(all_languages: bool) -> list:
    if all_languages:
        env_override = os.getenv("TC_TARGET_LANGUAGES")
        if env_override:
            return [l.strip().upper() for l in env_override.split(",") if l.strip()]
        return DEFAULT_TARGET_LANGUAGES
    return TEST_LANGUAGES


def main():
    parser = argparse.ArgumentParser(description="Sync a single Closed Tour (and its options)")
    parser.add_argument("--supplier-id", required=True, help="Numeric supplier ID")
    parser.add_argument("--closed-tour-code", required=True, help="Closed Tour code (e.g. TNR-03)")
    parser.add_argument("--all-languages", action="store_true", help="Use full 30-language list instead of the FR/DE test set")
    parser.add_argument("--dry-run", action="store_true", help="Preview only, no writes")
    parser.add_argument("--force", action="store_true", help="Ignore state tracker and re-translate")
    args = parser.parse_args()

    required_key = required_api_key_env_var()
    if not os.getenv(required_key):
        print(f"❌ {required_key} is not set.")
        sys.exit(1)
    if not os.getenv("TRAVELC_USERNAME") or not os.getenv("TRAVELC_PASSWORD"):
        print("❌ TRAVELC_USERNAME / TRAVELC_PASSWORD are not set.")
        sys.exit(1)

    target_languages = get_target_languages(args.all_languages)
    print(f"🎯 Target languages: {target_languages}")
    if args.dry_run:
        print("🧪 DRY RUN MODE\n")
    if args.force:
        print("🔁 FORCE MODE\n")

    api = TravelCompositorAPI()
    translator = get_translator()
    store = StateStore()

    result = sync_closed_tour(
        api, translator, store, args.supplier_id, args.closed_tour_code,
        target_languages, dry_run=args.dry_run, force=args.force,
    )

    if result.get("status") == "not_found":
        print(f"\n❌ {result.get('reason')}")
        sys.exit(1)

    print("\n=== RESULT ===")
    print(json.dumps(result, indent=2, ensure_ascii=False))


if __name__ == "__main__":
    main()
