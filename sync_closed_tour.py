"""
sync_closed_tour.py — Sync Closed Tours (main datasheet + options).

CONFIRMED SCOPE (from real Swagger + real live GET/example data for
supplier 50370, closed tour TNR-03, "Madagascar's People and Lemurs SIC"):

  MAIN ENTITY translatable content lives in `datasheets{<lang>: {...}}`,
  the same per-language-keyed-map pattern as Tickets. Confirmed real,
  populated fields for this tour: name, description, included, excluded,
  hotels. Also present in the documented schema but NOT populated on this
  particular tour (so untested against real data, but supported the same
  "only if present and non-empty" way as everything else in this project):
  voucherRemarks, meetingPoint, remarksTitle, remarksDescription.

  Unlike Tickets, `included`/`excluded` here are plain HTML STRINGS (e.g.
  "<ul><li>Airport transfers...</li></ul>"), not string arrays — so no
  join/split-to-list conversion is needed; they translate exactly like
  `description` does.

  `hotels` (inside datasheets, per language) is also a plain HTML string —
  a human-written "planned hotels for this tour" blurb with a bullet list.
  Confirmed real and worth translating (present in EN, missing in DE on
  this tour — i.e. genuinely not-yet-translated, exactly the case this
  tool exists to fix).

  NOT translated (deliberately out of scope for now, confirmed empty on
  the one real example available — see module-level NOTE below):
    - itinerary[].description{<lang>: string} — every stop's description
      map was `{}` (completely empty) on the one real tour we checked, so
      there is zero real content to validate the key format or confirm
      this is even actively used. Passed through untouched.
    - itinerary[].destination — a mix of real Travel Compositor
      destination codes (e.g. "TNR", "RMFN") and plain city names (e.g.
      "Ambositra") in the same list; not per-language, so left untouched
      regardless (same reasoning as Holiday Package `themes`).
    - supplements[] (the main entity's OWN embedded supplements array,
      each with `translations{<lang>: ContractClosedTourSupplementTranslationVO}`)
      — was `[]` (completely empty) on the one real tour checked. Its
      expanded schema was never obtained either. Passed through untouched.
      If you find a closed tour with real supplement data, that's the
      thing to paste next to extend coverage here.

  OPTIONS (fetched separately via GET/PUT .../{closedTourCode}/{optionCode},
  one call per modalityCode listed on the main entity) use
  `translations{<lang>: {name, remarks}}` — confirmed via real Swagger AND
  a real live example (option "Code1" of TNR-03). `remarks` was empty on
  that example; only `name` was populated.

  IMPORTANT OBSERVED DATA QUALITY CAVEAT (not something this code can or
  should try to detect/fix): on that same real option, EN's name was
  literally "Code3" — an apparent placeholder/internal label, not real
  English content — while DE's name was the genuine, well-written tour
  name. Since this tool always treats EN as the authoritative source to
  translate FROM, an option like this will just propagate "Code3" (or
  whatever nonsense is in EN) into every target language. That is a
  Travel-Compositor-side data quality issue on this particular option, not
  a bug here — if you spot this pattern being common rather than a one-off,
  let's revisit whether option names need a different strategy.

  DISCOVERY / "sync all" LIMITATION: Travel Compositor's Closed Tour API
  has NO bulk "list all closed tours for a supplier" endpoint (unlike
  Tickets/Transfers/Transports/Hotels, which all have GET /<type>/{supplierId}
  returning a list). So there is no fetch_all_closed_tours()/
  sync_all_closed_tours_for_supplier() here — only single-code entry
  points. Per your instruction: a human enters the Closed Tour Code:
  if it exists, translation proceeds; if not, a clear "not_found" status
  is returned (see sync_closed_tour() below) instead of a raw API error.
"""

import re
import time
from typing import Dict, Any, List, Optional
from concurrent.futures import ThreadPoolExecutor, as_completed

from state_store import StateStore, compute_hash
from translator import translate_in_batches

ENTITY_TYPE = "closed_tour"
OPTION_ENTITY_TYPE = "closed_tour_option"

# ---- Configuration ----
# Deliberately small: Closed Tour descriptions run MUCH longer than
# Tickets/Holiday Packages (a full multi-day itinerary write-up, easily
# thousands of words) — a big batch of languages risks truncating mid-JSON
# even with the larger output-token caps now in place (Gemini
# max_output_tokens=32768, Claude max_tokens=64000 — see translator.py).
# The first live test on the Madagascar tour (a 14-day, several-thousand-
# word description) burned real time/cost on repeated truncated-response
# retries with the OLD (too-small) Claude max_tokens=8192 default — now
# fixed.
#
# CONFIRMED via real Streamlit Cloud logs (BKK-3, supplier 49844, full
# 30-language run): with batch size 2 AND HTML formatting no longer
# stripped before translation (see strip_html_and_compress's docstring),
# Claude was hitting the 120s request timeout — "Request timed out or
# interrupted" — on multiple batches, 6 attempts each, before giving up
# and falling back to English for those languages. This isn't a transient
# network blip: sending 2 languages' worth of full un-stripped HTML
# across up to 9 fields (name/description/included/excluded/hotels/
# voucherRemarks/meetingPoint/remarksTitle/remarksDescription) in one
# call is apparently too much for Claude to generate inside the timeout,
# consistently — retrying the identical request 6 times just wastes
# minutes repeating the same failure. Reduced to 1 language per batch to
# roughly halve what has to be generated per call. Batches still run
# CONCURRENTLY (see translate_in_batches in translator.py), so more/
# smaller batches cost nothing in wall-clock time — if anything this
# should be faster overall now that batches actually complete instead of
# timing out and retrying.
DATASHEET_BATCH_SIZE = 1
OPTION_BATCH_SIZE = 10
MAX_OPTION_WORKERS = 5  # parallel option fetches, same pattern as Tickets/Transports

# ---- Main closed tour datasheet fields (confirmed + documented-but-untested) ----
TEXT_FIELDS = (
    "name", "description", "included", "excluded", "hotels",
    "voucherRemarks", "meetingPoint", "remarksTitle", "remarksDescription",
)

# ---- Option fields (confirmed via real example) ----
OPTION_TEXT_FIELDS = ("name", "remarks")


def strip_html_and_compress(text: str) -> str:
    """
    NO-OP passthrough now. This used to strip every HTML tag out of a
    field before sending it to the translator — which is exactly why the
    live ASW-3 test came back with description/included/excluded/hotels
    flattened to plain <p> text instead of keeping the source's <ul><li>,
    <b>, etc. structure. translator.py's SYSTEM_PROMPT already explicitly
    instructs the model to "preserve HTML tags ... EXACTLY as they
    appear, untouched, in the same position" — but that instruction was
    meaningless here because the tags were being stripped out before the
    model ever saw them. Fix: stop stripping; let the model see (and
    preserve) the real HTML.
    """
    return text


def compress_translatable_fields(fields: Dict[str, str]) -> Dict[str, str]:
    compressed = {}
    for key, value in fields.items():
        if isinstance(value, str):
            compressed[key] = strip_html_and_compress(value)
        else:
            compressed[key] = value
    return compressed


# =========================================================================
# MAIN CLOSED TOUR (datasheets)
# =========================================================================

def extract_translatable_fields_from_closed_tour(entry: Dict[str, Any]) -> Dict[str, str]:
    datasheets = entry.get("datasheets")
    if not datasheets:
        return {}
    en_entry = None
    for key in ("EN", "EN_US"):
        if key in datasheets:
            en_entry = datasheets[key]
            break
    if en_entry is None:
        for key in datasheets:
            if key.upper().startswith("EN"):
                en_entry = datasheets[key]
                break
    if not en_entry:
        return {}

    fields = {}
    for f in TEXT_FIELDS:
        val = en_entry.get(f)
        if isinstance(val, str) and val.strip():
            fields[f] = val
    return fields


def get_existing_content_for_language(entry: Dict[str, Any], lang: str) -> Dict[str, str]:
    datasheets = entry.get("datasheets", {})
    lang_entry = datasheets.get(lang, {})
    if not lang_entry:
        return {}
    fields = {}
    for f in TEXT_FIELDS:
        val = lang_entry.get(f)
        if isinstance(val, str) and val.strip():
            fields[f] = val
    return fields


def build_updated_datasheets(
    original_datasheets: Dict[str, Any],
    translations_by_lang: Dict[str, Dict[str, str]],
    en_entry: Dict[str, Any],
) -> Dict[str, Any]:
    new_datasheets = dict(original_datasheets)
    for lang, trans in translations_by_lang.items():
        base = dict(en_entry)
        for f, text in trans.items():
            base[f] = text
        if lang in original_datasheets:
            for k, v in original_datasheets[lang].items():
                if k not in base:
                    base[k] = v
        new_datasheets[lang] = base
    return new_datasheets


def verify_and_filter_needed(
    store: StateStore,
    entity_type: str,
    supplier_id: str,
    entity_id: str,
    source_hash: str,
    target_languages: List[str],
    current_entry: Dict[str, Any],
    source_fields: Dict[str, str],
    option_code: str = "",
) -> List[str]:
    state = store.get_state(entity_type, supplier_id, entity_id, option_code)
    if state is None or state["source_hash"] != source_hash:
        needed = list(target_languages)
    else:
        already_done = set(state["translated_languages"])
        needed = [lang for lang in target_languages if lang not in already_done]

    truly_needed = []
    languages_to_add_to_state = []

    for lang in needed:
        existing = get_existing_content_for_language(current_entry, lang) if not option_code \
            else get_existing_option_content_for_language(current_entry, lang)
        if not existing:
            truly_needed.append(lang)
            continue
        is_identical = all(existing.get(f) == src for f, src in source_fields.items())
        if is_identical:
            truly_needed.append(lang)
        else:
            languages_to_add_to_state.append(lang)

    if languages_to_add_to_state:
        prior_state = store.get_state(entity_type, supplier_id, entity_id, option_code)
        prior_langs = prior_state["translated_languages"] if prior_state and prior_state["source_hash"] == source_hash else []
        all_langs = sorted(set(prior_langs) | set(languages_to_add_to_state))
        store.upsert_state(entity_type, supplier_id, entity_id, source_hash, all_langs, option_code=option_code)

    return truly_needed


def sync_closed_tour_from_data(
    api,
    translator,
    store: StateStore,
    supplier_id: str,
    closed_tour_entry: Dict[str, Any],
    target_languages: List[str],
    closed_tour_code: str,
    dry_run: bool = True,
    force: bool = False,
) -> Dict[str, Any]:
    """
    IMPORTANT: `closed_tour_code` must be the code the caller used to fetch
    this entry via api.get_closed_tour(supplier_id, closed_tour_code) — NOT
    re-derived from closed_tour_entry.get("code"). Confirmed via a real
    live test (supplier 50951, queried code "ASW-3"): the entry's own
    "code" field came back as "CLOSEDTOUR-414490", a DIFFERENT value than
    the code used to look it up. The option endpoints
    (GET/PUT /closedtour/{supplierId}/{closedTourCode}/{optionCode}) need
    the code that Travel Compositor actually recognizes in that URL
    position, which is the queried code — using entry.get("code") there
    caused every option fetch to 404 with "Closed tour not found" (the
    closedTourCode segment itself was being rejected, not the option).
    """
    start_time = time.time()
    if not closed_tour_code:
        return {"status": "skipped", "reason": "no closed_tour_code provided"}

    # Same rule as every other entity type in this project: only active
    # tours get translated. If Closed Tours should NOT follow this rule,
    # tell me and I'll remove this check.
    if closed_tour_entry.get("active") is not True:
        return {"status": "skipped", "closed_tour_code": closed_tour_code, "reason": "active is not true"}

    datasheets = closed_tour_entry.get("datasheets")
    if not datasheets:
        return {"status": "skipped", "closed_tour_code": closed_tour_code, "reason": "no datasheets"}

    translatable = extract_translatable_fields_from_closed_tour(closed_tour_entry)
    if not translatable:
        return {"status": "skipped", "closed_tour_code": closed_tour_code, "reason": "no translatable fields found"}

    source_hash = compute_hash(translatable)
    if force:
        needed = list(target_languages)
    else:
        needed = verify_and_filter_needed(
            store, ENTITY_TYPE, supplier_id, closed_tour_code, source_hash,
            target_languages, closed_tour_entry, translatable
        )

    if not needed:
        return {"status": "up_to_date", "closed_tour_code": closed_tour_code}

    print(f"🌐 Translating closed tour {closed_tour_code} ('{translatable.get('name', '')}'): "
          f"fields={list(translatable.keys())} -> {needed}")

    compressed_translatable = compress_translatable_fields(translatable)
    translation_start = time.time()
    combined_translations, failed_languages = translate_in_batches(
        translator, compressed_translatable, needed, batch_size=DATASHEET_BATCH_SIZE
    )
    translation_time = time.time() - translation_start

    # NOTE: no longer treats "translation identical to source" as a failure
    # signal — short/common words can legitimately translate to themselves
    # in several languages. Only languages translate_in_batches itself
    # reports as failed (see its docstring in translator.py) get dropped.
    successful = {}
    for lang, trans in combined_translations.items():
        if lang in failed_languages:
            print(f"⚠️  Translation batch for {lang} failed; skipping.")
        else:
            successful[lang] = trans

    if not successful:
        return {"status": "skipped", "closed_tour_code": closed_tour_code, "reason": "no successful translations"}

    en_entry = datasheets.get("EN") or datasheets.get("EN_US") or {}
    new_datasheets = build_updated_datasheets(datasheets, successful, en_entry)

    if dry_run:
        preview = {lang: {k: v for k, v in trans.items() if k in TEXT_FIELDS}
                   for lang, trans in successful.items()}
        return {"status": "dry_run_preview", "closed_tour_code": closed_tour_code,
                "languages": list(successful.keys()), "preview": preview}

    write_start = time.time()
    # Full copy of the original entry, only datasheets replaced — itinerary,
    # supplements, images, pricing, modalityCodes, etc. all pass through
    # untouched. Safe here because PUT's documented request schema
    # (ContractClosedTourVO) is confirmed IDENTICAL to the GET response
    # schema (unlike Holiday Packages, where GET/PUT schemas diverged).
    payload = dict(closed_tour_entry)
    payload["datasheets"] = new_datasheets
    result = api.update_closed_tour(supplier_id, payload)
    write_time = time.time() - write_start
    if isinstance(result, dict) and "error" in result:
        return {"status": "put_failed", "closed_tour_code": closed_tour_code, "detail": result}

    written_langs = list(successful.keys())
    prior_state = store.get_state(ENTITY_TYPE, supplier_id, closed_tour_code)
    prior_langs = prior_state["translated_languages"] if prior_state and prior_state["source_hash"] == source_hash else []
    all_langs = sorted(set(prior_langs) | set(written_langs))
    store.upsert_state(ENTITY_TYPE, supplier_id, closed_tour_code, source_hash, all_langs)

    total_time = time.time() - start_time
    print(f"✅ Closed tour {closed_tour_code} done in {total_time:.1f}s "
          f"(translate: {translation_time:.1f}s, write: {write_time:.1f}s)")
    return {"status": "updated", "closed_tour_code": closed_tour_code, "languages_written": written_langs}


# =========================================================================
# OPTIONS
# =========================================================================

def extract_translatable_fields_from_option(option_entry: Dict[str, Any]) -> Dict[str, str]:
    fields = {}
    translations = option_entry.get("translations", {})
    en_entry = translations.get("EN") or translations.get("EN_US") or {}
    if isinstance(en_entry, dict):
        for f in OPTION_TEXT_FIELDS:
            val = en_entry.get(f)
            if isinstance(val, str) and val.strip():
                fields[f] = val
    return fields


def get_existing_option_content_for_language(option_entry: Dict[str, Any], lang: str) -> Dict[str, str]:
    translations = option_entry.get("translations", {})
    lang_entry = translations.get(lang, {})
    if not isinstance(lang_entry, dict):
        return {}
    fields = {}
    for f in OPTION_TEXT_FIELDS:
        val = lang_entry.get(f)
        if isinstance(val, str) and val.strip():
            fields[f] = val
    return fields


def build_updated_option(
    original_option: Dict[str, Any],
    translations_by_lang: Dict[str, Dict[str, str]],
) -> Dict[str, Any]:
    new_option = dict(original_option)
    translations = dict(original_option.get("translations", {}))
    for lang, trans in translations_by_lang.items():
        lang_trans = dict(translations.get(lang, {})) if isinstance(translations.get(lang), dict) else {}
        for f in OPTION_TEXT_FIELDS:
            if f in trans:
                lang_trans[f] = trans[f]
        translations[lang] = lang_trans
    new_option["translations"] = translations
    return new_option


def sync_closed_tour_option_from_data(
    api,
    translator,
    store: StateStore,
    supplier_id: str,
    option_entry: Dict[str, Any],
    closed_tour_code: str,
    option_code: str,
    target_languages: List[str],
    dry_run: bool = True,
    force: bool = False,
) -> Dict[str, Any]:
    start_time = time.time()
    translatable = extract_translatable_fields_from_option(option_entry)
    if not translatable:
        return {"status": "skipped", "option_code": option_code, "reason": "no translatable fields"}

    source_hash = compute_hash(translatable)
    entity_id = f"{closed_tour_code}|{option_code}"
    if force:
        needed = list(target_languages)
    else:
        needed = verify_and_filter_needed(
            store, OPTION_ENTITY_TYPE, supplier_id, entity_id, source_hash,
            target_languages, option_entry, translatable, option_code=option_code
        )

    if not needed:
        return {"status": "up_to_date", "option_code": option_code}

    compressed_translatable = compress_translatable_fields(translatable)
    combined_translations, failed_languages = translate_in_batches(
        translator, compressed_translatable, needed, batch_size=OPTION_BATCH_SIZE
    )

    # See the main-entity translate call above: identical-to-source is not a
    # failure signal on its own — only translate_in_batches-reported
    # failures get dropped.
    successful = {}
    for lang, trans in combined_translations.items():
        if lang in failed_languages:
            print(f"⚠️  Translation batch for {lang} failed; skipping.")
        else:
            successful[lang] = trans

    if not successful:
        return {"status": "skipped", "option_code": option_code, "reason": "no successful translations"}

    updated_option = build_updated_option(option_entry, successful)

    if dry_run:
        preview = {lang: {k: v for k, v in trans.items() if k in OPTION_TEXT_FIELDS}
                   for lang, trans in successful.items()}
        return {"status": "dry_run_preview", "option_code": option_code,
                "languages": list(successful.keys()), "preview": preview}

    result = api.update_closed_tour_option(supplier_id, closed_tour_code, updated_option)
    if isinstance(result, dict) and "error" in result:
        return {"status": "put_failed", "option_code": option_code, "detail": result}

    written_langs = list(successful.keys())
    prior_state = store.get_state(OPTION_ENTITY_TYPE, supplier_id, entity_id, option_code=option_code)
    prior_langs = prior_state["translated_languages"] if prior_state and prior_state["source_hash"] == source_hash else []
    all_langs = sorted(set(prior_langs) | set(written_langs))
    store.upsert_state(OPTION_ENTITY_TYPE, supplier_id, entity_id, source_hash, all_langs, option_code=option_code)

    elapsed = time.time() - start_time
    print(f"✅ Option {option_code} done in {elapsed:.1f}s")
    return {"status": "updated", "option_code": option_code, "languages_written": written_langs}


def sync_all_options_for_closed_tour_from_data(
    api,
    translator,
    store: StateStore,
    supplier_id: str,
    closed_tour_entry: Dict[str, Any],
    target_languages: List[str],
    closed_tour_code: str,
    dry_run: bool = True,
    force: bool = False,
) -> List[Dict[str, Any]]:
    """
    IMPORTANT: `closed_tour_code` must be the code originally used to fetch
    this entry (api.get_closed_tour(supplier_id, closed_tour_code)) — NOT
    closed_tour_entry.get("code"). See the docstring on
    sync_closed_tour_from_data for why: those two values can genuinely
    differ, and the option endpoints need the queried one.
    """
    modality_codes = closed_tour_entry.get("modalityCodes", [])
    if not modality_codes:
        return [{"status": "skipped", "closed_tour_code": closed_tour_code, "reason": "no options"}]

    option_entries = {}
    with ThreadPoolExecutor(max_workers=MAX_OPTION_WORKERS) as executor:
        future_to_code = {
            executor.submit(api.get_closed_tour_option, supplier_id, closed_tour_code, opt_code): opt_code
            for opt_code in modality_codes
        }
        for future in as_completed(future_to_code):
            opt_code = future_to_code[future]
            try:
                option = future.result()
                if isinstance(option, dict) and "error" in option:
                    option_entries[opt_code] = {"error": option}
                else:
                    option_entries[opt_code] = option
            except Exception as e:
                option_entries[opt_code] = {"error": str(e)}

    results = []
    for opt_code in modality_codes:
        option = option_entries.get(opt_code)
        if option is None:
            results.append({"status": "fetch_failed", "option_code": opt_code, "detail": "No response"})
            continue
        if isinstance(option, dict) and "error" in option:
            results.append({"status": "fetch_failed", "option_code": opt_code, "detail": option["error"]})
            continue
        result = sync_closed_tour_option_from_data(
            api, translator, store, supplier_id, option, closed_tour_code, opt_code,
            target_languages, dry_run=dry_run, force=force
        )
        results.append(result)
    return results


# =========================================================================
# ENTRY POINT (single closed tour code — no bulk "sync all", see module docstring)
# =========================================================================

def sync_closed_tour(
    api,
    translator,
    store: StateStore,
    supplier_id: str,
    closed_tour_code: str,
    target_languages: List[str],
    dry_run: bool = True,
    force: bool = False,
) -> Dict[str, Any]:
    """
    The single entry point for Closed Tours: fetches by code, and if the
    code doesn't exist for this supplier, returns a clear "not_found"
    status (rather than a raw API error) so the calling UI can show the
    human an unambiguous "that code wasn't found" message — per your
    instruction: check availability, translate if found, error clearly if
    not.
    """
    entry = api.get_closed_tour(supplier_id, closed_tour_code)
    if isinstance(entry, dict) and "error" in entry:
        error_code = entry.get("error")
        if error_code == 404:
            return {
                "status": "not_found",
                "closed_tour_code": closed_tour_code,
                "reason": (
                    f"No closed tour found for supplier {supplier_id} with code "
                    f"'{closed_tour_code}'. Double-check the code and try again."
                ),
            }
        return {"status": "fetch_failed", "closed_tour_code": closed_tour_code, "detail": entry}

    main_result = sync_closed_tour_from_data(
        api, translator, store, supplier_id, entry, target_languages,
        closed_tour_code, dry_run=dry_run, force=force
    )

    option_results = sync_all_options_for_closed_tour_from_data(
        api, translator, store, supplier_id, entry, target_languages,
        closed_tour_code, dry_run=dry_run, force=force
    ) if isinstance(main_result, dict) and main_result.get("status") not in ("fetch_failed",) else []

    if isinstance(main_result, dict):
        main_result["options"] = option_results

    return main_result
