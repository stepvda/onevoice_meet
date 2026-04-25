#!/usr/bin/env python3
"""
Translate the meet.witysk.org en.json into the same 49 locales onevoice uses,
via DeepSeek's chat-completion API and 20 parallel workers.

USAGE
-----
  export DEEPSEEK_API_KEY=sk-...
  python3 scripts/translate_locales.py [--only fr,es,de] [--force]

By default only locales that don't yet have a JSON file are produced. Pass
`--force` to overwrite existing translations. Pass `--only <list>` to
generate just a subset.

Output goes to frontend/public/locales/<lang>.json, mirroring the structure
of frontend/public/locales/en.json. Order is preserved.

Implementation notes
--------------------
- We send each *leaf string* through DeepSeek individually rather than in
  one giant payload, so a flaky API call on one key doesn't lose the lot.
- Within a language we use 20 parallel workers via asyncio + a Semaphore.
- DeepSeek's `chat/completions` endpoint is OpenAI-compatible.
- Interpolation tokens like `{{count}}` are protected: we wrap them in a
  `<<<KEEP_n>>>` sentinel, ask the model to copy-through, then unwrap.
- Plural-suffix keys (`xxx_one`, `xxx_other`) are sent unchanged so the
  model gets the singular/plural intent. CLDR plural categories vary per
  language; i18next will fall back to `_other` when needed.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import re
import sys
import time
from pathlib import Path

import httpx

REPO = Path(__file__).resolve().parent.parent
EN_PATH = REPO / "frontend" / "public" / "locales" / "en.json"
OUT_DIR = EN_PATH.parent

# Same 50 locales onevoice ships, minus en.
LANGS = [
    ("am", "Amharic"),
    ("ar", "Arabic"),
    ("bg", "Bulgarian"),
    ("bn", "Bengali"),
    ("cs", "Czech"),
    ("da", "Danish"),
    ("de", "German"),
    ("el", "Greek"),
    ("es", "Spanish"),
    ("fa", "Persian (Farsi)"),
    ("fi", "Finnish"),
    ("fil", "Filipino (Tagalog)"),
    ("fr", "French"),
    ("gu", "Gujarati"),
    ("ha", "Hausa"),
    ("he", "Hebrew"),
    ("hi", "Hindi"),
    ("hr", "Croatian"),
    ("hu", "Hungarian"),
    ("id", "Indonesian"),
    ("it", "Italian"),
    ("ja", "Japanese"),
    ("kk", "Kazakh"),
    ("ko", "Korean"),
    ("mr", "Marathi"),
    ("ms", "Malay"),
    ("my", "Burmese (Myanmar)"),
    ("ne", "Nepali"),
    ("nl", "Dutch"),
    ("no", "Norwegian"),
    ("pa", "Punjabi"),
    ("pl", "Polish"),
    ("pt", "Portuguese"),
    ("ro", "Romanian"),
    ("ru", "Russian"),
    ("sk", "Slovak"),
    ("sl", "Slovenian"),
    ("sr", "Serbian"),
    ("sv", "Swedish"),
    ("sw", "Swahili"),
    ("ta", "Tamil"),
    ("te", "Telugu"),
    ("th", "Thai"),
    ("tr", "Turkish"),
    ("uk", "Ukrainian"),
    ("ur", "Urdu"),
    ("uz", "Uzbek"),
    ("vi", "Vietnamese"),
    ("zh", "Chinese (Simplified)"),
]

# Limit to 20 in-flight DeepSeek requests per language.
WORKERS = 20

DEEPSEEK_URL = "https://api.deepseek.com/chat/completions"
MODEL = "deepseek-chat"

PLACEHOLDER_RE = re.compile(r"({{[^}]+?}})")  # {{count}}, {{name}}, etc.


def flatten(obj: dict, prefix: str = "") -> list[tuple[str, str]]:
    """Walk the JSON tree and yield (dotted-key, leaf-string) pairs."""
    out: list[tuple[str, str]] = []
    for k, v in obj.items():
        path = f"{prefix}.{k}" if prefix else k
        if isinstance(v, dict):
            out.extend(flatten(v, path))
        elif isinstance(v, str):
            out.append((path, v))
        # numbers, lists, etc. — skip (none in our schema)
    return out


def unflatten(pairs: list[tuple[str, str]]) -> dict:
    """Rebuild a nested dict from dotted keys, preserving insertion order."""
    root: dict = {}
    for k, v in pairs:
        parts = k.split(".")
        node = root
        for p in parts[:-1]:
            node = node.setdefault(p, {})
        node[parts[-1]] = v
    return root


def shield(text: str) -> tuple[str, list[str]]:
    """Replace `{{interp}}` with sentinels so the model preserves them."""
    placeholders: list[str] = []

    def sub(m: re.Match) -> str:
        placeholders.append(m.group(0))
        return f"<<<KEEP_{len(placeholders) - 1}>>>"

    return PLACEHOLDER_RE.sub(sub, text), placeholders


def unshield(text: str, placeholders: list[str]) -> str:
    for i, ph in enumerate(placeholders):
        text = text.replace(f"<<<KEEP_{i}>>>", ph)
    return text


def system_prompt(lang_name: str) -> str:
    return (
        f"You are a professional UI translator. Translate the user's text from "
        f"English into {lang_name}. The text is part of a video-conferencing "
        f"application's user interface. Keep the translation natural and concise; "
        f"prefer the wording a real product would use. Match the original "
        f"capitalisation style (sentence case, title case, etc.) where it is "
        f"meaningful in the target language. Preserve any tokens of the form "
        f"<<<KEEP_n>>> EXACTLY as-is — they are runtime placeholders. "
        f"Do not add any commentary, prefixes, suffixes, or quotation marks; "
        f"output only the translated text."
    )


async def translate_one(
    client: httpx.AsyncClient,
    api_key: str,
    lang_name: str,
    text: str,
    sem: asyncio.Semaphore,
    max_retries: int = 4,
) -> str:
    shielded, holders = shield(text)
    body = {
        "model": MODEL,
        "messages": [
            {"role": "system", "content": system_prompt(lang_name)},
            {"role": "user", "content": shielded},
        ],
        "temperature": 0.2,
        "max_tokens": 600,
    }
    backoff = 1.0
    async with sem:
        for attempt in range(max_retries):
            try:
                r = await client.post(
                    DEEPSEEK_URL,
                    headers={
                        "Authorization": f"Bearer {api_key}",
                        "Content-Type": "application/json",
                    },
                    json=body,
                    timeout=60,
                )
                if r.status_code == 429 or r.status_code >= 500:
                    raise httpx.HTTPStatusError(f"status {r.status_code}", request=r.request, response=r)
                r.raise_for_status()
                content = r.json()["choices"][0]["message"]["content"].strip()
                # Strip wrapping quotes the model sometimes adds anyway.
                if (content.startswith('"') and content.endswith('"')) or (
                    content.startswith("'") and content.endswith("'")
                ):
                    content = content[1:-1]
                return unshield(content, holders)
            except (httpx.HTTPError, KeyError, IndexError) as e:
                if attempt == max_retries - 1:
                    print(f"   ⚠️  giving up on {text[:60]!r}: {e}", file=sys.stderr)
                    return text  # graceful fallback: keep English
                await asyncio.sleep(backoff)
                backoff *= 2


async def translate_language(
    api_key: str,
    code: str,
    name: str,
    pairs: list[tuple[str, str]],
    out_path: Path,
) -> None:
    print(f"→ {code} ({name}) — {len(pairs)} strings, {WORKERS} workers")
    started = time.time()
    sem = asyncio.Semaphore(WORKERS)
    async with httpx.AsyncClient() as client:
        tasks = [translate_one(client, api_key, name, src, sem) for _, src in pairs]
        translated = await asyncio.gather(*tasks)
    new_pairs = list(zip([k for k, _ in pairs], translated, strict=True))
    out = unflatten(new_pairs)
    out_path.write_text(json.dumps(out, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(f"   ✅ {code} written ({(time.time() - started):.1f}s, {out_path.stat().st_size} bytes)")


async def amain(argv: list[str]) -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--only", help="comma-separated subset of language codes")
    p.add_argument("--force", action="store_true", help="overwrite existing files")
    args = p.parse_args(argv)

    api_key = os.environ.get("DEEPSEEK_API_KEY", "").strip()
    if not api_key:
        print("DEEPSEEK_API_KEY not set in environment", file=sys.stderr)
        return 2

    en = json.loads(EN_PATH.read_text(encoding="utf-8"))
    pairs = flatten(en)
    print(f"loaded {len(pairs)} strings from {EN_PATH.relative_to(REPO)}")

    selected = LANGS
    if args.only:
        wanted = {s.strip() for s in args.only.split(",")}
        selected = [(c, n) for c, n in LANGS if c in wanted]
        print(f"--only filter: {sorted(wanted)} → {len(selected)} matched")

    OUT_DIR.mkdir(parents=True, exist_ok=True)
    overall_started = time.time()
    for code, name in selected:
        out = OUT_DIR / f"{code}.json"
        if out.exists() and not args.force:
            print(f"skip {code} (exists; use --force to overwrite)")
            continue
        await translate_language(api_key, code, name, pairs, out)

    print(f"\n✅ all done in {(time.time() - overall_started):.1f}s")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(amain(sys.argv[1:])))
