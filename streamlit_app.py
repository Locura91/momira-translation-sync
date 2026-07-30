"""
streamlit_app.py — the "Translate now" button, as a small web app.

This is a thin UI layer over code that's already built and tested
(sync_holiday_package.py / travelcompositor_api.py / translator.py /
state_store.py). It does not contain any new sync logic — it just gives a
human a button to click instead of typing a CLI command.

HOW CREDENTIALS WORK HERE (read this before deploying):
This app reads Travel Compositor + Gemini/Anthropic credentials from
Streamlit's built-in Secrets manager (st.secrets), NOT from a .env file,
since a .env file never gets uploaded to GitHub/Streamlit Cloud (and
shouldn't be).
See README.md "Deploying the button (Streamlit)" for exactly where to
paste your credentials — it's a private field in Streamlit Cloud's own
dashboard, never committed to the repo, and never something you need to
share with anyone else (including me) to get this running.
"""

import os
import json

import streamlit as st


def _load_secrets_into_env():
    """
    Bridges Streamlit Cloud's Secrets manager into plain environment
    variables, since TravelCompositorAPI / GeminiTranslator / ClaudeTranslator
    already read their config via os.getenv(...) (so the exact same classes
    work unchanged whether run from the CLI with a .env file, or from here).
    """
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
            # st.secrets raises if no secrets.toml exists at all (e.g. running
            # locally without one) — that's fine, we just fall back to .env.
            pass


_load_secrets_into_env()

from dotenv import load_dotenv
load_dotenv()  # local fallback if you're running this on your own machine

from travelcompositor_api import TravelCompositorAPI
from translator import get_translator, required_api_key_env_var
from state_store import StateStore
from sync_holiday_package import sync_all_holiday_packages, sync_holiday_package

# Same confirmed 30-language list as run_sync_packages.py.
DEFAULT_TARGET_LANGUAGES = [
    "FR", "SL", "PL", "DE", "SK", "AR", "HR", "HU", "AZ", "NL", "ES", "TR",
    "KA", "UZ", "RU", "NO", "SV", "RO", "BG", "CS", "TH", "EL", "FI", "JA",
    "SR", "PT", "DA", "IT", "MS", "SQ",
]
TEST_LANGUAGES = ["FR", "DE"]

st.set_page_config(page_title="Momira Travel — Holiday Package Translator", page_icon="🌐")
st.title("🌐 Momira Travel — Holiday Package Translator")
st.caption("GET the active Holiday Package(s) → translate title/largeTitle/description/themes → PUT back, once per language.")

missing = [
    k for k in (required_api_key_env_var(), "TRAVELC_USERNAME", "TRAVELC_PASSWORD")
    if not os.getenv(k)
]
if missing:
    st.error(
        f"Missing required secret(s): {', '.join(missing)}. "
        "Add them in Streamlit Cloud's Secrets manager (Settings → Secrets) "
        "or in a local .env file, then reload this page."
    )
    st.stop()

with st.sidebar:
    st.header("Settings")
    microsite_id = st.text_input(
        "Microsite ID", value=os.getenv("TRAVELC_MICROSITE_ID", "momiratravel")
    )
    scope = st.radio(
        "Which package(s)?",
        ["All active packages", "One specific package ID"],
    )
    package_id = None
    if scope == "One specific package ID":
        package_id = st.text_input("Holiday Package ID")

    lang_mode = st.radio(
        "Languages",
        ["Test set (FR, DE — cheap, good for a first try)", "All 30 target languages"],
    )
    target_languages = TEST_LANGUAGES if lang_mode.startswith("Test") else DEFAULT_TARGET_LANGUAGES

    dry_run = st.checkbox(
        "Dry run (preview only — no writes to Travel Compositor)", value=True
    )
    limit = None
    if scope == "All active packages":
        limit_input = st.number_input(
            "Limit to first N packages (0 = no limit)", min_value=0, value=5
        )
        limit = limit_input or None

st.write(f"**Target languages this run:** {', '.join(target_languages)}")
if dry_run:
    st.info("🧪 Dry run mode — nothing will be written to Travel Compositor.")
else:
    st.warning("⚠️ Live mode — this WILL write translated content to Travel Compositor.")

if st.button("🚀 Translate now", type="primary"):
    api = TravelCompositorAPI()
    translator = get_translator()
    store = StateStore()

    with st.spinner("Working — this can take a little while for many packages/languages..."):
        if scope == "One specific package ID":
            if not package_id:
                st.error("Enter a Holiday Package ID first.")
                st.stop()
            result = sync_holiday_package(
                api, translator, store, microsite_id, package_id, target_languages, dry_run=dry_run
            )
            results = [result]
        else:
            results = sync_all_holiday_packages(
                api, translator, store, microsite_id, target_languages, dry_run=dry_run, limit=limit
            )

    by_status = {}
    for r in results:
        by_status.setdefault(r.get("status", "unknown"), []).append(r)

    st.subheader("Summary")
    for status, items in by_status.items():
        st.write(f"**{status}**: {len(items)}")

    st.subheader("Full result")
    st.json(results)
