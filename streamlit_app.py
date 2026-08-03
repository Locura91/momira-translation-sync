"""
streamlit_app.py — with supplier dropdown, progress, support for all services.
"""

import os
import json
import hmac
import streamlit as st


def check_password():
    """Returns `True` if the user had the correct password."""
    def password_entered():
        if hmac.compare_digest(st.session_state["password"], st.secrets["APP_PASSWORD"]):
            st.session_state["password_correct"] = True
            del st.session_state["password"]
        else:
            st.session_state["password_correct"] = False

    if st.session_state.get("password_correct", False):
        return True

    st.text_input(
        "Password", type="password", on_change=password_entered, key="password"
    )
    if "password_correct" in st.session_state:
        st.error("😕 Password incorrect")
    return False


if not check_password():
    st.stop()


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

# ---- Imports for all services ----

# Holiday packages
from sync_holiday_package import sync_holiday_package, sync_all_holiday_packages

# Tickets
from sync_ticket import (
    sync_ticket,
    sync_ticket_from_data,
    sync_all_options_for_ticket_from_data,
    fetch_all_tickets,
)

# Transfers
from sync_transfer import (
    sync_transfer,
    sync_transfer_from_data,
    sync_all_transfers_for_supplier,
    fetch_all_transfers,
)

# Transports
from sync_transport import (
    sync_transport,
    sync_transport_from_data,
    sync_all_transports_for_supplier,
    sync_all_options_for_transport_from_data,
    fetch_all_transports,
)

# Hotels
from sync_hotel import (
    sync_hotel,
    sync_hotel_from_data,
    sync_all_hotels_for_supplier,
    fetch_all_hotels,
)

DEFAULT_TARGET_LANGUAGES = [
    "FR", "SL", "PL", "DE", "SK", "AR", "HR", "HU", "AZ", "NL", "ES", "TR",
    "KA", "UZ", "RU", "NO", "SV", "RO", "BG", "CS", "TH", "EL", "FI", "JA",
    "SR", "PT", "DA", "IT", "MS", "SQ",
]
TEST_LANGUAGES = ["FR", "DE"]

st.set_page_config(page_title="Momira Travel — Translator", page_icon="🌐")
st.title("🌐 Momira Travel — Translation Sync")
st.caption("Translate Holiday Packages, Tickets, Transfers, Transports, or Hotels (live mode).")

missing = [
    k for k in (required_api_key_env_var(), "TRAVELC_USERNAME", "TRAVELC_PASSWORD")
    if not os.getenv(k)
]
if missing:
    st.error(f"Missing required secret(s): {', '.join(missing)}. Add them in Streamlit Cloud Secrets or local .env.")
    st.stop()


@st.cache_data(ttl=300)
def fetch_suppliers():
    try:
        api = TravelCompositorAPI()
        suppliers = api.get_all_suppliers()
        if not suppliers:
            return []
        suppliers_sorted = sorted(suppliers, key=lambda s: s.get('commercialName', '').lower())
        return [(s['id'], s.get('commercialName', f"Supplier {s['id']}")) for s in suppliers_sorted]
    except Exception as e:
        st.warning(f"Could not fetch suppliers: {e}")
        return []


with st.sidebar:
    st.header("Settings")
    entity_type = st.radio(
        "What to translate?",
        ["Holiday Packages", "Tickets", "Transfers", "Transports", "Hotels"]
    )

    # ---- Holiday Packages ----
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

    # ---- Tickets ----
    elif entity_type == "Tickets":
        suppliers = fetch_suppliers()
        if suppliers:
            supplier_options = {name: id for id, name in suppliers}
            selected_name = st.selectbox("Select Supplier", options=list(supplier_options.keys()))
            supplier_id = str(supplier_options[selected_name])
            st.caption(f"Using supplier ID: {supplier_id}")
        else:
            supplier_id = st.text_input("Supplier ID (numeric)", value=os.getenv("TRAVELC_SUPPLIER_ID", ""))
            if not supplier_id:
                st.warning("Please enter a supplier ID.")

        scope = st.radio("Which tickets?", ["All tickets", "One specific ticket code"])
        ticket_code = None
        if scope == "One specific ticket code":
            ticket_code = st.text_input("Ticket Code (e.g., JAP-T1)")
        limit = None
        if scope == "All tickets":
            limit_input = st.number_input("Limit to first N tickets (0 = no limit)", min_value=0, value=5)
            limit = limit_input or None

    # ---- Transfers ----
    elif entity_type == "Transfers":
        suppliers = fetch_suppliers()
        if suppliers:
            supplier_options = {name: id for id, name in suppliers}
            selected_name = st.selectbox("Select Supplier", options=list(supplier_options.keys()))
            supplier_id = str(supplier_options[selected_name])
            st.caption(f"Using supplier ID: {supplier_id}")
        else:
            supplier_id = st.text_input("Supplier ID (numeric)", value=os.getenv("TRAVELC_SUPPLIER_ID", ""))
            if not supplier_id:
                st.warning("Please enter a supplier ID.")

        scope = st.radio("Which transfers?", ["All transfers", "One specific transfer ID"])
        transfer_id = None
        if scope == "One specific transfer ID":
            transfer_id = st.text_input("Transfer ID (e.g., TRANSFER-412566)")
        limit = None
        if scope == "All transfers":
            limit_input = st.number_input("Limit to first N transfers (0 = no limit)", min_value=0, value=5)
            limit = limit_input or None

    # ---- Transports ----
    elif entity_type == "Transports":
        suppliers = fetch_suppliers()
        if suppliers:
            supplier_options = {name: id for id, name in suppliers}
            selected_name = st.selectbox("Select Supplier", options=list(supplier_options.keys()))
            supplier_id = str(supplier_options[selected_name])
            st.caption(f"Using supplier ID: {supplier_id}")
        else:
            supplier_id = st.text_input("Supplier ID (numeric)", value=os.getenv("TRAVELC_SUPPLIER_ID", ""))
            if not supplier_id:
                st.warning("Please enter a supplier ID.")

        scope = st.radio("Which transports?", ["All transports", "One specific transport ID"])
        transport_id = None
        if scope == "One specific transport ID":
            transport_id = st.text_input("Transport ID (e.g., TRANSPORT-412579)")
        limit = None
        if scope == "All transports":
            limit_input = st.number_input("Limit to first N transports (0 = no limit)", min_value=0, value=5)
            limit = limit_input or None

    # ---- Hotels ----
    else:  # Hotels
        suppliers = fetch_suppliers()
        if suppliers:
            supplier_options = {name: id for id, name in suppliers}
            selected_name = st.selectbox("Select Supplier", options=list(supplier_options.keys()))
            supplier_id = str(supplier_options[selected_name])
            st.caption(f"Using supplier ID: {supplier_id}")
        else:
            supplier_id = st.text_input("Supplier ID (numeric)", value=os.getenv("TRAVELC_SUPPLIER_ID", ""))
            if not supplier_id:
                st.warning("Please enter a supplier ID.")

        scope = st.radio("Which hotels?", ["All hotels", "One specific provider code"])
        provider_code = None
        if scope == "One specific provider code":
            provider_code = st.text_input("Provider Code (e.g., CAI-H1)")
        limit = None
        if scope == "All hotels":
            limit_input = st.number_input("Limit to first N hotels (0 = no limit)", min_value=0, value=5)
            limit = limit_input or None

    # ---- Common language settings ----
    lang_mode = st.radio(
        "Languages",
        ["Test set (FR, DE)", "All 30 target languages"],
    )
    target_languages = TEST_LANGUAGES if lang_mode.startswith("Test") else DEFAULT_TARGET_LANGUAGES

    force = st.checkbox("Force re-translate (ignore tracker)", value=False)

# ---- Main area ----
st.write(f"**Target languages:** {', '.join(target_languages)}")
st.warning("⚠️ Live mode – translations will be written to Travel Compositor immediately.")
if force:
    st.warning("🔁 Force re-translate is ON – ignores the tracker.")

log_placeholder = st.empty()

def log_message(msg):
    if 'log_lines' not in st.session_state:
        st.session_state.log_lines = []
    st.session_state.log_lines.append(msg)
    log_placeholder.text("\n".join(st.session_state.log_lines[-200:]))


if st.button("🚀 Translate now", type="primary"):
    if entity_type != "Holiday Packages" and not supplier_id:
        st.error("Supplier ID is required.")
        st.stop()

    st.session_state.log_lines = []
    log_placeholder.empty()

    api = TravelCompositorAPI()
    translator = get_translator()
    store = StateStore()

    with st.spinner("Working..."):
        # ---- Holiday Packages ----
        if entity_type == "Holiday Packages":
            if scope == "One specific package ID":
                if not package_id:
                    st.error("Enter a Holiday Package ID first.")
                    st.stop()
                result = sync_holiday_package(
                    api, translator, store, microsite_id, package_id, target_languages,
                    dry_run=False, force=force
                )
                results = [result]
            else:
                results = sync_all_holiday_packages(
                    api, translator, store, microsite_id, target_languages,
                    dry_run=False, limit=limit, force=force
                )

        # ---- Tickets ----
        elif entity_type == "Tickets":
            if scope == "One specific ticket code":
                if not ticket_code:
                    st.error("Enter a Ticket Code first.")
                    st.stop()
                main_result = sync_ticket(
                    api, translator, store, supplier_id, ticket_code, target_languages,
                    dry_run=False, force=force
                )
                option_results = sync_all_options_for_ticket_from_data(
                    api, translator, store, supplier_id, {"code": ticket_code}, target_languages,
                    dry_run=False, force=force
                ) if isinstance(main_result, dict) and main_result.get("status") != "fetch_failed" else []
                if isinstance(main_result, dict):
                    main_result["options"] = option_results
                results = [main_result] if isinstance(main_result, dict) else [main_result] + option_results
            else:
                log_message(f"📋 Fetching tickets for supplier {supplier_id}...")
                tickets = fetch_all_tickets(api, supplier_id, limit=limit)
                log_message(f"📋 Found {len(tickets)} ticket(s).")
                results = []
                progress_placeholder = st.empty()

                for idx, t in enumerate(tickets):
                    code = t.get("code")
                    if not code:
                        log_message(f"⚠️ Skipping ticket {idx+1}: no code field")
                        results.append({"status": "skipped", "reason": "no code field", "raw": t})
                        continue

                    progress_placeholder.write(f"🔄 Processing ticket {idx+1}/{len(tickets)}: **{code}**")
                    log_message(f"🔄 Processing ticket {idx+1}/{len(tickets)}: {code}")

                    main_result = sync_ticket_from_data(
                        api, translator, store, supplier_id, t, target_languages,
                        dry_run=False, force=force
                    )
                    if main_result.get("status") == "up_to_date":
                        log_message(f"   ✅ Skipped – already translated.")
                    else:
                        log_message(f"   → Syncing main ticket...")
                    results.append(main_result)

                    log_message(f"   → Syncing options...")
                    option_results = sync_all_options_for_ticket_from_data(
                        api, translator, store, supplier_id, t, target_languages,
                        dry_run=False, force=force
                    )
                    if isinstance(main_result, dict):
                        main_result["options"] = option_results
                    else:
                        results.extend(option_results)

                    if option_results:
                        up_to_date = sum(1 for r in option_results if r.get('status') == 'up_to_date')
                        updated = sum(1 for r in option_results if r.get('status') == 'updated')
                        skipped = sum(1 for r in option_results if r.get('status') == 'skipped')
                        log_message(f"      Options: {len(option_results)} total, {up_to_date} up-to-date, {updated} updated, {skipped} skipped")
                        for opt_res in option_results:
                            opt_code = opt_res.get('option_code', '?')
                            status = opt_res.get('status', 'unknown')
                            log_message(f"         - {opt_code}: {status}")

                    log_message(f"   ✅ Finished ticket {code}")
                progress_placeholder.empty()

        # ---- Transfers ----
        elif entity_type == "Transfers":
            if scope == "One specific transfer ID":
                if not transfer_id:
                    st.error("Enter a Transfer ID first.")
                    st.stop()
                result = sync_transfer(
                    api, translator, store, supplier_id, transfer_id, target_languages,
                    dry_run=False, force=force
                )
                results = [result]
            else:
                log_message(f"📋 Fetching transfers for supplier {supplier_id}...")
                transfers = fetch_all_transfers(api, supplier_id, limit=limit)
                log_message(f"📋 Found {len(transfers)} transfer(s).")
                results = []
                progress_placeholder = st.empty()

                for idx, t in enumerate(transfers):
                    transfer_id = t.get("id")
                    if not transfer_id:
                        log_message(f"⚠️ Skipping transfer {idx+1}: no 'id' field")
                        results.append({"status": "skipped", "reason": "no id field", "raw": t})
                        continue

                    progress_placeholder.write(f"🔄 Processing transfer {idx+1}/{len(transfers)}: **{transfer_id}**")
                    log_message(f"🔄 Processing transfer {idx+1}/{len(transfers)}: {transfer_id}")

                    result = sync_transfer_from_data(
                        api, translator, store, supplier_id, t, target_languages,
                        dry_run=False, force=force
                    )
                    if result.get("status") == "up_to_date":
                        log_message(f"   ✅ Skipped – already translated.")
                    else:
                        log_message(f"   → Syncing transfer...")
                    results.append(result)

                    log_message(f"   ✅ Finished transfer {transfer_id}")
                progress_placeholder.empty()

        # ---- Transports ----
        elif entity_type == "Transports":
            if scope == "One specific transport ID":
                if not transport_id:
                    st.error("Enter a Transport ID first.")
                    st.stop()
                result = sync_transport(
                    api, translator, store, supplier_id, transport_id, target_languages,
                    dry_run=False, force=force
                )
                results = [result]
            else:
                log_message(f"📋 Fetching transports for supplier {supplier_id}...")
                transports = fetch_all_transports(api, supplier_id, limit=limit)
                log_message(f"📋 Found {len(transports)} transport(s).")
                results = []
                progress_placeholder = st.empty()

                for idx, t in enumerate(transports):
                    transport_id = t.get("id")
                    if not transport_id:
                        log_message(f"⚠️ Skipping transport {idx+1}: no 'id' field")
                        results.append({"status": "skipped", "reason": "no id field", "raw": t})
                        continue

                    progress_placeholder.write(f"🔄 Processing transport {idx+1}/{len(transports)}: **{transport_id}**")
                    log_message(f"🔄 Processing transport {idx+1}/{len(transports)}: {transport_id}")

                    result = sync_transport_from_data(
                        api, translator, store, supplier_id, t, target_languages,
                        dry_run=False, force=force
                    )
                    if result.get("status") == "up_to_date":
                        log_message(f"   ✅ Skipped – already translated.")
                    else:
                        log_message(f"   → Syncing transport...")
                    results.append(result)

                    # Sync options
                    if t.get("optionCodes"):
                        log_message(f"   → Syncing options for {transport_id}...")
                        option_results = sync_all_options_for_transport_from_data(
                            api, translator, store, supplier_id, t, target_languages,
                            dry_run=False, force=force
                        )
                        if isinstance(result, dict):
                            result["options"] = option_results
                        else:
                            results.extend(option_results)

                        if option_results:
                            up_to_date = sum(1 for r in option_results if r.get('status') == 'up_to_date')
                            updated = sum(1 for r in option_results if r.get('status') == 'updated')
                            skipped = sum(1 for r in option_results if r.get('status') == 'skipped')
                            log_message(f"      Options: {len(option_results)} total, {up_to_date} up-to-date, {updated} updated, {skipped} skipped")
                            for opt_res in option_results:
                                opt_code = opt_res.get('option_code', '?')
                                status = opt_res.get('status', 'unknown')
                                log_message(f"         - {opt_code}: {status}")
                    else:
                        log_message(f"   → No options for {transport_id}")

                    log_message(f"   ✅ Finished transport {transport_id}")
                progress_placeholder.empty()

        # ---- Hotels ----
        else:  # Hotels
            if scope == "One specific provider code":
                if not provider_code:
                    st.error("Enter a Provider Code first.")
                    st.stop()
                log_message(f"📋 Fetching hotel {provider_code} for supplier {supplier_id}...")
                hotel = api.get_hotel(supplier_id, provider_code)
                if isinstance(hotel, dict) and "error" in hotel:
                    results = [{"status": "fetch_failed", "provider_code": provider_code, "detail": hotel}]
                else:
                    result = sync_hotel(api, translator, store, supplier_id, hotel, target_languages,
                                        dry_run=False, force=force)
                    results = [result]
            else:
                log_message(f"📋 Fetching hotels for supplier {supplier_id}...")
                hotels = fetch_all_hotels(api, supplier_id, limit=limit)
                log_message(f"📋 Found {len(hotels)} hotel(s).")
                results = []
                progress_placeholder = st.empty()

                for idx, h in enumerate(hotels):
                    provider_code = h.get("providerCode")
                    if not provider_code:
                        log_message(f"⚠️ Skipping hotel {idx+1}: no providerCode")
                        results.append({"status": "skipped", "reason": "no providerCode", "raw": h})
                        continue

                    progress_placeholder.write(f"🔄 Processing hotel {idx+1}/{len(hotels)}: **{provider_code}**")
                    log_message(f"🔄 Processing hotel {idx+1}/{len(hotels)}: {provider_code}")

                    # Get full hotel details
                    full_hotel = api.get_hotel(supplier_id, provider_code)
                    if isinstance(full_hotel, dict) and "error" in full_hotel:
                        log_message(f"   ❌ Failed to fetch full details for {provider_code}")
                        results.append({"status": "fetch_failed", "provider_code": provider_code, "detail": full_hotel})
                        continue

                    hotel_result = sync_hotel(api, translator, store, supplier_id, full_hotel,
                                              target_languages, dry_run=False, force=force)

                    # Log main status
                    main_status = hotel_result.get("main", {}).get("status", "unknown")
                    rooms_updated = sum(1 for r in hotel_result.get("rooms", []) if r.get("status") == "updated")
                    supps_updated = sum(1 for s in hotel_result.get("supplements", []) if s.get("status") == "updated")
                    log_message(f"   Main: {main_status}, Rooms updated: {rooms_updated}, Supplements updated: {supps_updated}")
                    results.append(hotel_result)
                    log_message(f"   ✅ Finished hotel {provider_code}")
                progress_placeholder.empty()

    # ---- Summary ----
    by_status = {}
    def count(r):
        if isinstance(r, dict):
            # If it's a hotel result, it has 'main', 'rooms', 'supplements'
            if "main" in r:
                main = r["main"]
                if isinstance(main, dict):
                    status = main.get("status", "unknown")
                    by_status.setdefault(status, []).append(main)
                for room in r.get("rooms", []):
                    count(room)
                for supp in r.get("supplements", []):
                    count(supp)
            else:
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
