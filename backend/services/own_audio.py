# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2025-2026 ViralMint Contributors
"""My audio: what Whisper heard -> scene cuts, and each cut's take key. Pure: no IO, no ffmpeg
(studio_audio.listen cuts the file).

Sentences end on . ! ? and their CJK twins. studio_doc.pack_scenes then groups them exactly as a typed
script is grouped, so a scene stays under LINE_MAX and the video under SCENE_MAX. A cut runs from its
first word's start minus LEAD_S (never before the previous cut's end) to its last word's end plus
TAIL_S (never past the next cut's first word)."""
from __future__ import annotations

import math
from dataclasses import dataclass

from backend.services import studio_doc as sd
from backend.services import voice_take_service as vts

LEAD_S = 0.08
TAIL_S = 0.12
MIN_CUT_S = 0.25
_ENDERS = ".!?。！？"
_CLOSERS = "\"'”’」』)]）】»"
NO_SPEECH = "We didn't hear any speech in this audio. Pick another one."
TOO_MUCH = "This audio has more sentences than one video can hold. Pick a shorter one."


class AudioRefused(Exception):
    """The audio can't become scenes. `code` is the job row's `refused` (final: trying again can't help)."""

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


@dataclass(frozen=True)
class Word:
    text: str
    start: float
    end: float
    lead_space: bool = True       # Whisper's token began with whitespace (a word break, even in Korean)


def heard_words(transcript: dict) -> list[Word]:
    out: list[Word] = []
    for seg in (transcript or {}).get("segments") or []:
        for w in (seg or {}).get("words") or []:
            raw_text = str((w or {}).get("word") or "")
            text = raw_text.strip()
            try:
                start, end = float(w.get("start")), float(w.get("end"))
            except (TypeError, ValueError, AttributeError):
                continue
            if not text or not math.isfinite(start) or not math.isfinite(end):
                continue
            start = max(0.0, start)
            out.append(Word(text, start, max(start, end), lead_space=raw_text[:1].isspace()))
    out.sort(key=lambda w: w.start)
    return out


def _ends_sentence(w: Word) -> bool:
    t = w.text.rstrip(_CLOSERS)
    return bool(t) and t[-1] in _ENDERS


def sentences(words: list[Word]) -> list[list[Word]]:
    out, cur = [], []
    for w in words:
        cur.append(w)
        if _ends_sentence(w):
            out.append(cur)
            cur = []
    if cur:
        out.append(cur)
    return out


_CONTINUES = frozenset(",.;:!?%'’)]}…\"”-")


def _continues(prev: str, w: Word) -> bool:
    """A token Whisper gave no leading space that belongs to the word before it: ",000", "%", ".5",
    "?", "'s", or digits after digits. A letter never qualifies, so a producer that drops the leading
    spaces can't glue two real words together."""
    if w.lead_space or not prev:
        return False
    first = w.text[0]
    return first in _CONTINUES or (first.isdigit() and prev[-1].isdigit())


def join_words(words) -> str:
    text = ""
    for w in words:
        if text and w.lead_space and sd._is_cjk(text[-1]) and sd._is_cjk(w.text[0]):
            text = f"{text} {w.text}"                    # Whisper marked a word break (Korean): keep it
        elif _continues(text, w):
            text = f"{text}{w.text}"                     # the rest of the same word: 60 + ,000
        else:
            text = sd.join_sentences(text, w.text)       # Chinese/Japanese tokens: no space; everything else: one
    return sd.normalise_line(text)


def _compact(text: str) -> int:
    return len("".join(text.split()))


def _assign(lines: list[str], words: list[Word]) -> list[list[Word]]:
    """The packed lines back onto the words they came from (non-space characters line up); a word
    straddling a hard cut stays with the line it starts in."""
    out: list[list[Word]] = []
    i, debt = 0, 0
    for line in lines:
        want = _compact(line) - debt
        if want <= 0:
            debt = -want
            continue
        got, cut = 0, []
        while i < len(words) and got < want:
            cut.append(words[i])
            got += _compact(words[i].text)
            i += 1
        debt = got - want
        if cut:
            out.append(cut)
    if i < len(words):                                    # never drop a word
        if out:
            out[-1].extend(words[i:])
        else:
            out.append(list(words[i:]))
    return out


def _fit_line(cuts: list[list[Word]]) -> list[list[Word]]:
    """No cut over LINE_MAX: a straddling word moves on to the next cut."""
    k = 0
    while k < len(cuts):
        while len(join_words(cuts[k])) > sd.LINE_MAX and len(cuts[k]) > 1:
            w = cuts[k].pop()
            if k + 1 < len(cuts):
                cuts[k + 1].insert(0, w)
            else:
                cuts.append([w])
        k += 1
    return cuts


def _span(cut: list[Word]) -> float:
    return cut[-1].end - cut[0].start + LEAD_S + TAIL_S


def split_long_spans(cuts: list[list[Word]]) -> list[list[Word]]:
    """No cut over AUDIO_SCENE_MAX_S: it splits at its largest gap between words, again and again.
    Every word stays in exactly one cut, in order."""
    out: list[list[Word]] = []
    for cut in cuts:
        if _span(cut) <= sd.AUDIO_SCENE_MAX_S or len(cut) < 2:
            out.append(cut)
            continue
        k = max(range(1, len(cut)), key=lambda i: cut[i].start - cut[i - 1].end)
        out.extend(split_long_spans([cut[:k], cut[k:]]))
    return out


def merge_to(cuts, limit: int) -> list[list[Word]]:
    """At most `limit` cuts: the shortest neighbouring pair that still fits a line merges, again and
    again. Nothing is dropped; a recording that can't fit is refused."""
    cuts = [list(c) for c in cuts]
    while len(cuts) > limit:
        best = None
        for k in range(len(cuts) - 1):
            size = len(join_words(cuts[k] + cuts[k + 1]))
            if size <= sd.LINE_MAX and _span(cuts[k] + cuts[k + 1]) <= sd.AUDIO_SCENE_MAX_S and (best is None or size < best[0]):
                best = (size, k)
        if best is None:
            raise AudioRefused("too_long", TOO_MUCH)
        k = best[1]
        cuts[k:k + 2] = [cuts[k] + cuts[k + 1]]
    return cuts


def bounds(cuts: list[list[Word]], duration: float) -> list[tuple[float, float]]:
    out, prev_end = [], 0.0
    for k, cut in enumerate(cuts):
        start = max(prev_end, cut[0].start - LEAD_S)
        cap = cuts[k + 1][0].start if k + 1 < len(cuts) else (duration if duration > 0 else cut[-1].end + TAIL_S)
        end = min(cut[-1].end + TAIL_S, cap)
        if duration > 0:
            end = min(end, duration)
        if end - start < MIN_CUT_S:
            end = start + MIN_CUT_S
        start, end = round(start, 3), round(end, 3)
        out.append((start, end))
        prev_end = end
    return out


def take_key(audio_id: str, text: str, start: float, end: float) -> str:
    return vts.key_for_line(vts.VoiceSpec.own_audio(audio_id, start, end), text)


def cut_lines(transcript: dict, duration: float, audio_id: str) -> list[dict]:
    """The scenes of one recording: [{text, start, end, duration, key, words}], words relative to the cut."""
    words = heard_words(transcript)
    if not words:
        raise AudioRefused("no_speech", NO_SPEECH)
    packed = sd.pack_scenes([join_words(g) for g in sentences(words)])
    cuts = merge_to(split_long_spans(_fit_line(_assign(packed, words))), sd.SCENE_MAX)
    out = []
    for cut, (start, end) in zip(cuts, bounds(cuts, float(duration or 0))):
        text = join_words(cut)
        span = round(end - start, 3)
        out.append({"text": text, "start": start, "end": end, "duration": span,
                    "key": take_key(audio_id, text, start, end),
                    "words": [[w.text[:sd.WORD_CHARS_MAX], round(min(span, max(0.0, w.start - start)), 3),
                               round(min(span, max(0.0, w.end - start)), 3)] for w in caption_words(cut)][:sd.WORDS_MAX]})
    return out


def caption_words(words: list[Word]) -> list[Word]:
    """The words a caption shows one by one: a token that continues the word before it (",000", ".99",
    "%", "'s", "?": `_continues`, the rule `join_words` uses for the line) is merged into that word,
    so word-by-word captions read "60,000" and "$9.99", never "60 ,000"."""
    out: list[Word] = []
    for w in words:
        if out and _continues(out[-1].text, w):
            prev = out[-1]
            out[-1] = Word(prev.text + w.text, prev.start, max(prev.end, w.end), prev.lead_space)
        else:
            out.append(w)
    return out
