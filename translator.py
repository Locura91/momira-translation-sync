"""
translator.py — AI translation engine for the nbext sync tool.

Two providers are implemented, both behind the SAME interface
(translate_fields(source_fields, target_languages) -> {lang: {field: text}}),
so sync_engine.py / sync_holiday_package.py never need to know or care
which one is active:

  - GeminiTranslator (Google Gemini 2.5 Flash) — DEFAULT, chosen for lowest
    cost per your comparison (~half the cost of Claude Haiku for this
    batched-per-entity translation job).
  - ClaudeTranslator (Claude Haiku via Anthropic) — kept working in case
    you want to switch back; same batching approach, slightly higher cost.

Both translate a whole entity's text fields into ALL requested target
languages in ONE API call (using each provider's structured-output /
JSON-schema feature to get back reliable JSON instead of parsing free
text) — this is what keeps cost down: 1 API call per entity instead of
1 call per entity per language.

Which one runs is controlled by the TRANSLATION_PROVIDER env var
("gemini" or "claude", default "gemini") — see .env.example.

Setup required before this file will run (Gemini, the default):
  pip install google-genai
  export GEMINI_API_KEY=...   (or put it in your .env file — see README
  "Step 1b — Get a Gemini API key" for where to get this)

Setup required for the Claude fallback:
  pip install anthropic
  export ANTHROPIC_API_KEY=sk-ant-...
"""

import os
import json
import time
from typing import Dict, List

from dotenv import load_dotenv

load_dotenv()

# Anthropic model used for translation, if TRANSLATION_PROVIDER=claude.
DEFAULT_MODEL = "claude-haiku-4-5-20251001"

# Gemini model used for translation, if TRANSLATION_PROVIDER=gemini (default).
# gemini-2.5-flash is the stable (non-preview) Flash model — deliberately not
# the newer "gemini-3-flash-preview"/"gemini-3.5-flash", since preview models
# can change/disappear without notice and this is a production glue script.
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
  identifiers that appear inside the text.
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


# Shared prompt text used by both providers so the actual translation
# instructions (domain preservation, tone, locale variants) don't drift
# apart if we ever tweak one and forget the other.
USER_PROMPT_TEMPLATE = (
    "Translate the following fields from English into these target languages: "
    "{languages}.\n\n"
    "Fields (JSON):\n{fields_json}\n\n"
    "Respond with one entry per target language, each containing every field "
    "listed above translated into that language."
)


class ClaudeTranslator:
    def __init__(self, api_key: str = None, model: str = None):
        from anthropic import Anthropic  # imported lazily so Gemini-only setups don't need this package
        self.client = Anthropic(api_key=api_key or os.getenv("ANTHROPIC_API_KEY"))
        self.model = model or os.getenv("ANTHROPIC_MODEL", DEFAULT_MODEL)

    def translate_fields(
        self,
        source_fields: Dict[str, str],
        target_languages: List[str],
        retries: int = 2,
    ) -> Dict[str, Dict[str, str]]:
        """
        source_fields: {"title": "Airport Transfer to Hotel", "description": "..."}
        target_languages: ["ES", "FR", "DE", ...]

        Returns: {"ES": {"title": "...", "description": "..."}, "FR": {...}, ...}

        On any failure (API error, missing language/field in the response), falls
        back to the English source text for the affected language/field and keeps
        going — this mirrors the design doc's fallback rule: never block the whole
        run over one bad translation, but never fabricate silently either (a warning
        is printed for every fallback used).
        """
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

        last_error = None
        for attempt in range(retries + 1):
            try:
                response = self.client.messages.create(
                    model=self.model,
                    max_tokens=8192,
                    system=SYSTEM_PROMPT,
                    tools=[TRANSLATION_TOOL],
                    tool_choice={"type": "tool", "name": "submit_translations"},
                    messages=[{"role": "user", "content": prompt}],
                )
                tool_use = next((b for b in response.content if b.type == "tool_use"), None)
                if not tool_use:
                    raise TranslationError("Model did not call submit_translations")
                raw_translations = tool_use.input.get("translations", {})
                break
            except Exception as e:
                last_error = e
                print(f"⚠️  Translation call failed (attempt {attempt + 1}/{retries + 1}): {e}")
                if attempt < retries:
                    time.sleep(2 ** attempt)
                else:
                    print(f"❌ Giving up after {retries + 1} attempts — falling back to English for ALL languages.")
                    raw_translations = {}

        # Build the final result, filling any gaps with the English source
        # (per-language AND per-field, so one bad language doesn't take down others).
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
    """
    Builds a JSON schema forcing Gemini to return exactly:
      {"translations": {"FR": {"title": "...", "description": "..."}, "DE": {...}, ...}}
    with every requested language AND every requested field required — same
    guarantee Claude's forced tool_choice gives us, via Gemini's structured
    JSON output feature instead.
    """
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
    """Same public interface as ClaudeTranslator — a drop-in replacement."""

    def __init__(self, api_key: str = None, model: str = None):
        from google import genai  # imported lazily so Claude-only setups don't need this package
        self.client = genai.Client(api_key=api_key or os.getenv("GEMINI_API_KEY"))
        self.model = model or os.getenv("GEMINI_MODEL", DEFAULT_GEMINI_MODEL)

    def translate_fields(
        self,
        source_fields: Dict[str, str],
        target_languages: List[str],
        retries: int = 2,
    ) -> Dict[str, Dict[str, str]]:
        non_empty_fields = {k: v for k, v in source_fields.items() if isinstance(v, str) and v.strip()}
        if not non_empty_fields:
            return {lang: dict(source_fields) for lang in target_languages}

        schema = _build_gemini_response_schema(non_empty_fields, target_languages)
        prompt = USER_PROMPT_TEMPLATE.format(
            languages=", ".join(target_languages),
            fields_json=json.dumps(non_empty_fields, ensure_ascii=False, indent=2),
        )

        last_error = None
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
                        # Flash's "thinking" mode adds real latency for a task
                        # this simple (a 2-minute round trip for two short
                        # languages was the "spinning a lot" issue) — turning
                        # it off is correct here, we don't need reasoning for
                        # translation, just fast structured output.
                        "thinking_config": {"thinking_budget": 0},
                        # Generous cap so a full 30-language batch (the real
                        # production run) can't get silently truncated mid-JSON.
                        "max_output_tokens": 8192,
                    },
                )
                parsed = json.loads(response.text)
                raw_translations = parsed.get("translations", {})
                if not raw_translations:
                    raise TranslationError("Model returned no 'translations' key")
                break
            except Exception as e:
                last_error = e
                print(f"⚠️  Translation call failed (attempt {attempt + 1}/{retries + 1}): {e}")
                if attempt < retries:
                    time.sleep(2 ** attempt)
                else:
                    print(f"❌ Giving up after {retries + 1} attempts — falling back to English for ALL languages.")
                    raw_translations = {}

        # Same per-language, per-field fallback logic as ClaudeTranslator, so
        # sync_engine.py / sync_holiday_package.py behave identically either way.
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


def required_api_key_env_var() -> str:
    """Which env var an entrypoint script should check for before starting, based on TRANSLATION_PROVIDER."""
    provider = (os.getenv("TRANSLATION_PROVIDER") or "gemini").strip().lower()
    return "GEMINI_API_KEY" if provider == "gemini" else "ANTHROPIC_API_KEY"


def get_translator(api_key: str = None, model: str = None):
    """
    Factory: returns whichever translator TRANSLATION_PROVIDER selects
    ("gemini" or "claude", default "gemini" per your cost comparison).
    Every run_sync*.py / streamlit_app.py entrypoint should call this
    instead of instantiating ClaudeTranslator/GeminiTranslator directly,
    so switching providers later is a one-line .env change.
    """
    provider = (os.getenv("TRANSLATION_PROVIDER") or "gemini").strip().lower()
    if provider == "gemini":
        return GeminiTranslator(api_key=api_key, model=model)
    elif provider == "claude":
        return ClaudeTranslator(api_key=api_key, model=model)
    else:
        raise ValueError(
            f"Unknown TRANSLATION_PROVIDER '{provider}' — expected 'gemini' or 'claude'."
        )
