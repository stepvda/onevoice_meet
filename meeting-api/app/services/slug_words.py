"""
Short-English-word slug generator.

Produces memorable 3-word room names like `happy-blue-tiger`. Room slugs are
exposed in shareable URLs (e.g. `https://meet.witysk.org/happy-blue-tiger`)
so they must be:

  - URL-safe (lowercase letters + dashes only)
  - easy to dictate over the phone (no homophones or obscure words)
  - not offensive in obvious ways
  - short (≤ 6 characters each)

The word list is intentionally small and hand-picked. With ~280 words the
space is roughly 280**3 ≈ 22 million combinations (~24 bits of entropy).
That's weak against offline search but ample against online enumeration when
paired with the anon-token rate limiter (30 requests/hour/IP). Sensitive
meetings should use the password option on top.

Adjectives and nouns are mixed in one pool rather than positional buckets —
avoids predictability and keeps collisions uniform.
"""
from __future__ import annotations

import secrets
from collections.abc import Callable


# Curated list of short, common, inoffensive English words (4–6 letters).
WORDS: tuple[str, ...] = (
    # colours
    "red", "blue", "green", "pink", "gold", "amber", "ruby", "coral",
    "lime", "ivory", "onyx", "plum", "rose", "sage", "tan", "teal",
    "violet", "white", "black", "cyan", "indigo", "olive", "peach",
    # animals
    "bear", "cat", "dog", "fox", "owl", "wolf", "deer", "duck",
    "frog", "hawk", "lion", "otter", "panda", "seal", "shark",
    "swan", "tiger", "whale", "zebra", "rabbit", "hare", "crow",
    "eagle", "goose", "koala", "lemur", "moose", "panther", "raven",
    "robin", "skunk", "sloth", "snail", "squid", "stoat", "toad",
    "turtle", "viper", "walrus", "yak", "buffalo", "cobra", "gazelle",
    "heron", "hedgehog", "jaguar", "parrot", "puffin", "salmon",
    # nature
    "cloud", "river", "stone", "leaf", "tree", "wind", "moon", "star",
    "sun", "sky", "sea", "lake", "hill", "peak", "path", "field",
    "forest", "meadow", "valley", "glade", "brook", "cliff", "dune",
    "beach", "ocean", "island", "canyon", "prairie", "wood", "grove",
    "petal", "flower", "clover", "fern", "pine", "oak", "maple",
    "cedar", "willow", "ivy", "moss", "reed",
    # weather
    "storm", "rain", "snow", "mist", "frost", "dew", "thunder",
    "breeze", "flurry", "fog", "hail", "ray", "spark", "ember",
    # objects
    "book", "coin", "lamp", "key", "door", "boat", "ship", "kite",
    "mask", "mug", "ring", "rope", "pen", "seal", "song", "tale",
    "drum", "flute", "harp", "piano", "violin", "shield", "sword",
    "anchor", "bell", "bridge", "candle", "carpet", "clock", "crown",
    "cube", "feather", "globe", "helm", "ladder", "mirror", "pillow",
    "quilt", "scroll", "torch", "thread", "vase", "wheel",
    # adjectives (size / feel / texture)
    "soft", "bold", "warm", "cool", "cold", "cozy", "crisp", "dense",
    "deep", "dry", "fair", "firm", "free", "full", "light", "loud",
    "mild", "neat", "pure", "quick", "quiet", "rapid", "rich",
    "ripe", "rough", "round", "sharp", "shiny", "slim", "slow",
    "small", "smooth", "solid", "steep", "sturdy", "sweet", "swift",
    "tall", "tame", "tangy", "tender", "tidy", "tiny", "tough",
    "wavy", "wide", "wild", "wise", "young", "zesty",
    # adjectives (mood / quality)
    "brave", "calm", "clever", "dreamy", "eager", "fancy", "friendly",
    "funny", "gentle", "glad", "happy", "humble", "jolly", "kind",
    "lucky", "merry", "noble", "peppy", "perky", "plucky", "proud",
    "shy", "silly", "smiling", "sunny", "tidy", "witty",
    # actions / verbs (gerund-ish nouns)
    "dancer", "dreamer", "runner", "jumper", "singer", "painter",
    "writer", "walker", "sailor", "baker", "gardener",
    # food
    "apple", "berry", "bread", "cherry", "honey", "mango", "melon",
    "peach", "pear", "plum", "grape", "lemon", "lime", "olive",
    "olives", "pasta", "spice", "sugar", "tea", "toast",
    # misc
    "alpha", "arc", "atlas", "aurora", "avenue", "cabin", "canopy",
    "carnival", "castle", "citadel", "comet", "crescent", "crystal",
    "echo", "ember", "galaxy", "harbor", "haven", "horizon", "lantern",
    "lyric", "mango", "meadow", "nectar", "oasis", "orchid", "palette",
    "pebble", "pine", "portal", "poster", "quartz", "rainbow", "ribbon",
    "scroll", "spiral", "spring", "sapphire", "studio", "thunder",
    "topaz", "trail", "velvet", "voyage", "wander", "wonder", "zenith",
)


# Deduplicate while preserving order (dev safety — the list may grow).
def _unique_preserve(seq):
    seen = set()
    out = []
    for s in seq:
        ls = s.lower()
        if ls in seen:
            continue
        seen.add(ls)
        out.append(ls)
    return out


_DEDUPED: tuple[str, ...] = tuple(_unique_preserve(WORDS))
if len(_DEDUPED) < 100:
    raise RuntimeError(f"slug word list too small: {len(_DEDUPED)} after dedupe")


def generate_slug() -> str:
    """One fresh 3-word slug like `happy-blue-tiger`.

    Uses `secrets` for cryptographic randomness (not `random`) since the slug
    is used as the user-visible room identifier."""
    return "-".join(secrets.choice(_DEDUPED) for _ in range(3))


def generate_unique_slug(exists: Callable[[str], bool], max_attempts: int = 16) -> str:
    """Generate a 3-word slug that doesn't collide with an existing one.

    `exists(slug)` should return True if that slug is already taken. Raises
    RuntimeError after `max_attempts` failed attempts (virtually impossible
    with 22M combinations but guards against a bug in the caller).
    """
    for _ in range(max_attempts):
        slug = generate_slug()
        if not exists(slug):
            return slug
    raise RuntimeError("could not generate a unique slug after many attempts")


__all__ = ["generate_slug", "generate_unique_slug", "WORDS"]
