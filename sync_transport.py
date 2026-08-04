"""
sync_transport.py — Sync transports (main + options) with self‑healing verification.
"""

import json
import re
import time
from typing import Dict, Any, List, Optional
from concurrent.futures import ThreadPoolExecutor, as_completed

from state_store import StateStore, compute_hash
from translator import get_translator, translate_in_batches

# ---- Configuration ----
BATCH_SIZE = 10
DELAY_BETWEEN_BATCHES = 2
MAX_OPTION_WORKERS = 5

# ---- Main transport fields ----
MAIN_TEXT_FIELDS = ("name", "description")


def strip_html_and_compress(text: str) -> str:
    """
    NO-OP passthrough now. This used to strip every HTML tag out of a
    field before sending it to the translator, which silently destroyed
    any real formatting the source field had (bullet lists, bold, etc.).
    translator.py's SYSTEM_PROMPT already explicitly instructs the model
    to "preserve HTML tags ... EXACTLY as they appear, untouched, in the
    same position" — but that instruction is meaningless if the tags are
    stripped out before the model ever sees them. Confirmed as the cause
    of translated Closed Tour fields losing all formatting (came back as
    flat <p> text instead of the original's <ul><li>/<b> structure); this
    file shares the exact same bug for any HTML-bearing field, so it's
    fixed the same way here.
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


def extract_translatable_fields_from_transport(transport_entry: Dict[str, Any]) -> Dict[str, str]:
    datasheets = transport_entry.get("datasheets")
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
    for f in MAIN_TEXT_FIELDS:
        val = en_entry.get(f)
        if isinstance(val, str) and val.strip():
            fields[f] = val
    return fields


def get_existing_content_for_language(entry: Dict[str, Any], lang: str) -> Dict[str, str]:
    """
    Look for genuinely per-language existing content for `lang`, checking
    (in order) datasheets, translations, and remarks — the ONLY places
    per-language name/description ever actually get WRITTEN for a transport
    (see build_updated_datasheets / sync_transport_from_data: every write
    nests translated content inside one of these three, never at the
    entry's top level).

    IMPORTANT — this used to also fall back to the transport's top-level
    "name"/"description" fields when no per-language entry was found for
    `lang`. That fallback is what caused a real, confirmed live bug: a
    genuine transport (TRANSPORT-408971, "One-way transfer Praslin - La
    Digue") has a top-level "name" field (a static reference copy of the EN
    name — never itself translated) but NO top-level "description" field at
    all; "description" only ever exists inside datasheets.EN. When the
    self-healing/verify check compared that partial fallback
    ({"name": EN_name} only — "description" simply missing/None) against
    the real source fields ({"name": EN_name, "description": EN_desc}), the
    missing "description" key made the comparison register as "differs from
    source" — which the surrounding logic (wrongly) reads as "already
    translated". Result: on the very FIRST sync of this transport, before
    the translator was ever called, the state store got marked "done" for
    every target language and the live run reported "up_to_date" — name and
    description were never translated at all, silently, forever (every
    later run kept trusting that same stale state).

    Fix: removed the top-level fallback entirely. The only real source of
    per-language transport content is datasheets[lang] (or translations/
    remarks, if a transport ever uses those instead) — if none of those has
    `lang` yet, the correct signal is simply "not translated yet" (empty
    dict), not a guess pieced together from unrelated top-level fields that
    are never actually written to.

    If you're re-running a transport that already got stuck reporting
    "up_to_date" without ever writing a translation (from before this fix),
    the stale "done" state for it needs to be overridden once with --force
    so it actually retranslates and records the real result; after that,
    normal runs behave correctly on their own.
    """
    for key in ("datasheets", "translations", "remarks"):
        container = entry.get(key)
        if container and isinstance(container, dict):
            lang_entry = container.get(lang)
            if lang_entry and isinstance(lang_entry, dict):
                fields = {}
                for k, v in lang_entry.items():
                    if isinstance(v, str) and v.strip():
                        fields[k] = v
                if fields:
                    return fields
            elif isinstance(lang_entry, str) and lang_entry.strip():
                return {"remarks": lang_entry}
    return {}


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
        existing = get_existing_content_for_language(current_entry, lang)
        if not existing:
            truly_needed.append(lang)
            continue

        is_identical = True
        for field, src_text in source_fields.items():
            if existing.get(field) != src_text:
                is_identical = False
                break

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


def sync_transport_from_data(
    api,
    translator,
    store: StateStore,
    supplier_id: str,
    transport_entry: Dict[str, Any],
    target_languages: List[str],
    dry_run: bool = True,
    force: bool = False,
) -> Dict[str, Any]:
    start_time = time.time()
    transport_id = transport_entry.get("id")
    if not transport_id:
        return {"status": "skipped", "reason": "no 'id' field"}

    datasheets = transport_entry.get("datasheets")
    if not datasheets:
        return {"status": "skipped", "transport_id": transport_id, "reason": "no datasheets"}

    translatable = extract_translatable_fields_from_transport(transport_entry)
    if not translatable:
        return {"status": "skipped", "transport_id": transport_id, "reason": "no translatable fields"}

    source_hash = compute_hash(translatable)
    t0 = time.time()
    if force:
        needed = list(target_languages)
    else:
        needed = verify_and_filter_needed(
            store, "transport", supplier_id, transport_id, source_hash,
            target_languages, transport_entry, translatable
        )

    # --- Self‑healing: if state says all done, verify a sample language ---
    if not needed:
        sample_lang = "FR" if "FR" in target_languages else target_languages[0] if target_languages else "EN"
        existing = get_existing_content_for_language(transport_entry, sample_lang)
        if existing:
            is_identical = True
            for field, src_text in translatable.items():
                if existing.get(field) != src_text:
                    is_identical = False
                    break
            if is_identical:
                print(f"🔍 Verification: {sample_lang} content is identical to source or missing. Forcing re-translation for all languages.")
                needed = list(target_languages)
            else:
                # Content differs – it's actually translated, so up_to_date
                verify_time = time.time() - t0
                return {"status": "up_to_date", "transport_id": transport_id}
        else:
            print(f"🔍 Verification: {sample_lang} has no content. Forcing re-translation for all languages.")
            needed = list(target_languages)

    verify_time = time.time() - t0

    if not needed:
        return {"status": "up_to_date", "transport_id": transport_id}

    compressed_translatable = compress_translatable_fields(translatable)

    translation_start = time.time()
    combined_translations, failed_languages = translate_in_batches(translator, compressed_translatable, needed, batch_size=BATCH_SIZE)
    translation_time = time.time() - translation_start

    # NOTE: this used to filter out any language whose translated text was
    # identical to the source, on the assumption that meant the call had
    # silently failed. That's wrong for short/common words that legitimately
    # translate to themselves in several languages — it would silently and
    # permanently drop a correct result. Now we only exclude languages
    # translate_in_batches itself reports as having failed (see its
    # docstring in translator.py); everything else is trusted as real.
    successful = {}
    for lang, trans in combined_translations.items():
        if lang in failed_languages:
            print(f"⚠️  Translation batch for {lang} failed; skipping.")
        else:
            successful[lang] = trans

    if not successful:
        return {"status": "skipped", "transport_id": transport_id, "reason": "no successful translations"}

    en_entry = datasheets.get("EN") or datasheets.get("EN_US") or {}
    new_datasheets = build_updated_datasheets(datasheets, successful, en_entry)

    if dry_run:
        preview = {lang: {k: v for k, v in trans.items() if k in MAIN_TEXT_FIELDS}
                   for lang, trans in successful.items()}
        return {"status": "dry_run_preview", "transport_id": transport_id,
                "languages": list(successful.keys()), "preview": preview}

    write_start = time.time()
    payload = dict(transport_entry)
    payload["datasheets"] = new_datasheets
    payload.setdefault("airlineCode", "")
    result = api.update_transport(supplier_id, payload)
    write_time = time.time() - write_start
    if isinstance(result, dict) and "error" in result:
        return {"status": "put_failed", "transport_id": transport_id, "detail": result}

    written_langs = list(successful.keys())
    prior_state = store.get_state("transport", supplier_id, transport_id)
    prior_langs = prior_state["translated_languages"] if prior_state and prior_state["source_hash"] == source_hash else []
    all_langs = sorted(set(prior_langs) | set(written_langs))
    store.upsert_state("transport", supplier_id, transport_id, source_hash, all_langs)

    total_time = time.time() - start_time
    print(f"✅ Transport {transport_id} done in {total_time:.1f}s "
          f"(verify: {verify_time:.1f}s, translate: {translation_time:.1f}s, write: {write_time:.1f}s)")
    return {"status": "updated", "transport_id": transport_id, "languages_written": written_langs}


def sync_transport(api, translator, store: StateStore,
                   supplier_id: str, transport_id: str,
                   target_languages: List[str],
                   dry_run: bool = True, force: bool = False) -> Dict[str, Any]:
    transport = api.get_transport(supplier_id, transport_id)
    if isinstance(transport, dict) and "error" in transport:
        return {"status": "fetch_failed", "transport_id": transport_id, "detail": transport}

    main_result = sync_transport_from_data(api, translator, store, supplier_id, transport,
                                           target_languages, dry_run=dry_run, force=force)

    if transport.get("optionCodes"):
        option_results = sync_all_options_for_transport_from_data(
            api, translator, store, supplier_id, transport, target_languages,
            dry_run=dry_run, force=force
        )
        if isinstance(main_result, dict):
            main_result["options"] = option_results
    else:
        if isinstance(main_result, dict):
            main_result["options"] = [{"status": "skipped", "reason": "no options"}]

    return main_result


# ---- Option functions (unchanged) ----
def extract_translatable_fields_from_transport_option(option_entry: Dict[str, Any]) -> Dict[str, str]:
    fields = {}
    # Try translations
    translations = option_entry.get("translations")
    if translations:
        en_entry = translations.get("EN") or translations.get("EN_US") or {}
        if isinstance(en_entry, dict):
            for k, v in en_entry.items():
                if isinstance(v, str) and v.strip():
                    fields[k] = v
        elif isinstance(en_entry, str) and en_entry.strip():
            fields["name"] = en_entry
    # Try datasheets
    datasheets = option_entry.get("datasheets")
    if datasheets:
        en_entry = datasheets.get("EN") or datasheets.get("EN_US") or {}
        if isinstance(en_entry, dict):
            for k, v in en_entry.items():
                if isinstance(v, str) and v.strip() and k not in fields:
                    fields[k] = v
    # Try remarks
    remarks = option_entry.get("remarks")
    if remarks and isinstance(remarks, dict):
        en_remarks = remarks.get("EN") or remarks.get("EN_US") or {}
        if isinstance(en_remarks, dict):
            for k, v in en_remarks.items():
                if isinstance(v, str) and v.strip() and k not in fields:
                    fields[k] = v
        elif isinstance(en_remarks, str) and en_remarks.strip():
            fields["remarks"] = en_remarks
    # Also top-level name/description
    for f in ("name", "description"):
        val = option_entry.get(f)
        if isinstance(val, str) and val.strip() and f not in fields:
            fields[f] = val
    return fields


def build_updated_option(original_option: Dict[str, Any],
                         translations_by_lang: Dict[str, Dict[str, str]]) -> Dict[str, Any]:
    new_option = dict(original_option)

    # Determine where to put translations (prefer translations, then datasheets, then remarks)
    target_key = None
    if "translations" in original_option and isinstance(original_option["translations"], dict):
        target_key = "translations"
    elif "datasheets" in original_option and isinstance(original_option["datasheets"], dict):
        target_key = "datasheets"
    elif "remarks" in original_option and isinstance(original_option["remarks"], dict):
        target_key = "remarks"
    else:
        target_key = "translations"
        new_option[target_key] = {}

    original_map = new_option.get(target_key, {})
    new_map = dict(original_map)

    for lang, trans in translations_by_lang.items():
        if isinstance(original_map.get(lang), dict):
            base = dict(original_map.get(lang, {}))
        else:
            base = {}
        for field, text in trans.items():
            base[field] = text
        if target_key == "remarks" and len(trans) == 1 and "remarks" in trans:
            new_map[lang] = trans["remarks"]
        else:
            new_map[lang] = base

    new_option[target_key] = new_map
    return new_option


def sync_transport_option_from_data(
    api,
    translator,
    store: StateStore,
    supplier_id: str,
    option_entry: Dict[str, Any],
    transport_id: str,
    option_code: str,
    target_languages: List[str],
    dry_run: bool = True,
    force: bool = False,
) -> Dict[str, Any]:
    start_time = time.time()
    translatable = extract_translatable_fields_from_transport_option(option_entry)
    if not translatable:
        return {"status": "skipped", "option_code": option_code, "reason": "no translatable fields"}

    source_hash = compute_hash(translatable)
    entity_id = f"{transport_id}|{option_code}"
    if force:
        needed = list(target_languages)
    else:
        needed = verify_and_filter_needed(
            store, "transport_option", supplier_id, entity_id, source_hash,
            target_languages, option_entry, translatable, option_code=option_code
        )

    if not needed:
        return {"status": "up_to_date", "option_code": option_code}

    compressed_translatable = compress_translatable_fields(translatable)
    combined_translations, failed_languages = translate_in_batches(translator, compressed_translatable, needed, batch_size=BATCH_SIZE)

    # See the comment on the main-entity translate call above: an option's
    # translated text can legitimately be identical to the source for
    # short/common words — only drop languages translate_in_batches itself
    # reports as having failed.
    successful = {}
    for lang, trans in combined_translations.items():
        if lang in failed_languages:
            print(f"⚠️  Translation batch for {lang} failed; skipping.")
        else:
            successful[lang] = trans

    if not successful:
        return {"status": "skipped", "option_code": option_code, "reason": "no successful translations"}

    updated_option = build_updated_option(option_entry, successful)

    # Ensure baggageAllowance is a number (default to 1)
    if "baggageAllowance" not in updated_option or not isinstance(updated_option.get("baggageAllowance"), (int, float)):
        updated_option["baggageAllowance"] = 1

    if dry_run:
        preview = {lang: {k: v for k, v in trans.items()} for lang, trans in successful.items()}
        return {"status": "dry_run_preview", "option_code": option_code,
                "languages": list(successful.keys()), "preview": preview}

    result = api.update_transport_option(supplier_id, transport_id, updated_option)
    if isinstance(result, dict) and "error" in result:
        return {"status": "put_failed", "option_code": option_code, "detail": result}

    written_langs = list(successful.keys())
    prior_state = store.get_state("transport_option", supplier_id, entity_id, option_code=option_code)
    prior_langs = prior_state["translated_languages"] if prior_state and prior_state["source_hash"] == source_hash else []
    all_langs = sorted(set(prior_langs) | set(written_langs))
    store.upsert_state("transport_option", supplier_id, entity_id, source_hash, all_langs, option_code=option_code)

    elapsed = time.time() - start_time
    print(f"✅ Option {option_code} done in {elapsed:.1f}s")
    return {"status": "updated", "option_code": option_code, "languages_written": written_langs}


def sync_all_options_for_transport_from_data(
    api,
    translator,
    store: StateStore,
    supplier_id: str,
    transport_entry: Dict[str, Any],
    target_languages: List[str],
    dry_run: bool = True,
    force: bool = False,
) -> List[Dict[str, Any]]:
    option_codes = transport_entry.get("optionCodes", [])
    if not option_codes:
        return [{"status": "skipped", "transport_id": transport_entry.get("id"), "reason": "no options"}]

    transport_id = transport_entry.get("id")
    option_entries = {}
    with ThreadPoolExecutor(max_workers=MAX_OPTION_WORKERS) as executor:
        future_to_code = {
            executor.submit(api.get_transport_option, supplier_id, transport_id, opt_code): opt_code
            for opt_code in option_codes
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
    for opt_code in option_codes:
        option = option_entries.get(opt_code)
        if option is None:
            results.append({"status": "fetch_failed", "option_code": opt_code, "detail": "No response"})
            continue
        if isinstance(option, dict) and "error" in option:
            results.append({"status": "fetch_failed", "option_code": opt_code, "detail": option["error"]})
            continue
        result = sync_transport_option_from_data(
            api, translator, store, supplier_id, option, transport_id, opt_code,
            target_languages, dry_run=dry_run, force=force
        )
        results.append(result)
    return results


def fetch_all_transports(api, supplier_id: str, limit: int = None) -> List[Dict[str, Any]]:
    data = api.get_transports(supplier_id)
    transports = data.get("transport", []) if isinstance(data, dict) else []
    if limit:
        transports = transports[:limit]
    return transports


def sync_all_transports_for_supplier(
    api,
    translator,
    store: StateStore,
    supplier_id: str,
    target_languages: List[str],
    dry_run: bool = True,
    limit: int = None,
    force: bool = False,
) -> List[Dict[str, Any]]:
    transports = fetch_all_transports(api, supplier_id, limit=limit)
    print(f"📋 Found {len(transports)} transport(s) for supplier {supplier_id}.")

    results = []
    for t in transports:
        transport_id = t.get("id")
        if not transport_id:
            results.append({"status": "skipped", "reason": "no id field", "raw": t})
            continue

        main_result = sync_transport_from_data(
            api, translator, store, supplier_id, t, target_languages,
            dry_run=dry_run, force=force
        )
        results.append(main_result)

        if t.get("optionCodes"):
            option_results = sync_all_options_for_transport_from_data(
                api, translator, store, supplier_id, t, target_languages,
                dry_run=dry_run, force=force
            )
            if isinstance(main_result, dict):
                main_result["options"] = option_results
            else:
                results.extend(option_results)
        else:
            if isinstance(main_result, dict):
                main_result["options"] = [{"status": "skipped", "reason": "no options"}]
    return results
