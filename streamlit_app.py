"""
streamlit_app.py — the "Translate now" button, now for Holiday Packages AND Tickets.
"""

import os
import json

import streamlit as st


def _load_secrets_into_env():
    keys = [
        "TRAVELC_BASE_URL", "TRAVELC_MICROSITE_ID", "TRAVELC_USERNAME",
        "TRAVELC_PASSWORD", "TRANSLATION_PROVIDER",
        "GEMINI_API_KEY", "GEMINI_MODEL",
        "ANTHROPIC_API_KEY", "ANTHROPIC_MODEL",
        "TC_TARGET_LANGUAGES",
    ]
    for key in keys:
        try:
            if key in st.secrets:
                os.environ[key] = str(st.secrets[key])
        except Exception:
            pass


_load_secrets_into_env()

from dotenv import load_dotenv
load_dotenv()

from travelcompositor_api import TravelCompositorAPI
from translator import get_translator, required_api_key_env_var
from state_store import StateStore

# Holiday package imports
from sync_holiday_package import sync_holiday_package, sync_all_holiday_packages

# Ticket imports
from sync_ticket import sync_ticket, sync_all_options_for_ticket, fetch_all_tickets

DEFAULT_TARGET_LANGUAGES = [
    "FR", "SL", "PL", "DE", "SK", "AR", "HR", "HU", "AZ", "NL", "ES", "TR",
    "KA", "UZ", "RU", "NO", "SV", "RO", "BG", "CS", "TH", "EL", "FI", "JA",
    "SR", "PT", "DA", "IT", "MS", "SQ",
]
TEST_LANGUAGES = ["FR", "DE"]

st.set_page_config(page_title="Momira Travel — Translator", page_icon="🌐")
st.title("🌐 Momira Travel — Translation Sync")
st.caption("Translate Holiday Packages or Tickets + their options.")

missing = [
    k for k in (required_api_key_env_var(), "TRAVELC_USERNAME", "TRAVELC_PASSWORD")
    if not os.getenv(k)
]
if missing:
    st.error(f"Missing required secret(s): {', '.join(missing)}. Add them in Streamlit Cloud Secrets or local .env.")
    st.stop()

with st.sidebar:
    st.header("Settings")
    entity_type = st.radio("What to translate?", ["Holiday Packages", "Tickets"])

    if entity_type == "Holiday Packages":
        microsite_id = st.text_input("Microsite ID", value=os.getenv("TRAVELC_MICROSITE_ID", "momiratravel"))
        scope = st.radio("Which packages?", ["All active packages", "One specific package ID"])
        package_id = None
        if scope == "One specific package ID":
            package_id = st.text_input("Holiday Package ID")
        limit = None
        if scope == "All active packages":
            limit_input = st.number_input("Limit to first N packages (0 = no limit)", min_value=0, value=5)
            limit = limit_input or None

    else:  # Tickets
        supplier_id = st.text_input("Supplier ID (numeric)", value=os.getenv("TRAVELC_SUPPLIER_ID", ""))
        if not supplier_id:
            st.warning("Please set TRAVELC_SUPPLIER_ID in secrets or enter it here.")
        scope = st.radio("Which tickets?", ["All tickets", "One specific ticket code"])
        ticket_code = None
        if scope == "One specific ticket code":
            ticket_code = st.text_input("Ticket Code (e.g., JAP-T1)")
        limit = None
        if scope == "All tickets":
            limit_input = st.number_input("Limit to first N tickets (0 = no limit)", min_value=0, value=5)
            limit = limit_input or None

    lang_mode = st.radio(
        "Languages",
        ["Test set (FR, DE)", "All 30 target languages"],
    )
    target_languages = TEST_LANGUAGES if lang_mode.startswith("Test") else DEFAULT_TARGET_LANGUAGES

    dry_run = st.checkbox("Dry run (preview only)", value=True)
    force = st.checkbox("Force re-translate (ignore tracker)", value=False)

st.write(f"**Target languages:** {', '.join(target_languages)}")
if dry_run:
    st.info("🧪 Dry run mode — nothing will be written to Travel Compositor.")
else:
    st.warning("⚠️ Live mode — this WILL write translated content.")
if force:
    st.warning("🔁 Force re-translate is ON — ignores the tracker.")

if st.button("🚀 Translate now", type="primary"):
    api = TravelCompositorAPI()
    translator = get_translator()
    store = StateStore()

    with st.spinner("Working..."):
        if entity_type == "Holiday Packages":
            if scope == "One specific package ID":
                if not package_id:
                    st.error("Enter a Holiday Package ID first.")
                    st.stop()
                result = sync_holiday_package(
                    api, translator, store, microsite_id, package_id, target_languages,
                    dry_run=dry_run, force=force
                )
                results = [result]
            else:
                results = sync_all_holiday_packages(
                    api, translator, store, microsite_id, target_languages,
                    dry_run=dry_run, limit=limit, force=force
                )

        else:  # Tickets
            if not supplier_id:
                st.error("Supplier ID is required.")
                st.stop()
            if scope == "One specific ticket code":
                if not ticket_code:
                    st.error("Enter a Ticket Code first.")
                    st.stop()
                # Sync main ticket
                main_result = sync_ticket(
                    api, translator, store, supplier_id, ticket_code, target_languages,
                    dry_run=dry_run, force=force
                )
                # Sync options
                option_results = sync_all_options_for_ticket(
                    api, translator, store, supplier_id, ticket_code, target_languages,
                    dry_run=dry_run, force=force
                )
                if isinstance(main_result, dict):
                    main_result["options"] = option_results
                results = [main_result] if isinstance(main_result, dict) else [main_result] + option_results
            else:
                # All tickets: we need to fetch tickets and process each
                tickets = fetch_all_tickets(api, supplier_id, limit=limit)
                results = []
                for t in tickets:
                    code = t.get("code")
                    if not code:
                        continue
                    main_result = sync_ticket(
                        api, translator, store, supplier_id, code, target_languages,
                        dry_run=dry_run, force=force
                    )
                    results.append(main_result)
                    if main_result.get("status") not in ("fetch_failed", "skipped"):
                        option_results = sync_all_options_for_ticket(
                            api, translator, store, supplier_id, code, target_languages,
                            dry_run=dry_run, force=force
                        )
                        if isinstance(main_result, dict):
                            main_result["options"] = option_results
                        else:
                            results.extend(option_results)

    # Show summary
    by_status = {}
    def count(r):
        if isinstance(r, dict):
            status = r.get("status", "unknown")
            by_status.setdefault(status, []).append(r)
            if "options" in r and isinstance(r["options"], list):
                for opt in r["options"]:
                    count(opt)
        else:
            by_status.setdefault("unknown", []).append(r)
    for r in results:
        count(r)

    st.subheader("Summary")
    for status, items in by_status.items():
        st.write(f"**{status}**: {len(items)}")
    st.subheader("Full result")
    st.json(results)
