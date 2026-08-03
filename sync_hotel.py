"""
sync_hotel.py — Sync hotels (main + rooms + supplements) with translations.
"""

import json
import re
import time
from typing import Dict, Any, List, Optional
from concurrent.futures import ThreadPoolExecutor, as_completed

from state_store import StateStore, compute_hash
from translator import get_translator

# ---- Configuration ----
BATCH_SIZE = 10
DELAY_BETWEEN_BATCHES = 2
MAX_WORKERS = 5

# ---- Translatable fields ----
HOTEL_TEXT_FIELDS = ("hotelname", "description")
ROOM_TEXT_FIELDS = ("name",)
SUPPLEMENT_TEXT_FIELDS = ("description",)


def strip_html_and_compress(text: str) -> str:
    if not text:
        return text
    text = re.sub(r'<[^>]+>', ' ', text)
    text = re.sub(r'\s+', ' ', text)
    return text.strip()


def compress_translatable_fields(fields: Dict[str, str]) -> Dict[str, str]:
    compressed = {}
    for key, value in fields.items():
        if isinstance(value, str):
            compressed[key] = strip_html_and_compress(value)
        else:
            compressed[key] = value
    return compressed


# ---- Main hotel extraction ----
def extract_translatable_fields_from_hotel(hotel_entry: Dict[str, Any]) -> Dict[str, str]:
    """Extract hotelname and EN description from descriptions array."""
    fields = {}
    # Hotel name (top-level)
    hotelname = hotel_entry.get("hotelname")
    if isinstance(hotelname, str) and hotelname.strip():
        fields["hotelname"] = hotelname

    # Description from descriptions array (EN)
    descriptions = hotel_entry.get("descriptions", [])
    for desc in descriptions:
        if desc.get("language") == "EN" and desc.get("description"):
            fields["description"] = desc["description"]
            break
    return fields


def get_existing_hotel_content_for_language(hotel_entry: Dict[str, Any], lang: str) -> Dict[str, str]:
    """Get existing name/description for a specific language from descriptions array."""
    result = {}
    descriptions = hotel_entry.get("descriptions", [])
    for desc in descriptions:
        if desc.get("language") == lang:
            if "name" in desc and desc["name"]:
                result["hotelname"] = desc["name"]
            if "description" in desc and desc["description"]:
                result["description"] = desc["description"]
            break
    # If no entry found, return empty (will be treated as missing)
    return result


def build_updated_hotel_descriptions(
    original_descriptions: List[Dict[str, Any]],
    translations_by_lang: Dict[str, Dict[str, str]],
    en_name: str,
    en_desc: str,
) -> List[Dict[str, Any]]:
    """Merge translations into descriptions array."""
    # Start with a copy of original descriptions
    new_descriptions = [dict(d) for d in original_descriptions]

    # Keep track of which languages we already have
    existing_langs = {d.get("language") for d in new_descriptions}

    for lang, trans in translations_by_lang.items():
        if lang in existing_langs:
            # Update existing entry
            for d in new_descriptions:
                if d.get("language") == lang:
                    if "hotelname" in trans:
                        d["name"] = trans["hotelname"]
                    if "description" in trans:
                        d["description"] = trans["description"]
                    break
        else:
            # Create new entry
            new_entry = {"language": lang}
            if "hotelname" in trans:
                new_entry["name"] = trans["hotelname"]
            if "description" in trans:
                new_entry["description"] = trans["description"]
            new_descriptions.append(new_entry)

    return new_descriptions


# ---- Rooms extraction ----
def extract_translatable_fields_from_room(room_entry: Dict[str, Any]) -> Dict[str, str]:
    fields = {}
    name = room_entry.get("name")
    if isinstance(name, str) and name.strip():
        fields["name"] = name
    # Also check if there is a description field (not in sample, but for completeness)
    description = room_entry.get("description")
    if isinstance(description, str) and description.strip():
        fields["description"] = description
    return fields


def get_existing_room_content_for_language(room_entry: Dict[str, Any], lang: str) -> Dict[str, str]:
    """Get existing translations from room's translations or datasheets."""
    # Check translations
    translations = room_entry.get("translations", {})
    lang_entry = translations.get(lang)
    if isinstance(lang_entry, dict):
        fields = {}
        for f in ROOM_TEXT_FIELDS:
            val = lang_entry.get(f)
            if isinstance(val, str) and val.strip():
                fields[f] = val
        return fields
    # Also check datasheets (if any)
    datasheets = room_entry.get("datasheets", {})
    lang_entry = datasheets.get(lang)
    if isinstance(lang_entry, dict):
        fields = {}
        for f in ROOM_TEXT_FIELDS:
            val = lang_entry.get(f)
            if isinstance(val, str) and val.strip():
                fields[f] = val
        return fields
    return {}


def build_updated_room(original_room: Dict[str, Any],
                       translations_by_lang: Dict[str, Dict[str, str]]) -> Dict[str, Any]:
    """Merge translations into room (create/update translations field)."""
    new_room = dict(original_room)

    # Determine target key: prefer 'translations', else 'datasheets', else create 'translations'
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
        base = dict(target_map.get(lang, {}))
        for field, text in trans.items():
            base[field] = text
        target_map[lang] = base

    new_room[target_key] = target_map
    return new_room


# ---- Supplements extraction ----
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
    """Merge translations into supplement's names array."""
    new_supp = dict(original_supp)
    # Get existing names list
    existing_names = [dict(n) for n in new_supp.get("names", [])]
    existing_langs = {n.get("language") for n in existing_names}

    for lang, trans in translations_by_lang.items():
        if lang in existing_langs:
            # Update existing entry
            for n in existing_names:
                if n.get("language") == lang:
                    if "description" in trans:
                        n["description"] = trans["description"]
                    break
        else:
            # Create new entry
            new_entry = {"language": lang}
            if "description" in trans:
                new_entry["description"] = trans["description"]
            existing_names.append(new_entry)

    new_supp["names"] = existing_names
    return new_supp


# ---- Verification and filtering ----
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
        # We need a function to get existing content for the given entity type.
        # For main hotel, we have get_existing_hotel_content_for_language.
        # For rooms, get_existing_room_content_for_language.
        # For supplements, get_existing_supplement_content_for_language.
        # Since this function is generic, we'll pass the extraction function as a parameter.
        # For simplicity, we'll handle within the specific sync functions.
        pass  # We'll implement verification in each sync function directly.

    # This generic function is not used; we'll write specific verification per entity.
    return needed


# ---- Main hotel sync ----
def sync_hotel_from_data(
    api,
    translator,
    store: StateStore,
    supplier_id: str,
    hotel_entry: Dict[str, Any],
    target_languages: List[str],
    dry_run: bool = True,
    force: bool = False,
) -> Dict[str, Any]:
    start_time = time.time()
    contract_id = hotel_entry.get("contractId")
    if not contract_id:
        return {"status": "skipped", "reason": "no 'contractId' field"}

    provider_code = hotel_entry.get("providerCode")
    if not provider_code:
        return {"status": "skipped", "contract_id": contract_id, "reason": "no providerCode"}

    # ---- Main hotel ----
    translatable = extract_translatable_fields_from_hotel(hotel_entry)
    if not translatable:
        return {"status": "skipped", "contract_id": contract_id, "reason": "no translatable fields"}

    source_hash = compute_hash(translatable)
    t0 = time.time()
    if force:
        needed = list(target_languages)
    else:
        # Check state for main hotel
        state = store.get_state("hotel", supplier_id, contract_id)
        if state is None or state["source_hash"] != source_hash:
            needed = list(target_languages)
        else:
            already_done = set(state["translated_languages"])
            needed = [lang for lang in target_languages if lang not in already_done]
        # Verify existing content for needed languages (re-check)
        truly_needed = []
        languages_to_add_to_state = []
        for lang in needed:
            existing = get_existing_hotel_content_for_language(hotel_entry, lang)
            if not existing:
                truly_needed.append(lang)
                continue
            # Check if any field differs from source
            is_identical = True
            for field, src_text in translatable.items():
                if existing.get(field) != src_text:
                    is_identical = False
                    break
            if is_identical:
                truly_needed.append(lang)
            else:
                languages_to_add_to_state.append(lang)
        # Update state for languages already correctly translated
        if languages_to_add_to_state:
            prior_state = store.get_state("hotel", supplier_id, contract_id)
            prior_langs = prior_state["translated_languages"] if prior_state and prior_state["source_hash"] == source_hash else []
            all_langs = sorted(set(prior_langs) | set(languages_to_add_to_state))
            store.upsert_state("hotel", supplier_id, contract_id, source_hash, all_langs)
        needed = truly_needed

    verify_time = time.time() - t0

    # Self-healing: if state says all done, verify a sample language
    if not needed:
        sample_lang = "FR" if "FR" in target_languages else target_languages[0] if target_languages else "EN"
        existing = get_existing_hotel_content_for_language(hotel_entry, sample_lang)
        if existing:
            is_identical = True
            for field, src_text in translatable.items():
                if existing.get(field) != src_text:
                    is_identical = False
                    break
            if is_identical:
                print(f"🔍 Verification: {sample_lang} content is identical to source or missing. Forcing re-translation.")
                needed = list(target_languages)
            else:
                # Content differs – up_to_date
                return {"status": "up_to_date", "contract_id": contract_id, "entity": "main"}
        else:
            print(f"🔍 Verification: {sample_lang} has no content. Forcing re-translation.")
            needed = list(target_languages)

    if not needed:
        return {"status": "up_to_date", "contract_id": contract_id, "entity": "main"}

    # Translate main hotel
    compressed = compress_translatable_fields(translatable)
    combined = {}
    total_batches = (len(needed) + BATCH_SIZE - 1) // BATCH_SIZE
    translation_start = time.time()
    for i in range(0, len(needed), BATCH_SIZE):
        batch = needed[i:i+BATCH_SIZE]
        batch_num = i//BATCH_SIZE + 1
        print(f"   Hotel batch {batch_num}/{total_batches}: {batch}")
        batch_result = translator.translate_fields(compressed, batch)
        combined.update(batch_result)
        if i + BATCH_SIZE < len(needed):
            time.sleep(DELAY_BETWEEN_BATCHES)
    translation_time = time.time() - translation_start

    successful = {}
    for lang, trans in combined.items():
        changed = False
        for field, src in translatable.items():
            if trans.get(field) != src:
                changed = True
                break
        if changed:
            successful[lang] = trans
        else:
            print(f"⚠️  Translation for {lang} identical to source; skipping.")

    if successful:
        en_name = translatable.get("hotelname", "")
        en_desc = translatable.get("description", "")
        new_descriptions = build_updated_hotel_descriptions(
            hotel_entry.get("descriptions", []),
            successful,
            en_name,
            en_desc
        )
        # Build payload
        payload = dict(hotel_entry)
        payload["descriptions"] = new_descriptions
        # Also update top-level hotelname? Maybe keep as is.
        # We'll keep the original hotelname (English) because it's used as identifier.
        # The translated name will be in descriptions.

        if dry_run:
            preview = {lang: {k: v for k, v in trans.items() if k in HOTEL_TEXT_FIELDS}
                       for lang, trans in successful.items()}
            return {"status": "dry_run_preview", "contract_id": contract_id,
                    "languages": list(successful.keys()), "preview": preview}

        write_start = time.time()
        result = api.update_hotel(supplier_id, payload)
        write_time = time.time() - write_start
        if isinstance(result, dict) and "error" in result:
            return {"status": "put_failed", "contract_id": contract_id, "detail": result}

        # Update state
        written_langs = list(successful.keys())
        prior_state = store.get_state("hotel", supplier_id, contract_id)
        prior_langs = prior_state["translated_languages"] if prior_state and prior_state["source_hash"] == source_hash else []
        all_langs = sorted(set(prior_langs) | set(written_langs))
        store.upsert_state("hotel", supplier_id, contract_id, source_hash, all_langs)
        total_time = time.time() - start_time
        print(f"✅ Hotel {contract_id} main done in {total_time:.1f}s")
        return {"status": "updated", "contract_id": contract_id, "entity": "main", "languages_written": written_langs}
    else:
        return {"status": "skipped", "contract_id": contract_id, "entity": "main", "reason": "no successful translations"}


# ---- Room sync ----
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
    t0 = time.time()
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

    if not needed:
        # Self-healing check
        sample_lang = "FR" if "FR" in target_languages else target_languages[0] if target_languages else "EN"
        existing = get_existing_room_content_for_language(room_entry, sample_lang)
        if existing:
            is_identical = True
            for field, src in translatable.items():
                if existing.get(field) != src:
                    is_identical = False
                    break
            if is_identical:
                print(f"🔍 Room verification: {sample_lang} identical to source. Re-translating.")
                needed = list(target_languages)
            else:
                return {"status": "up_to_date", "room_code": room_provider_code}
        else:
            print(f"🔍 Room verification: {sample_lang} missing. Re-translating.")
            needed = list(target_languages)

    if not needed:
        return {"status": "up_to_date", "room_code": room_provider_code}

    compressed = compress_translatable_fields(translatable)
    combined = {}
    total_batches = (len(needed) + BATCH_SIZE - 1) // BATCH_SIZE
    for i in range(0, len(needed), BATCH_SIZE):
        batch = needed[i:i+BATCH_SIZE]
        print(f"   Room {room_provider_code} batch {i//BATCH_SIZE+1}/{total_batches}: {batch}")
        batch_result = translator.translate_fields(compressed, batch)
        combined.update(batch_result)
        if i + BATCH_SIZE < len(needed):
            time.sleep(DELAY_BETWEEN_BATCHES)

    successful = {}
    for lang, trans in combined.items():
        changed = False
        for field, src in translatable.items():
            if trans.get(field) != src:
                changed = True
                break
        if changed:
            successful[lang] = trans
        else:
            print(f"⚠️  Room translation for {lang} identical to source; skipping.")

    if not successful:
        return {"status": "skipped", "room_code": room_provider_code, "reason": "no successful translations"}

    updated_room = build_updated_room(room_entry, successful)

    # We need to update the hotel entry with the modified room
    # We'll find the room in the hotel's rooms list and replace it
    # We'll do this in the caller (sync_hotel_with_children) to avoid multiple PUTs

    # For now, return the updated room and the translations info.
    return {
        "status": "updated",
        "room_code": room_provider_code,
        "updated_room": updated_room,
        "languages_written": list(successful.keys())
    }


# ---- Supplement sync ----
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

    compressed = compress_translatable_fields(translatable)
    combined = {}
    total_batches = (len(needed) + BATCH_SIZE - 1) // BATCH_SIZE
    for i in range(0, len(needed), BATCH_SIZE):
        batch = needed[i:i+BATCH_SIZE]
        print(f"   Supplement {supp_provider_code} batch {i//BATCH_SIZE+1}/{total_batches}: {batch}")
        batch_result = translator.translate_fields(compressed, batch)
        combined.update(batch_result)
        if i + BATCH_SIZE < len(needed):
            time.sleep(DELAY_BETWEEN_BATCHES)

    successful = {}
    for lang, trans in combined.items():
        changed = False
        for field, src in translatable.items():
            if trans.get(field) != src:
                changed = True
                break
        if changed:
            successful[lang] = trans
        else:
            print(f"⚠️  Supplement translation for {lang} identical; skipping.")

    if not successful:
        return {"status": "skipped", "supplement_code": supp_provider_code, "reason": "no successful translations"}

    updated_supp = build_updated_supplement(supp_entry, successful)
    return {
        "status": "updated",
        "supplement_code": supp_provider_code,
        "updated_supplement": updated_supp,
        "languages_written": list(successful.keys())
    }


# ---- Main sync for all hotels ----
def sync_hotel(api, translator, store: StateStore,
               supplier_id: str, hotel_entry: Dict[str, Any],
               target_languages: List[str],
               dry_run: bool = True, force: bool = False) -> Dict[str, Any]:
    """
    Sync a hotel: main, rooms, supplements. Returns result with status for each.
    """
    # Sync main hotel (updates descriptions)
    main_result = sync_hotel_from_data(api, translator, store, supplier_id, hotel_entry,
                                       target_languages, dry_run=dry_run, force=force)
    results = {"main": main_result, "rooms": [], "supplements": []}

    # Sync rooms
    rooms = hotel_entry.get("rooms", [])
    if rooms:
        room_results = []
        for idx, room in enumerate(rooms):
            room_result = sync_room(api, translator, store, supplier_id, hotel_entry, room,
                                    target_languages, dry_run=dry_run, force=force)
            room_results.append(room_result)
            # We need to collect updated rooms to send in a single PUT.
            # We'll collect them in a list and update the hotel payload after all translations.
        # We'll handle merging later. For now, we just report.
        results["rooms"] = room_results

    # Sync supplements
    supplements = hotel_entry.get("supplements", [])
    if supplements:
        supp_results = []
        for supp in supplements:
            supp_result = sync_supplement(api, translator, store, supplier_id, hotel_entry, supp,
                                          target_languages, dry_run=dry_run, force=force)
            supp_results.append(supp_result)
        results["supplements"] = supp_results

    # Now, if any room or supplement was updated, we need to rebuild the hotel payload
    # with the updated rooms and supplements and send a PUT.
    # We'll check if any room or supplement has "updated" status.
    any_updated = False
    updated_hotel = dict(hotel_entry)  # start with original

    # Rebuild rooms
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

    # Rebuild supplements
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

    if any_updated and not dry_run:
        # Send PUT with updated hotel
        result = api.update_hotel(supplier_id, updated_hotel)
        if isinstance(result, dict) and "error" in result:
            # Mark put_failed for rooms/supps
            for r in room_results:
                if r.get("status") == "updated":
                    r["status"] = "put_failed"
                    r["detail"] = result
            for s in supp_results:
                if s.get("status") == "updated":
                    s["status"] = "put_failed"
                    s["detail"] = result
            results["rooms"] = room_results
            results["supplements"] = supp_results
        else:
            # Update state for rooms and supplements that were written
            for r in room_results:
                if r.get("status") == "updated" and "languages_written" in r:
                    # Update state for room
                    room_provider_code = r.get("room_code")
                    entity_id = f"{hotel_entry.get('contractId')}|room|{room_provider_code}"
                    prior_state = store.get_state("hotel_room", supplier_id, entity_id, option_code=room_provider_code)
                    prior_langs = prior_state["translated_languages"] if prior_state else []
                    all_langs = sorted(set(prior_langs) | set(r["languages_written"]))
                    # We don't have source_hash here; we assume it was updated earlier.
                    # We'll just update with the same source_hash by re-fetching? Better to store hash in the result.
                    # We'll add source_hash to room_result for simplicity, but we'll trust the existing state.
                    # Actually, the state was updated in sync_room already if it was successful.
                    # But we only updated state for the verification step, not for the final write.
                    # We'll update state here.
                    # We'll use the source_hash from the room translation.
                    # We need to compute it again.
                    translatable_room = extract_translatable_fields_from_room(room)
                    room_source_hash = compute_hash(translatable_room)
                    store.upsert_state("hotel_room", supplier_id, entity_id, room_source_hash, all_langs, option_code=room_provider_code)
            for s in supp_results:
                if s.get("status") == "updated" and "languages_written" in s:
                    supp_provider_code = s.get("supplement_code")
                    entity_id = f"{hotel_entry.get('contractId')}|supplement|{supp_provider_code}"
                    translatable_supp = extract_translatable_fields_from_supplement(supp_entry)
                    supp_source_hash = compute_hash(translatable_supp)
                    prior_state = store.get_state("hotel_supplement", supplier_id, entity_id, option_code=supp_provider_code)
                    prior_langs = prior_state["translated_languages"] if prior_state else []
                    all_langs = sorted(set(prior_langs) | set(s["languages_written"]))
                    store.upsert_state("hotel_supplement", supplier_id, entity_id, supp_source_hash, all_langs, option_code=supp_provider_code)

    return results


# ---- Fetch all hotels ----
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
        # For each hotel, we need the full details (GET /hotel/{supplierId}/{providerCode})
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
