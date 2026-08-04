"""
sync_hotel.py — Sync hotels (main + rooms + supplements + offers).
Room name is NOT translated; only room description is translated.
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
MAX_WORKERS = 5

# ---- Translatable fields ----
HOTEL_TEXT_FIELDS = ("hotelname", "description")
ROOM_TEXT_FIELDS = ("description",)   # only description, name excluded
SUPPLEMENT_TEXT_FIELDS = ("description",)
OFFER_TEXT_FIELDS = ("description",)


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


# ---------- Main hotel ----------
def extract_translatable_fields_from_hotel(hotel_entry: Dict[str, Any]) -> Dict[str, str]:
    fields = {}
    hotelname = hotel_entry.get("hotelname")
    if isinstance(hotelname, str) and hotelname.strip():
        fields["hotelname"] = hotelname
    descriptions = hotel_entry.get("descriptions", [])
    for desc in descriptions:
        if desc.get("language") == "EN" and desc.get("description"):
            fields["description"] = desc["description"]
            break
    return fields


def get_existing_hotel_content_for_language(hotel_entry: Dict[str, Any], lang: str) -> Dict[str, str]:
    result = {}
    descriptions = hotel_entry.get("descriptions", [])
    for desc in descriptions:
        if desc.get("language") == lang:
            if "name" in desc and desc["name"]:
                result["hotelname"] = desc["name"]
            if "description" in desc and desc["description"]:
                result["description"] = desc["description"]
            break
    return result


def build_updated_hotel_descriptions(
    original_descriptions: List[Dict[str, Any]],
    translations_by_lang: Dict[str, Dict[str, str]],
    en_name: str,
    en_desc: str,
) -> List[Dict[str, Any]]:
    new_descriptions = [dict(d) for d in original_descriptions]
    existing_langs = {d.get("language") for d in new_descriptions}
    for lang, trans in translations_by_lang.items():
        if lang in existing_langs:
            for d in new_descriptions:
                if d.get("language") == lang:
                    if "hotelname" in trans:
                        d["name"] = trans["hotelname"]
                    if "description" in trans:
                        d["description"] = trans["description"]
                    break
        else:
            new_entry = {"language": lang}
            if "hotelname" in trans:
                new_entry["name"] = trans["hotelname"]
            if "description" in trans:
                new_entry["description"] = trans["description"]
            new_descriptions.append(new_entry)
    return new_descriptions


# ---------- Rooms (description only) ----------
def extract_translatable_fields_from_room(room_entry: Dict[str, Any]) -> Dict[str, str]:
    fields = {}
    # Only description is translatable; name is skipped.
    description = room_entry.get("description")
    if isinstance(description, str) and description.strip():
        fields["description"] = description
    else:
        # Check translations/datasheets for description if not top-level
        translations = room_entry.get("translations", {})
        en_trans = translations.get("EN") or translations.get("EN_US")
        if isinstance(en_trans, dict) and en_trans.get("description"):
            fields["description"] = en_trans["description"]
        else:
            datasheets = room_entry.get("datasheets", {})
            en_ds = datasheets.get("EN") or datasheets.get("EN_US")
            if isinstance(en_ds, dict) and en_ds.get("description"):
                fields["description"] = en_ds["description"]
    return fields


def get_existing_room_content_for_language(room_entry: Dict[str, Any], lang: str) -> Dict[str, str]:
    result = {}
    translations = room_entry.get("translations", {})
    lang_trans = translations.get(lang)
    if isinstance(lang_trans, dict):
        if "description" in lang_trans and lang_trans["description"]:
            result["description"] = lang_trans["description"]
        return result
    datasheets = room_entry.get("datasheets", {})
    lang_ds = datasheets.get(lang)
    if isinstance(lang_ds, dict):
        if "description" in lang_ds and lang_ds["description"]:
            result["description"] = lang_ds["description"]
    return result


def build_updated_room(original_room: Dict[str, Any],
                       translations_by_lang: Dict[str, Dict[str, str]]) -> Dict[str, Any]:
    new_room = dict(original_room)
    target_key = None
    if "translations" in new_room and isinstance(new_room["translations"], dict):
        target_key = "translations"
    elif "datasheets" in new_room and isinstance(new_room["datasheets"], dict):
        target_key = "datasheets"
    else:
        target_key = "translations"
        new_room[target_key] = {}
    target_map = dict(new_room.get(target_key, {}))
    for lang, trans in translations_by_lang.items():
        base = dict(target_map.get(lang, {}))   # preserve existing fields (e.g., name)
        if "description" in trans:
            base["description"] = trans["description"]
        target_map[lang] = base
    new_room[target_key] = target_map
    return new_room


# ---------- Supplements ----------
def extract_translatable_fields_from_supplement(supp_entry: Dict[str, Any]) -> Dict[str, str]:
    fields = {}
    names = supp_entry.get("names", [])
    for name_obj in names:
        if name_obj.get("language") == "EN" and name_obj.get("description"):
            fields["description"] = name_obj["description"]
            break
    return fields


def get_existing_supplement_content_for_language(supp_entry: Dict[str, Any], lang: str) -> Dict[str, str]:
    names = supp_entry.get("names", [])
    for name_obj in names:
        if name_obj.get("language") == lang:
            desc = name_obj.get("description")
            if desc:
                return {"description": desc}
    return {}


def build_updated_supplement(original_supp: Dict[str, Any],
                             translations_by_lang: Dict[str, Dict[str, str]]) -> Dict[str, Any]:
    new_supp = dict(original_supp)
    existing_names = [dict(n) for n in new_supp.get("names", [])]
    existing_langs = {n.get("language") for n in existing_names}
    for lang, trans in translations_by_lang.items():
        if lang in existing_langs:
            for n in existing_names:
                if n.get("language") == lang:
                    if "description" in trans:
                        n["description"] = trans["description"]
                    break
        else:
            new_entry = {"language": lang}
            if "description" in trans:
                new_entry["description"] = trans["description"]
            existing_names.append(new_entry)
    new_supp["names"] = existing_names
    return new_supp


# ---------- Offers ----------
def extract_translatable_fields_from_offer(offer_entry: Dict[str, Any]) -> Dict[str, str]:
    fields = {}
    names = offer_entry.get("names", [])
    for name_obj in names:
        if name_obj.get("language") == "EN" and name_obj.get("description"):
            fields["description"] = name_obj["description"]
            break
    return fields


def get_existing_offer_content_for_language(offer_entry: Dict[str, Any], lang: str) -> Dict[str, str]:
    names = offer_entry.get("names", [])
    for name_obj in names:
        if name_obj.get("language") == lang:
            desc = name_obj.get("description")
            if desc:
                return {"description": desc}
    return {}


def build_updated_offer(original_offer: Dict[str, Any],
                        translations_by_lang: Dict[str, Dict[str, str]]) -> Dict[str, Any]:
    new_offer = dict(original_offer)
    existing_names = [dict(n) for n in new_offer.get("names", [])]
    existing_langs = {n.get("language") for n in existing_names}
    for lang, trans in translations_by_lang.items():
        if lang in existing_langs:
            for n in existing_names:
                if n.get("language") == lang:
                    if "description" in trans:
                        n["description"] = trans["description"]
                    break
        else:
            new_entry = {"language": lang}
            if "description" in trans:
                new_entry["description"] = trans["description"]
            existing_names.append(new_entry)
    new_offer["names"] = existing_names
    return new_offer


# ---------- Main sync functions ----------
def sync_hotel_main(api, translator, store: StateStore,
                    supplier_id: str, hotel_entry: Dict[str, Any],
                    target_languages: List[str],
                    dry_run: bool = True, force: bool = False) -> Dict[str, Any]:
    contract_id = hotel_entry.get("contractId")
    if not contract_id:
        return {"status": "skipped", "reason": "no contractId"}

    translatable = extract_translatable_fields_from_hotel(hotel_entry)
    if not translatable:
        return {"status": "skipped", "contract_id": contract_id, "reason": "no translatable fields"}

    source_hash = compute_hash(translatable)
    t0 = time.time()
    if force:
        needed = list(target_languages)
    else:
        state = store.get_state("hotel", supplier_id, contract_id)
        if state is None or state["source_hash"] != source_hash:
            needed = list(target_languages)
        else:
            already_done = set(state["translated_languages"])
            needed = [lang for lang in target_languages if lang not in already_done]
        # Verify existing content
        truly_needed = []
        languages_to_add = []
        for lang in needed:
            existing = get_existing_hotel_content_for_language(hotel_entry, lang)
            if not existing:
                truly_needed.append(lang)
                continue
            is_identical = True
            for field, src in translatable.items():
                if existing.get(field) != src:
                    is_identical = False
                    break
            if is_identical:
                truly_needed.append(lang)
            else:
                languages_to_add.append(lang)
        if languages_to_add:
            prior_state = store.get_state("hotel", supplier_id, contract_id)
            prior_langs = prior_state["translated_languages"] if prior_state and prior_state["source_hash"] == source_hash else []
            all_langs = sorted(set(prior_langs) | set(languages_to_add))
            store.upsert_state("hotel", supplier_id, contract_id, source_hash, all_langs)
        needed = truly_needed

    # Self-healing
    if not needed:
        sample_lang = "FR" if "FR" in target_languages else target_languages[0] if target_languages else "EN"
        existing = get_existing_hotel_content_for_language(hotel_entry, sample_lang)
        if existing:
            is_identical = True
            for field, src in translatable.items():
                if existing.get(field) != src:
                    is_identical = False
                    break
            if is_identical:
                print(f"🔍 Hotel verification: {sample_lang} identical. Re-translating.")
                needed = list(target_languages)
            else:
                return {"status": "up_to_date", "contract_id": contract_id}
        else:
            print(f"🔍 Hotel verification: {sample_lang} missing. Re-translating.")
            needed = list(target_languages)

    if not needed:
        return {"status": "up_to_date", "contract_id": contract_id}

    # Translate
    compressed = compress_translatable_fields(translatable)
    combined, failed_languages = translate_in_batches(translator, compressed, needed, batch_size=BATCH_SIZE)

    # NOTE: no longer treats "translation identical to source" as a failure
    # signal — a hotel name/description can legitimately be the same word
    # in several languages (brand names, short common words). Only
    # languages translate_in_batches itself reports as failed get dropped.
    successful = {}
    for lang, trans in combined.items():
        if lang in failed_languages:
            print(f"⚠️  Hotel translation batch for {lang} failed; skipping.")
        else:
            successful[lang] = trans

    if not successful:
        return {"status": "skipped", "contract_id": contract_id, "reason": "no successful translations"}

    en_name = translatable.get("hotelname", "")
    en_desc = translatable.get("description", "")
    new_descriptions = build_updated_hotel_descriptions(
        hotel_entry.get("descriptions", []),
        successful,
        en_name,
        en_desc
    )

    if dry_run:
        preview = {lang: {k: v for k, v in trans.items() if k in HOTEL_TEXT_FIELDS}
                   for lang, trans in successful.items()}
        return {"status": "dry_run_preview", "contract_id": contract_id,
                "languages": list(successful.keys()), "preview": preview}

    payload = dict(hotel_entry)
    payload["descriptions"] = new_descriptions
    result = api.update_hotel(supplier_id, payload)
    if isinstance(result, dict) and "error" in result:
        return {"status": "put_failed", "contract_id": contract_id, "detail": result}

    written_langs = list(successful.keys())
    prior_state = store.get_state("hotel", supplier_id, contract_id)
    prior_langs = prior_state["translated_languages"] if prior_state and prior_state["source_hash"] == source_hash else []
    all_langs = sorted(set(prior_langs) | set(written_langs))
    store.upsert_state("hotel", supplier_id, contract_id, source_hash, all_langs)
    return {"status": "updated", "contract_id": contract_id, "languages_written": written_langs}


def sync_hotel(api, translator, store: StateStore,
               supplier_id: str, hotel_entry: Dict[str, Any],
               target_languages: List[str],
               dry_run: bool = True, force: bool = False) -> Dict[str, Any]:
    """
    Sync a hotel: main, rooms, supplements, offers. Returns result with status for each.
    """
    # ---- Main hotel ----
    main_result = sync_hotel_main(api, translator, store, supplier_id, hotel_entry,
                                  target_languages, dry_run=dry_run, force=force)
    results = {"main": main_result, "rooms": [], "supplements": [], "offers": []}

    # ---- Rooms ----
    rooms = hotel_entry.get("rooms", [])
    if rooms:
        room_results = []
        for room in rooms:
            room_result = sync_room(api, translator, store, supplier_id, hotel_entry, room,
                                    target_languages, dry_run=dry_run, force=force)
            room_results.append(room_result)
        results["rooms"] = room_results

    # ---- Supplements ----
    supplements = hotel_entry.get("supplements", [])
    if supplements:
        supp_results = []
        for supp in supplements:
            supp_result = sync_supplement(api, translator, store, supplier_id, hotel_entry, supp,
                                          target_languages, dry_run=dry_run, force=force)
            supp_results.append(supp_result)
        results["supplements"] = supp_results

    # ---- Offers ----
    offers = hotel_entry.get("offers", [])
    if offers:
        offer_results = []
        for offer in offers:
            offer_result = sync_offer(api, translator, store, supplier_id, hotel_entry, offer,
                                      target_languages, dry_run=dry_run, force=force)
            offer_results.append(offer_result)
        results["offers"] = offer_results

    # ---- Build updated payload if any room, supplement, or offer was updated ----
    any_updated = False
    updated_hotel = dict(hotel_entry)

    # Rooms
    if rooms and any(r.get("status") == "updated" for r in room_results):
        new_rooms = []
        for idx, room in enumerate(rooms):
            r = room_results[idx]
            if r.get("status") == "updated" and "updated_room" in r:
                new_rooms.append(r["updated_room"])
            else:
                new_rooms.append(room)
        updated_hotel["rooms"] = new_rooms
        any_updated = True

    # Supplements
    if supplements and any(s.get("status") == "updated" for s in supp_results):
        new_supps = []
        for idx, supp in enumerate(supplements):
            s = supp_results[idx]
            if s.get("status") == "updated" and "updated_supplement" in s:
                new_supps.append(s["updated_supplement"])
            else:
                new_supps.append(supp)
        updated_hotel["supplements"] = new_supps
        any_updated = True

    # Offers
    if offers and any(o.get("status") == "updated" for o in offer_results):
        new_offers = []
        for idx, offer in enumerate(offers):
            o = offer_results[idx]
            if o.get("status") == "updated" and "updated_offer" in o:
                new_offers.append(o["updated_offer"])
            else:
                new_offers.append(offer)
        updated_hotel["offers"] = new_offers
        any_updated = True

    if any_updated and not dry_run:
        # Send PUT with updated hotel
        result = api.update_hotel(supplier_id, updated_hotel)
        if isinstance(result, dict) and "error" in result:
            # Mark put_failed for rooms/supps/offers
            for r in room_results:
                if r.get("status") == "updated":
                    r["status"] = "put_failed"
                    r["detail"] = result
            for s in supp_results:
                if s.get("status") == "updated":
                    s["status"] = "put_failed"
                    s["detail"] = result
            for o in offer_results:
                if o.get("status") == "updated":
                    o["status"] = "put_failed"
                    o["detail"] = result
            results["rooms"] = room_results
            results["supplements"] = supp_results
            results["offers"] = offer_results
        else:
            # Update state for rooms, supplements, offers
            for r in room_results:
                if r.get("status") == "updated" and "languages_written" in r:
                    room_provider_code = r.get("room_code")
                    entity_id = f"{hotel_entry.get('contractId')}|room|{room_provider_code}"
                    translatable_room = extract_translatable_fields_from_room(room)
                    room_source_hash = compute_hash(translatable_room)
                    prior_state = store.get_state("hotel_room", supplier_id, entity_id, option_code=room_provider_code)
                    prior_langs = prior_state["translated_languages"] if prior_state else []
                    all_langs = sorted(set(prior_langs) | set(r["languages_written"]))
                    store.upsert_state("hotel_room", supplier_id, entity_id, room_source_hash, all_langs, option_code=room_provider_code)
            for s in supp_results:
                if s.get("status") == "updated" and "languages_written" in s:
                    supp_provider_code = s.get("supplement_code")
                    entity_id = f"{hotel_entry.get('contractId')}|supplement|{supp_provider_code}"
                    translatable_supp = extract_translatable_fields_from_supplement(supp)
                    supp_source_hash = compute_hash(translatable_supp)
                    prior_state = store.get_state("hotel_supplement", supplier_id, entity_id, option_code=supp_provider_code)
                    prior_langs = prior_state["translated_languages"] if prior_state else []
                    all_langs = sorted(set(prior_langs) | set(s["languages_written"]))
                    store.upsert_state("hotel_supplement", supplier_id, entity_id, supp_source_hash, all_langs, option_code=supp_provider_code)
            for o in offer_results:
                if o.get("status") == "updated" and "languages_written" in o:
                    offer_provider_code = o.get("offer_code")
                    entity_id = f"{hotel_entry.get('contractId')}|offer|{offer_provider_code}"
                    translatable_offer = extract_translatable_fields_from_offer(offer)
                    offer_source_hash = compute_hash(translatable_offer)
                    prior_state = store.get_state("hotel_offer", supplier_id, entity_id, option_code=offer_provider_code)
                    prior_langs = prior_state["translated_languages"] if prior_state else []
                    all_langs = sorted(set(prior_langs) | set(o["languages_written"]))
                    store.upsert_state("hotel_offer", supplier_id, entity_id, offer_source_hash, all_langs, option_code=offer_provider_code)

    return results


# ---------- Room sync ----------
def sync_room(api, translator, store: StateStore,
              supplier_id: str, hotel_entry: Dict[str, Any],
              room_entry: Dict[str, Any], target_languages: List[str],
              dry_run: bool = True, force: bool = False) -> Dict[str, Any]:
    contract_id = hotel_entry.get("contractId")
    room_provider_code = room_entry.get("providerCode")
    if not room_provider_code:
        return {"status": "skipped", "reason": "no providerCode in room"}

    translatable = extract_translatable_fields_from_room(room_entry)
    if not translatable:
        return {"status": "skipped", "room_code": room_provider_code, "reason": "no translatable fields"}

    source_hash = compute_hash(translatable)
    entity_id = f"{contract_id}|room|{room_provider_code}"
    if force:
        needed = list(target_languages)
    else:
        state = store.get_state("hotel_room", supplier_id, entity_id, option_code=room_provider_code)
        if state is None or state["source_hash"] != source_hash:
            needed = list(target_languages)
        else:
            already_done = set(state["translated_languages"])
            needed = [lang for lang in target_languages if lang not in already_done]
        # Verify
        truly_needed = []
        languages_to_add = []
        for lang in needed:
            existing = get_existing_room_content_for_language(room_entry, lang)
            if not existing:
                truly_needed.append(lang)
                continue
            is_identical = True
            for field, src in translatable.items():
                if existing.get(field) != src:
                    is_identical = False
                    break
            if is_identical:
                truly_needed.append(lang)
            else:
                languages_to_add.append(lang)
        if languages_to_add:
            prior_state = store.get_state("hotel_room", supplier_id, entity_id, option_code=room_provider_code)
            prior_langs = prior_state["translated_languages"] if prior_state and prior_state["source_hash"] == source_hash else []
            all_langs = sorted(set(prior_langs) | set(languages_to_add))
            store.upsert_state("hotel_room", supplier_id, entity_id, source_hash, all_langs, option_code=room_provider_code)
        needed = truly_needed

    # Self-healing
    if not needed:
        sample_lang = "FR" if "FR" in target_languages else target_languages[0] if target_languages else "EN"
        existing = get_existing_room_content_for_language(room_entry, sample_lang)
        if existing:
            is_identical = True
            for field, src in translatable.items():
                if existing.get(field) != src:
                    is_identical = False
                    break
            if is_identical:
                print(f"🔍 Room verification: {sample_lang} identical. Re-translating.")
                needed = list(target_languages)
            else:
                return {"status": "up_to_date", "room_code": room_provider_code}
        else:
            print(f"🔍 Room verification: {sample_lang} missing. Re-translating.")
            needed = list(target_languages)

    if not needed:
        return {"status": "up_to_date", "room_code": room_provider_code}

    # Translate
    compressed = compress_translatable_fields(translatable)
    combined, failed_languages = translate_in_batches(translator, compressed, needed, batch_size=BATCH_SIZE)

    # See the main-hotel translate call above: identical-to-source is not a
    # failure signal on its own — only translate_in_batches-reported
    # failures get dropped.
    successful = {}
    for lang, trans in combined.items():
        if lang in failed_languages:
            print(f"⚠️  Room translation batch for {lang} failed; skipping.")
        else:
            successful[lang] = trans

    if not successful:
        return {"status": "skipped", "room_code": room_provider_code, "reason": "no successful translations"}

    updated_room = build_updated_room(room_entry, successful)
    return {
        "status": "updated",
        "room_code": room_provider_code,
        "updated_room": updated_room,
        "languages_written": list(successful.keys())
    }


# ---------- Supplement sync ----------
def sync_supplement(api, translator, store: StateStore,
                    supplier_id: str, hotel_entry: Dict[str, Any],
                    supp_entry: Dict[str, Any], target_languages: List[str],
                    dry_run: bool = True, force: bool = False) -> Dict[str, Any]:
    contract_id = hotel_entry.get("contractId")
    supp_provider_code = supp_entry.get("providerCode")
    if not supp_provider_code:
        return {"status": "skipped", "reason": "no providerCode in supplement"}

    translatable = extract_translatable_fields_from_supplement(supp_entry)
    if not translatable:
        return {"status": "skipped", "supplement_code": supp_provider_code, "reason": "no translatable fields"}

    source_hash = compute_hash(translatable)
    entity_id = f"{contract_id}|supplement|{supp_provider_code}"
    if force:
        needed = list(target_languages)
    else:
        state = store.get_state("hotel_supplement", supplier_id, entity_id, option_code=supp_provider_code)
        if state is None or state["source_hash"] != source_hash:
            needed = list(target_languages)
        else:
            already_done = set(state["translated_languages"])
            needed = [lang for lang in target_languages if lang not in already_done]
        # Verify
        truly_needed = []
        languages_to_add = []
        for lang in needed:
            existing = get_existing_supplement_content_for_language(supp_entry, lang)
            if not existing:
                truly_needed.append(lang)
                continue
            is_identical = True
            for field, src in translatable.items():
                if existing.get(field) != src:
                    is_identical = False
                    break
            if is_identical:
                truly_needed.append(lang)
            else:
                languages_to_add.append(lang)
        if languages_to_add:
            prior_state = store.get_state("hotel_supplement", supplier_id, entity_id, option_code=supp_provider_code)
            prior_langs = prior_state["translated_languages"] if prior_state and prior_state["source_hash"] == source_hash else []
            all_langs = sorted(set(prior_langs) | set(languages_to_add))
            store.upsert_state("hotel_supplement", supplier_id, entity_id, source_hash, all_langs, option_code=supp_provider_code)
        needed = truly_needed

    # Self-healing
    if not needed:
        sample_lang = "FR" if "FR" in target_languages else target_languages[0] if target_languages else "EN"
        existing = get_existing_supplement_content_for_language(supp_entry, sample_lang)
        if existing:
            is_identical = True
            for field, src in translatable.items():
                if existing.get(field) != src:
                    is_identical = False
                    break
            if is_identical:
                print(f"🔍 Supplement verification: {sample_lang} identical. Re-translating.")
                needed = list(target_languages)
            else:
                return {"status": "up_to_date", "supplement_code": supp_provider_code}
        else:
            print(f"🔍 Supplement verification: {sample_lang} missing. Re-translating.")
            needed = list(target_languages)

    if not needed:
        return {"status": "up_to_date", "supplement_code": supp_provider_code}

    # Translate
    compressed = compress_translatable_fields(translatable)
    combined, failed_languages = translate_in_batches(translator, compressed, needed, batch_size=BATCH_SIZE)

    # See the main-hotel translate call above: identical-to-source is not a
    # failure signal on its own — only translate_in_batches-reported
    # failures get dropped.
    successful = {}
    for lang, trans in combined.items():
        if lang in failed_languages:
            print(f"⚠️  Supplement translation batch for {lang} failed; skipping.")
        else:
            successful[lang] = trans

    if not successful:
        return {"status": "skipped", "supplement_code": supp_provider_code, "reason": "no successful translations"}

    updated_supp = build_updated_supplement(supp_entry, successful)
    return {
        "status": "updated",
        "supplement_code": supp_provider_code,
        "updated_supplement": updated_supp,
        "languages_written": list(successful.keys())
    }


# ---------- Offer sync ----------
def sync_offer(api, translator, store: StateStore,
               supplier_id: str, hotel_entry: Dict[str, Any],
               offer_entry: Dict[str, Any], target_languages: List[str],
               dry_run: bool = True, force: bool = False) -> Dict[str, Any]:
    contract_id = hotel_entry.get("contractId")
    offer_provider_code = offer_entry.get("providerCode")
    if not offer_provider_code:
        return {"status": "skipped", "reason": "no providerCode in offer"}

    translatable = extract_translatable_fields_from_offer(offer_entry)
    if not translatable:
        return {"status": "skipped", "offer_code": offer_provider_code, "reason": "no translatable fields"}

    source_hash = compute_hash(translatable)
    entity_id = f"{contract_id}|offer|{offer_provider_code}"
    if force:
        needed = list(target_languages)
    else:
        state = store.get_state("hotel_offer", supplier_id, entity_id, option_code=offer_provider_code)
        if state is None or state["source_hash"] != source_hash:
            needed = list(target_languages)
        else:
            already_done = set(state["translated_languages"])
            needed = [lang for lang in target_languages if lang not in already_done]
        # Verify
        truly_needed = []
        languages_to_add = []
        for lang in needed:
            existing = get_existing_offer_content_for_language(offer_entry, lang)
            if not existing:
                truly_needed.append(lang)
                continue
            is_identical = True
            for field, src in translatable.items():
                if existing.get(field) != src:
                    is_identical = False
                    break
            if is_identical:
                truly_needed.append(lang)
            else:
                languages_to_add.append(lang)
        if languages_to_add:
            prior_state = store.get_state("hotel_offer", supplier_id, entity_id, option_code=offer_provider_code)
            prior_langs = prior_state["translated_languages"] if prior_state and prior_state["source_hash"] == source_hash else []
            all_langs = sorted(set(prior_langs) | set(languages_to_add))
            store.upsert_state("hotel_offer", supplier_id, entity_id, source_hash, all_langs, option_code=offer_provider_code)
        needed = truly_needed

    # Self-healing
    if not needed:
        sample_lang = "FR" if "FR" in target_languages else target_languages[0] if target_languages else "EN"
        existing = get_existing_offer_content_for_language(offer_entry, sample_lang)
        if existing:
            is_identical = True
            for field, src in translatable.items():
                if existing.get(field) != src:
                    is_identical = False
                    break
            if is_identical:
                print(f"🔍 Offer verification: {sample_lang} identical. Re-translating.")
                needed = list(target_languages)
            else:
                return {"status": "up_to_date", "offer_code": offer_provider_code}
        else:
            print(f"🔍 Offer verification: {sample_lang} missing. Re-translating.")
            needed = list(target_languages)

    if not needed:
        return {"status": "up_to_date", "offer_code": offer_provider_code}

    # Translate
    compressed = compress_translatable_fields(translatable)
    combined, failed_languages = translate_in_batches(translator, compressed, needed, batch_size=BATCH_SIZE)

    # See the main-hotel translate call above: identical-to-source is not a
    # failure signal on its own — only translate_in_batches-reported
    # failures get dropped.
    successful = {}
    for lang, trans in combined.items():
        if lang in failed_languages:
            print(f"⚠️  Offer translation batch for {lang} failed; skipping.")
        else:
            successful[lang] = trans

    if not successful:
        return {"status": "skipped", "offer_code": offer_provider_code, "reason": "no successful translations"}

    updated_offer = build_updated_offer(offer_entry, successful)
    return {
        "status": "updated",
        "offer_code": offer_provider_code,
        "updated_offer": updated_offer,
        "languages_written": list(successful.keys())
    }


# ---------- Fetch and sync all hotels ----------
def fetch_all_hotels(api, supplier_id: str, limit: int = None) -> List[Dict[str, Any]]:
    data = api.get_hotels(supplier_id)
    hotels = data.get("hotel", []) if isinstance(data, dict) else []
    if limit:
        hotels = hotels[:limit]
    return hotels


def sync_all_hotels_for_supplier(
    api,
    translator,
    store: StateStore,
    supplier_id: str,
    target_languages: List[str],
    dry_run: bool = True,
    limit: int = None,
    force: bool = False,
) -> List[Dict[str, Any]]:
    hotels = fetch_all_hotels(api, supplier_id, limit=limit)
    print(f"📋 Found {len(hotels)} hotel(s) for supplier {supplier_id}.")

    results = []
    for h in hotels:
        provider_code = h.get("providerCode")
        if not provider_code:
            results.append({"status": "skipped", "reason": "no providerCode", "raw": h})
            continue
        full_hotel = api.get_hotel(supplier_id, provider_code)
        if isinstance(full_hotel, dict) and "error" in full_hotel:
            results.append({"status": "fetch_failed", "provider_code": provider_code, "detail": full_hotel})
            continue
        hotel_result = sync_hotel(api, translator, store, supplier_id, full_hotel, target_languages,
                                  dry_run=dry_run, force=force)
        results.append(hotel_result)
    return results
