#!/usr/bin/env python3
"""
Delta-translate only the keys that exist in en.json but are missing from each
target locale. Existing translations are preserved.

USAGE
-----
  export DEEPSEEK_API_KEY=sk-...
  python3 scripts/translate_missing_keys.py [--only fr,es,de] [--invalidate key1,key2]

Re-runs are idempotent: keys already present in a locale file are skipped
unless explicitly listed via --invalidate.

Why this exists alongside translate_locales.py: that script overwrites the
whole file. We just added ~40 new keys to en.json; we want to fill those into
the existing locale files without re-translating the other 280-odd keys.
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

LANGS = [
    ("am", "Amharic"), ("ar", "Arabic"), ("bg", "Bulgarian"), ("bn", "Bengali"),
    ("cs", "Czech"), ("da", "Danish"), ("de", "German"), ("el", "Greek"),
    ("es", "Spanish"), ("fa", "Persian (Farsi)"), ("fi", "Finnish"),
    ("fil", "Filipino (Tagalog)"), ("fr", "French"), ("gu", "Gujarati"),
    ("ha", "Hausa"), ("he", "Hebrew"), ("hi", "Hindi"), ("hr", "Croatian"),
    ("hu", "Hungarian"), ("id", "Indonesian"), ("it", "Italian"),
    ("ja", "Japanese"), ("kk", "Kazakh"), ("ko", "Korean"), ("mr", "Marathi"),
    ("ms", "Malay"), ("my", "Burmese (Myanmar)"), ("ne", "Nepali"),
    ("nl", "Dutch"), ("no", "Norwegian"), ("pa", "Punjabi"), ("pl", "Polish"),
    ("pt", "Portuguese"), ("ro", "Romanian"), ("ru", "Russian"),
    ("sk", "Slovak"), ("sl", "Slovenian"), ("sr", "Serbian"),
    ("sv", "Swedish"), ("sw", "Swahili"), ("ta", "Tamil"), ("te", "Telugu"),
    ("th", "Thai"), ("tr", "Turkish"), ("uk", "Ukrainian"), ("ur", "Urdu"),
    ("uz", "Uzbek"), ("vi", "Vietnamese"), ("zh", "Chinese (Simplified)"),
]

WORKERS = 20
DEEPSEEK_URL = "https://api.deepseek.com/chat/completions"
MODEL = "deepseek-chat"

# Match {{name}}, {{count}}, etc. AND <1>...</1> Trans markers.
PLACEHOLDER_RE = re.compile(r"({{[^}]+?}}|<\d+>|</\d+>)")


def flatten(obj: dict, prefix: str = "") -> dict[str, str]:
    out: dict[str, str] = {}
    for k, v in obj.items():
        path = f"{prefix}.{k}" if prefix else k
        if isinstance(v, dict):
            out.update(flatten(v, path))
        elif isinstance(v, str):
            out[path] = v
    return out


def set_at(root: dict, dotted: str, value: str) -> None:
    parts = dotted.split(".")
    node = root
    for p in parts[:-1]:
        nxt = node.get(p)
        if not isinstance(nxt, dict):
            nxt = {}
            node[p] = nxt
        node = nxt
    node[parts[-1]] = value


def shield(text: str) -> tuple[str, list[str]]:
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
        f"web app's user interface. Keep the translation natural and concise; "
        f"prefer the wording a real product would use. Match the original "
        f"capitalisation style. Preserve any tokens of the form <<<KEEP_n>>> "
        f"EXACTLY as-is — they are runtime placeholders. Do not translate brand "
        f"names like meet.witysk.org, one.witysk.org, YouTube, LiveKit, TURN, "
        f"WebRTC, IPv4, IPv6, MP4, WebM, H.264, AAC, VP9, Opus. Do not add any "
        f"commentary, prefixes, suffixes, or quotation marks; output only the "
        f"translated text."
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
                    raise httpx.HTTPStatusError(
                        f"status {r.status_code}", request=r.request, response=r
                    )
                r.raise_for_status()
                content = r.json()["choices"][0]["message"]["content"].strip()
                if (content.startswith('"') and content.endswith('"')) or (
                    content.startswith("'") and content.endswith("'")
                ):
                    content = content[1:-1]
                return unshield(content, holders)
            except (httpx.HTTPError, KeyError, IndexError) as e:
                if attempt == max_retries - 1:
                    print(f"   giving up on {text[:60]!r}: {e}", file=sys.stderr)
                    return text
                await asyncio.sleep(backoff)
                backoff *= 2


async def translate_locale(
    api_key: str,
    code: str,
    name: str,
    en_flat: dict[str, str],
    invalidate: set[str],
) -> None:
    out_path = OUT_DIR / f"{code}.json"
    if not out_path.exists():
        print(f"  {code}: file does not exist; use translate_locales.py first")
        return
    existing = json.loads(out_path.read_text(encoding="utf-8"))
    existing_flat = flatten(existing)
    for k in invalidate:
        existing_flat.pop(k, None)

    missing = [(k, en_flat[k]) for k in en_flat if k not in existing_flat]
    if not missing:
        print(f"  {code}: nothing to do")
        return

    print(f"  {code} ({name}): translating {len(missing)} keys")
    started = time.time()
    sem = asyncio.Semaphore(WORKERS)
    async with httpx.AsyncClient() as client:
        tasks = [translate_one(client, api_key, name, src, sem) for _, src in missing]
        translated = await asyncio.gather(*tasks)
    for (path, _src), tr in zip(missing, translated, strict=True):
        set_at(existing, path, tr)
    out_path.write_text(json.dumps(existing, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(f"     wrote {code}.json in {(time.time() - started):.1f}s")


async def amain(argv: list[str]) -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--only", help="comma-separated subset of language codes")
    p.add_argument(
        "--invalidate",
        help="comma-separated dotted keys to drop from each locale (force retranslation)",
        default="background.notImage,recordings.empty",
    )
    args = p.parse_args(argv)

    api_key = os.environ.get("DEEPSEEK_API_KEY", "").strip()
    if not api_key:
        print("DEEPSEEK_API_KEY not set in environment", file=sys.stderr)
        return 2

    en = json.loads(EN_PATH.read_text(encoding="utf-8"))
    en_flat = flatten(en)
    print(f"en.json: {len(en_flat)} keys")

    selected = LANGS
    if args.only:
        wanted = {s.strip() for s in args.only.split(",")}
        selected = [(c, n) for c, n in LANGS if c in wanted]
    invalidate = {s.strip() for s in args.invalidate.split(",") if s.strip()}
    if invalidate:
        print(f"invalidate (force-retranslate): {sorted(invalidate)}")

    overall = time.time()
    for code, name in selected:
        await translate_locale(api_key, code, name, en_flat, invalidate)
    print(f"\nall done in {(time.time() - overall):.1f}s")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(amain(sys.argv[1:])))
