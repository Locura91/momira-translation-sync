"""
translator.py — AI translation engine with fallback (Gemini first, then Claude).
"""

import os
import json
import time
from typing import Dict, List

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
                    max_tokens=8192,
                    system=SYSTEM_PROMPT,
                    tools=[TRANSLATION_TOOL],
                    tool_choice={"type": "tool", "name": "submit_translations"},
                    messages=[{"role": "user", "content": prompt}],
                    timeout=60,
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
                        "thinking_config": {"thinking_budget": 0},
                        "max_output_tokens": 8192,
                        "timeout": 60,
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
