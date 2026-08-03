import streamlit as st
import hmac

def check_password():
    """Returns `True` if the user had the correct password."""

    def password_entered():
        """Checks whether a password entered by the user is correct."""
        if hmac.compare_digest(st.session_state["password"], st.secrets["APP_PASSWORD"]):
            st.session_state["password_correct"] = True
            del st.session_state["password"]  # Don't store the password.
        else:
            st.session_state["password_correct"] = False

    # Return True if the password is validated.
    if st.session_state.get("password_correct", False):
        return True

    # Show input for password.
    st.text_input(
        "Password", type="password", on_change=password_entered, key="password"
    )
    if "password_correct" in st.session_state:
        st.error("😕 Password incorrect")
    return False

if not check_password():
    st.stop()  # Do not continue if check_password is False.

# --- Rest of your app's main code here ---


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
from sync_ticket import (
    sync_ticket,
    sync_ticket_from_data,
    sync_all_options_for_ticket_from_data,
    fetch_all_tickets,
)

# Transfer imports
from sync_transfer import (
    sync_transfer,
    sync_transfer_from_data,
    sync_all_transfers_for_supplier,
    fetch_all_transfers,
)

DEFAULT_TARGET_LANGUAGES = [
    "FR", "SL", "PL", "DE", "SK", "AR", "HR", "HU", "AZ", "NL", "ES", "TR",
    "KA", "UZ", "RU", "NO", "SV", "RO", "BG", "CS", "TH", "EL", "FI", "JA",
    "SR", "PT", "DA", "IT", "MS", "SQ",
]
TEST_LANGUAGES = ["FR", "DE"]

st.set_page_config(page_title="Momira Travel — Translator", page_icon="🌐")
st.title("🌐 Momira Travel — Translation Sync")
st.caption("Translate Holiday Packages, Tickets, or Transfers (live mode).")

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
    entity_type = st.radio("What to translate?", ["Holiday Packages", "Tickets", "Transfers"])

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

    else:  # Transfers
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

    lang_mode = st.radio(
        "Languages",
        ["Test set (FR, DE)", "All 30 target languages"],
    )
    target_languages = TEST_LANGUAGES if lang_mode.startswith("Test") else DEFAULT_TARGET_LANGUAGES

    force = st.checkbox("Force re-translate (ignore tracker)", value=False)

st.write(f"**Target languages:** {', '.join(target_languages)}")
st.warning("⚠️ Live mode – translations will be written to Travel Compositor immediately.")
if force:
    st.warning("🔁 Force re-translate is ON – ignores the tracker.")

# Log area placeholder
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

    # Clear logs
    st.session_state.log_lines = []
    log_placeholder.empty()

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
                    dry_run=False, force=force
                )
                results = [result]
            else:
                results = sync_all_holiday_packages(
                    api, translator, store, microsite_id, target_languages,
                    dry_run=False, limit=limit, force=force
                )

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
                # All tickets
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

        else:  # Transfers
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
                # All transfers
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
