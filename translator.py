"""
translator.py — AI translation engine with fallback (Gemini first, then Claude).

CONFIRMED BUG FIX (verified directly against the installed google-genai SDK,
same way the earlier response_format bug was caught): the GeminiTranslator
config previously included a top-level "timeout": 60 key. That is NOT a
valid field on google.genai.types.GenerateContentConfig — instantiating it
raises pydantic.ValidationError: "Extra inputs are not permitted
[type=extra_forbidden]". This meant EVERY Gemini call was failing
immediately, on every entity type (Holiday Packages, Tickets, Transfers,
Transports, Hotels). Under TRANSLATION_PROVIDER=fallback, this silently
fell back to Claude every time (i.e. paying full Claude Haiku prices, not
the cheaper Gemini price this switch was meant to get, plus ~31s of wasted
retry/backoff sleep per batch). Under TRANSLATION_PROVIDER=gemini (no
fallback), every sync call would hard-fail.

Fix: a client-side request timeout, if wanted, goes under
"http_options": {"timeout": <milliseconds>} — NOT a bare "timeout" key.
We're not setting one at all here (removing it entirely) since the
retries/backoff loop already handles slow/hanging calls, and an
overly-aggressive client timeout on a legitimately-slow big-batch call
would just trigger more retries.

Also restored max_output_tokens to 32768 (was reduced to 8192) — Tickets
have more fields per language (name, description, meetingPoint,
activityType, voucherRemarks, departureTime, includes, excludes) than
Holiday Packages (title, description, ribbonText), so a 10-language batch
risked truncating mid-JSON at 8192.
"""

import os
import json
import time
from typing import Dict, List
from concurrent.futures import ThreadPoolExecutor, as_completed

from dotenv import load_dotenv

load_dotenv()

# Default models
DEFAULT_MODEL = "claude-haiku-4-5-20251001"
DEFAULT_GEMINI_MODEL = "gemini-2.5-flash"

TRANSLATION_TOOL = {
    "name": "submit_translations",
    "description": "Submit the translated text for every requested field, in every requested target language.",
    "input_schema": {
        "type": "object",
        "properties": {
            "translations": {
                "type": "object",
                "description": (
                    "Map of target language code -> object mapping each requested field name "
                    "to its translated text. Every requested language AND every requested field "
                    "must be present, even if you have to leave a field's translation identical "
                    "to the source when it's untranslatable (e.g. a product code)."
                ),
                "additionalProperties": {
                    "type": "object",
                    "additionalProperties": {"type": "string"}
                }
            }
        },
        "required": ["translations"]
    }
}

SYSTEM_PROMPT = """You are a professional travel-industry translator for Momira Travel.

Rules you must always follow:
- Domain preservation: keep travel-industry terms idiomatic in the target language
  ("airport transfer", "half-board", "meeting point", "pickup location") rather than
  literal word-for-word substitutes. Never translate product codes, IDs, or supplier
  identifiers that appear inside the text — meaning literal internal reference codes
  such as "TNR-03", "DPS-1", or "Code1", NOT tour/package titles. A field named
  "title" or "largeTitle" (e.g. "8 Days Bali Complete", "Getting to Know Madagascar's
  People and Lemurs - 14 Days") is real customer-facing marketing copy and MUST be
  translated in full — translate every translatable word (numbers-as-words like
  "Days"/"Tage"/"Jours", connecting words, adjectives like "Complete"/"Classic"/
  "Highlights") while only leaving genuine proper nouns (destination/place names,
  brand names like "Mövenpick") unchanged. Do not skip or leave a title untranslated
  just because it is short or contains place names — a title consisting only of a
  place name plus ordinary descriptive words is not a product code.
- Formatting integrity: preserve HTML tags (<b>, <br>, etc.), Markdown, and template
  variables (e.g. {duration}, {pickupTime}) EXACTLY as they appear, untouched, in the
  same position.
- Tone: professional, inviting, conversion-oriented — the register a travel consumer
  expects in that market, not a stiff literal translation.
- Locale-variant awareness: PT vs PT_BR (European vs Brazilian Portuguese) must reflect
  genuine regional differences in spelling and idiom, not be copies of each other.
- If a field's source text is empty or whitespace-only, return it unchanged (empty) —
  do not invent content.

You must respond ONLY by calling the submit_translations tool. Do not write any other text."""


class TranslationError(Exception):
    pass


class ProviderRateLimitError(Exception):
    pass


USER_PROMPT_TEMPLATE = (
    "Translate the following fields from English into these target languages: "
    "{languages}.\n\n"
    "Fields (JSON):\n{fields_json}\n\n"
    "Respond with one entry per target language, each containing every field "
    "listed above translated into that language."
)


class ClaudeTranslator:
    def __init__(self, api_key: str = None, model: str = None):
        from anthropic import Anthropic
        self.client = Anthropic(api_key=api_key or os.getenv("ANTHROPIC_API_KEY"))
        self.model = model or os.getenv("ANTHROPIC_MODEL", DEFAULT_MODEL)

    def translate_fields(
        self,
        source_fields: Dict[str, str],
        target_languages: List[str],
        retries: int = 5,
    ) -> Dict[str, Dict[str, str]]:
        non_empty_fields = {k: v for k, v in source_fields.items() if isinstance(v, str) and v.strip()}
        if not non_empty_fields:
            return {lang: dict(source_fields) for lang in target_languages}

        prompt = (
            f"Translate the following fields from English into these target languages: "
            f"{', '.join(target_languages)}.\n\n"
            f"Fields (JSON):\n{json.dumps(non_empty_fields, ensure_ascii=False, indent=2)}\n\n"
            f"Call submit_translations with one entry per target language, each containing "
            f"every field listed above translated into that language."
        )

        raw_translations = {}
        for attempt in range(retries + 1):
            try:
                response = self.client.messages.create(
                    model=self.model,
                    # CONFIRMED via Anthropic's docs (platform.claude.com):
                    # Claude Haiku 4.5 supports up to 64k output tokens via
                    # the standard Messages API, no beta header needed. The
                    # old value here (8192) was ~8x too small for anything
                    # with a long description (e.g. a multi-day Closed Tour
                    # itinerary translated across several languages in one
                    # batch) — the response got cut off mid-generation,
                    # failed to parse, and retried up to 6 times per batch,
                    # each attempt generating (and billing for) a large
                    # truncated response before giving up. That is almost
                    # certainly what caused the runaway cost/time on the
                    # first live Closed Tour test.
                    max_tokens=64000,
                    system=SYSTEM_PROMPT,
                    tools=[TRANSLATION_TOOL],
                    tool_choice={"type": "tool", "name": "submit_translations"},
                    messages=[{"role": "user", "content": prompt}],
                    timeout=120,
                )
                tool_use = next((b for b in response.content if b.type == "tool_use"), None)
                if not tool_use:
                    raise TranslationError("Model did not call submit_translations")
                raw_translations = tool_use.input.get("translations", {})
                break
            except Exception as e:
                is_rate_limit = False
                if hasattr(e, 'response') and hasattr(e.response, 'status_code'):
                    is_rate_limit = e.response.status_code == 429
                if not is_rate_limit and '429' in str(e):
                    is_rate_limit = True

                if is_rate_limit:
                    wait = (2 ** attempt) * 5
                    print(f"🚦 Claude rate limit hit (attempt {attempt+1}). Waiting {wait}s before retry...")
                    time.sleep(wait)
                else:
                    print(f"⚠️  Claude call failed (attempt {attempt+1}/{retries+1}): {e}")
                    if attempt < retries:
                        time.sleep(2 ** attempt)

                if attempt == retries:
                    raise ProviderRateLimitError(f"Claude rate limit exhausted after {retries+1} attempts")

        # Build result, filling gaps
        result = {}
        for lang in target_languages:
            lang_result = {}
            lang_data = raw_translations.get(lang, {}) if isinstance(raw_translations, dict) else {}
            for field, source_value in source_fields.items():
                translated = lang_data.get(field)
                if not translated or not str(translated).strip():
                    if field in non_empty_fields:
                        print(f"⚠️  Missing/empty translation for '{field}' -> {lang}; falling back to English source.")
                    translated = source_value
                lang_result[field] = translated
            result[lang] = lang_result
        return result


def _build_gemini_response_schema(fields: Dict[str, str], target_languages: List[str]) -> dict:
    field_names = list(fields.keys())
    per_language_schema = {
        "type": "object",
        "properties": {name: {"type": "string"} for name in field_names},
        "required": field_names,
    }
    return {
        "type": "object",
        "properties": {
            "translations": {
                "type": "object",
                "properties": {lang: per_language_schema for lang in target_languages},
                "required": target_languages,
            }
        },
        "required": ["translations"],
    }


class GeminiTranslator:
    def __init__(self, api_key: str = None, model: str = None):
        from google import genai
        self.client = genai.Client(api_key=api_key or os.getenv("GEMINI_API_KEY"))
        self.model = model or os.getenv("GEMINI_MODEL", DEFAULT_GEMINI_MODEL)

    def translate_fields(
        self,
        source_fields: Dict[str, str],
        target_languages: List[str],
        retries: int = 5,
    ) -> Dict[str, Dict[str, str]]:
        non_empty_fields = {k: v for k, v in source_fields.items() if isinstance(v, str) and v.strip()}
        if not non_empty_fields:
            return {lang: dict(source_fields) for lang in target_languages}

        schema = _build_gemini_response_schema(non_empty_fields, target_languages)
        prompt = USER_PROMPT_TEMPLATE.format(
            languages=", ".join(target_languages),
            fields_json=json.dumps(non_empty_fields, ensure_ascii=False, indent=2),
        )

        raw_translations = {}
        for attempt in range(retries + 1):
            try:
                response = self.client.models.generate_content(
                    model=self.model,
                    contents=prompt,
                    config={
                        "system_instruction": SYSTEM_PROMPT,
                        "response_mime_type": "application/json",
                        "response_json_schema": schema,
                        # 0 = disabled. Flash's "thinking" mode adds real
                        # latency for a task this simple — we don't need
                        # reasoning for translation, just fast structured
                        # output.
                        "thinking_config": {"thinking_budget": 0},
                        # Generous cap so a big multi-field batch (e.g.
                        # Tickets: name/description/meetingPoint/
                        # activityType/voucherRemarks/departureTime/
                        # includes/excludes across up to 10 languages at
                        # once) can't get silently truncated mid-JSON.
                        "max_output_tokens": 32768,
                        # NOTE: a client-side request timeout, if wanted,
                        # goes under http_options (milliseconds) — NOT a
                        # bare "timeout" key. A bare "timeout" key is not a
                        # valid GenerateContentConfig field and raises a
                        # pydantic ValidationError on every single call
                        # (confirmed against the installed SDK) — this was
                        # silently breaking every Gemini call and forcing a
                        # fallback to Claude (or a hard failure with no
                        # fallback configured). Deliberately omitted here;
                        # add "http_options": {"timeout": 60000} if a
                        # client-side timeout is genuinely needed.
                    },
                )
                parsed = json.loads(response.text)
                raw_translations = parsed.get("translations", {})
                if not raw_translations:
                    raise TranslationError("Model returned no 'translations' key")
                break
            except Exception as e:
                is_rate_limit = False
                if hasattr(e, 'response') and hasattr(e.response, 'status_code'):
                    is_rate_limit = e.response.status_code == 429
                if not is_rate_limit and ('429' in str(e) or 'RESOURCE_EXHAUSTED' in str(e)):
                    is_rate_limit = True

                if is_rate_limit:
                    wait = (2 ** attempt) * 5
                    print(f"🚦 Gemini rate limit hit (attempt {attempt+1}). Waiting {wait}s before retry...")
                    time.sleep(wait)
                else:
                    print(f"⚠️  Gemini call failed (attempt {attempt+1}/{retries+1}): {e}")
                    if attempt < retries:
                        time.sleep(2 ** attempt)

                if attempt == retries:
                    raise ProviderRateLimitError(f"Gemini rate limit exhausted after {retries+1} attempts")

        result = {}
        for lang in target_languages:
            lang_result = {}
            lang_data = raw_translations.get(lang, {}) if isinstance(raw_translations, dict) else {}
            for field, source_value in source_fields.items():
                translated = lang_data.get(field)
                if not translated or not str(translated).strip():
                    if field in non_empty_fields:
                        print(f"⚠️  Missing/empty translation for '{field}' -> {lang}; falling back to English source.")
                    translated = source_value
                lang_result[field] = translated
            result[lang] = lang_result
        return result


class FallbackTranslator:
    def __init__(self, primary: GeminiTranslator, fallback: ClaudeTranslator):
        self.primary = primary
        self.fallback = fallback

    def translate_fields(
        self,
        source_fields: Dict[str, str],
        target_languages: List[str],
        retries: int = 5,
    ) -> Dict[str, Dict[str, str]]:
        try:
            result = self.primary.translate_fields(source_fields, target_languages, retries)
            # Check if all languages are still English (no changes)
            all_english = True
            for lang in target_languages:
                for field, src in source_fields.items():
                    if result.get(lang, {}).get(field) != src:
                        all_english = False
                        break
                if not all_english:
                    break
            if all_english:
                print("⚠️  Primary provider returned only English fallback. Switching to Claude for this batch...")
                return self.fallback.translate_fields(source_fields, target_languages, retries)
            return result
        except (ProviderRateLimitError, Exception) as e:
            print(f"⚠️  Primary provider failed: {e}. Switching to Claude for this batch...")
            return self.fallback.translate_fields(source_fields, target_languages, retries)


def translate_in_batches(
    translator,
    fields: Dict[str, str],
    target_languages: List[str],
    batch_size: int = 10,
    max_workers: int = 4,
):
    """
    Speed fix for the translation step: every sync_*.py file used to split
    target_languages into batches of batch_size and translate them
    SEQUENTIALLY, sleeping 2 seconds between each batch for no functional
    reason (an artifact from early rate-limit caution). For a full
    30-language run at batch_size=10, that was 3 sequential API calls plus
    ~4s of pure dead-time sleep; at batch_size=5 (Holiday Packages), 6
    sequential calls plus ~10s of dead-time. Worse, a single batch raising
    an exception had no per-batch handling, so it could abort the ENTIRE
    sync for that item.

    This runs the batches CONCURRENTLY instead (bounded by max_workers, so
    we don't blast the provider with unlimited parallel requests), and
    isolates failures per-batch: if one batch's translate_fields() call
    raises, only that batch's languages fall back to English — every other
    concurrent batch still completes normally. Each provider's own
    retry/backoff/rate-limit handling (in GeminiTranslator/ClaudeTranslator/
    FallbackTranslator) is unchanged and still applies within each batch.

    Wall-clock time for a full run becomes roughly
    (number of batches / max_workers) * (single batch's own latency),
    instead of (number of batches) * (single batch's own latency + 2s).

    Returns (combined_translations, failed_languages):
      - combined_translations: Dict[lang -> {field: translated_text}], same
        as before.
      - failed_languages: set of languages whose BATCH ITSELF failed (the
        translate_fields() call raised after exhausting retries), and which
        therefore got a verbatim copy of the English source instead of a
        real translation.

    CONFIRMED live bug this fixes: every calling sync_*.py file used to
    decide "did this language actually translate?" by comparing the
    translated text to the source text field-by-field — if identical,
    it assumed the call had silently failed and dropped that language
    entirely (never written, never marked done). That heuristic breaks for
    short, commonly-borrowed words: a ticket modality literally named
    "Standard" legitimately translates to "Standard" in French, German,
    Polish, etc. (real value, not a fallback) — but the old "identical =
    failed" check couldn't tell that apart from a genuine failure and
    silently discarded it, every single run, forever. `failed_languages`
    gives calling code a real signal to filter on instead: a language is
    only unreliable if its batch actually raised, not merely because its
    correct translation happens to match the English source.
    """
    failed_languages = set()
    batches = [target_languages[i:i + batch_size] for i in range(0, len(target_languages), batch_size)]
    if len(batches) <= 1:
        # No concurrency needed/possible for a single batch.
        try:
            return translator.translate_fields(fields, target_languages), failed_languages
        except Exception as e:
            print(f"⚠️  Batch {target_languages} failed entirely: {e} — falling back to English for these languages.")
            failed_languages.update(target_languages)
            return {lang: dict(fields) for lang in target_languages}, failed_languages

    combined: Dict[str, Dict[str, str]] = {}
    with ThreadPoolExecutor(max_workers=min(max_workers, len(batches))) as executor:
        future_to_batch = {
            executor.submit(translator.translate_fields, fields, batch): batch
            for batch in batches
        }
        for future in as_completed(future_to_batch):
            batch = future_to_batch[future]
            try:
                result = future.result()
                combined.update(result)
            except Exception as e:
                print(f"⚠️  Batch {batch} failed entirely: {e} — falling back to English for these languages.")
                for lang in batch:
                    combined[lang] = dict(fields)
                    failed_languages.add(lang)
    return combined, failed_languages


def required_api_key_env_var() -> str:
    provider = (os.getenv("TRANSLATION_PROVIDER") or "gemini").strip().lower()
    if provider == "fallback":
        return "GEMINI_API_KEY"
    return "GEMINI_API_KEY" if provider == "gemini" else "ANTHROPIC_API_KEY"


def get_translator(api_key: str = None, model: str = None):
    provider = (os.getenv("TRANSLATION_PROVIDER") or "gemini").strip().lower()
    if provider == "gemini":
        return GeminiTranslator(api_key=api_key, model=model)
    elif provider == "claude":
        return ClaudeTranslator(api_key=api_key, model=model)
    elif provider == "fallback":
        gemini = GeminiTranslator(api_key=os.getenv("GEMINI_API_KEY"), model=model)
        claude = ClaudeTranslator(api_key=os.getenv("ANTHROPIC_API_KEY"), model=model)
        return FallbackTranslator(gemini, claude)
    else:
        raise ValueError(
            f"Unknown TRANSLATION_PROVIDER '{provider}' — expected 'gemini', 'claude', or 'fallback'."
        )
