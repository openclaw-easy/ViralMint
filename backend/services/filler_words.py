# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2025-2026 ViralMint Contributors
"""Which words of a transcript are fillers, in ANY language.

"Remove silence & filler words" used to match an English word list (`um`, `you know`, …), so on a Chinese, Spanish
or Japanese video it never found a filler: half the feature, silently. The list also cut words like "so", "like" and
"right" with no context ("so much" lost its "so"). Here the configured AI model reads the clip's own numbered words
and returns the indices that are pure disfluency in context — hesitation sounds, stutter repeats, filler phrases —
never a word that carries meaning.

Guarantees (tests/test_filler_words.py):
- Only indices that exist are kept; a chunk where the model marks more than MAX_SHARE of the words is not trusted
  and cuts nothing (a model that misreads the task must not butcher speech).
- Removing a real word is far worse than leaving an "um" in, so the prompt says: when unsure, keep it.
- Any failure (no AI key configured, a provider error, an unparseable reply, the deadline) is None: the caller cuts
  silences only and says so.
- Bounded: CHUNK_WORDS per call, CONCURRENCY calls at once, MAX_CHUNKS per transcript (past it, the rest is
  silence-only), and DEADLINE_S for the whole check.
"""
from __future__ import annotations

import asyncio
import json
import logging

logger = logging.getLogger(__name__)

CHUNK_WORDS = 300
CONCURRENCY = 3
MAX_CHUNKS = 40           # 12,000 words, about 80 minutes of speech
MAX_SHARE = 0.25          # more than this marked in one chunk = the model misread the task
MIN_WORDS_FOR_SHARE = 20  # a short chunk (a clip's tail) is too small to judge by share
DEADLINE_S = 40.0         # the whole check: past it the clip is "not checked" and only silences are cut
_MAX_TOKENS = 600

FILLER_PROMPT = """Below is part of a spoken transcript, first as text, then one word per line with its number. The speech can be in any language.

Mark only the words a careful editor would cut from the audio without changing what is said:
- hesitation sounds: sounds that are not words at all, like "um", "uh", "er", "嗯", "呃", "えーと", "ähm", "eh", and their equivalents in any language,
- one copy of a word the speaker stumbled on and said twice in a row ("I I think" → mark the first "I"),
- a phrase that is pure filler in THIS sentence (in English for example "you know" or "I mean" used as padding), only when the sentence reads exactly the same without it.

A word that does grammar in its sentence is never a filler: a demonstrative or article before a noun ("este libro", "那个人", "あの人", "this book"), a verb with its object ("do you know him"), a comparison ("like his dad"), a connective that links two clauses ("so", "also", "就是" meaning "is exactly"), a particle that ends or colours a sentence ("吧", "呢", "ね", "よ"). When unsure, do NOT mark it: leaving a filler in is fine, cutting a real word is not.

Answer with JSON only: {{"fillers": [numbers]}}. If there are none, answer {{"fillers": []}}.

Text:
{text}

Words:
{lines}"""


def _word_text(w: dict) -> str:
    return " ".join(str(w.get("text", "")).split())[:40]


async def _one_chunk(ai, words: list[dict], offset: int) -> set[int] | None:
    from backend.core.prompt_rules import extract_json_text
    lines = "\n".join(f"{i} {_word_text(w)}" for i, w in enumerate(words))
    text = " ".join(_word_text(w) for w in words)
    reply = await ai.chat(
        messages=[{"role": "user", "content": FILLER_PROMPT.format(text=text, lines=lines)}],
        max_tokens=_MAX_TOKENS,
    )
    parsed = json.loads(extract_json_text(reply))
    raw = parsed.get("fillers") if isinstance(parsed, dict) else parsed
    if not isinstance(raw, list):
        return None
    picked = {int(x) for x in raw if isinstance(x, (int, float)) and not isinstance(x, bool)
              and float(x).is_integer() and 0 <= int(x) < len(words)}
    if len(words) >= MIN_WORDS_FOR_SHARE and len(picked) > MAX_SHARE * len(words):
        logger.warning("Filler pick distrusted: %d of %d words marked", len(picked), len(words))
        return set()
    return {offset + i for i in picked}


async def find_fillers(words: list[dict], user_settings=None) -> set[int] | None:
    """Indices into `words` (dicts with `text`) that are fillers, judged by the AI model `user_settings` configures.

    None when it couldn't be answered at all; a chunk that fails is treated as having none (its silences are still
    cut)."""
    if not words:
        return set()
    try:
        from backend.core.ai_provider import get_ai_client
        ai = get_ai_client(user_settings)
    except Exception as e:  # noqa: BLE001 — no AI key configured: no filler pass, silences only
        logger.info("Filler detection unavailable: %s", type(e).__name__)
        return None
    chunks = [(i, words[i:i + CHUNK_WORDS]) for i in range(0, len(words), CHUNK_WORDS)][:MAX_CHUNKS]
    gate = asyncio.Semaphore(CONCURRENCY)

    async def run(offset: int, chunk: list[dict]):
        async with gate:
            try:
                return await _one_chunk(ai, chunk, offset)
            except Exception as e:  # noqa: BLE001 — one chunk failing keeps the rest
                logger.warning("Filler detection failed for words %d-%d: %s", offset, offset + len(chunk), type(e).__name__)
                return None

    try:
        # Async HTTP calls, so wait_for really cancels them (unlike a call parked in a thread).
        results = await asyncio.wait_for(asyncio.gather(*(run(o, c) for o, c in chunks)), DEADLINE_S)
    except asyncio.TimeoutError:
        logger.warning("Filler detection passed its %.0f s deadline: removing silences only", DEADLINE_S)
        return None
    if all(r is None for r in results):
        return None
    return set().union(*(r for r in results if r))
