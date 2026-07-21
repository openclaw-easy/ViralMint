# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2025-2026 ViralMint Contributors
"""Singleton faster-whisper transcription service."""
import asyncio
import logging
from pathlib import Path
from backend.config import settings

logger = logging.getLogger(__name__)

WHISPER_QUALITY_MAP = {
    "fast":     "base",
    "balanced": "small",
    "accurate": "medium",
    "best":     "large-v3",
}

# Approximate download sizes for first-time use
WHISPER_MODEL_SIZES = {
    "base":     "~150 MB",
    "small":    "~500 MB",
    "medium":   "~1.5 GB",
    "large-v3": "~3 GB",
}


class WhisperService:
    _model = None
    _loaded_quality = None

    @classmethod
    def is_model_cached(cls, quality: str = "balanced") -> bool:
        """Check if the Whisper model is already downloaded in HuggingFace cache."""
        model_name = WHISPER_QUALITY_MAP.get(quality, "small")
        cache_dir = Path.home() / ".cache" / "huggingface" / "hub"
        model_dir = cache_dir / f"models--Systran--faster-whisper-{model_name}"
        if not model_dir.exists():
            return False
        # Check for .incomplete files — model is still downloading
        blobs = model_dir / "blobs"
        if blobs.exists():
            for f in blobs.iterdir():
                if f.suffix == ".incomplete":
                    return False
        return True

    @classmethod
    def load(cls, quality: str = "balanced"):
        if cls._model is None or cls._loaded_quality != quality:
            from faster_whisper import WhisperModel
            model_name = WHISPER_QUALITY_MAP.get(quality, "small")
            logger.info(f"Loading Whisper model: {model_name}")
            cls._model = WhisperModel(
                model_name,
                device="cpu",
                compute_type="int8",
            )
            cls._loaded_quality = quality
            logger.info(f"Whisper model '{model_name}' loaded")
        return cls._model

    async def transcribe(self, audio_path: str | Path, language: str = None) -> dict:
        """
        Transcribe audio file. Returns:
        { "text": "...", "language": "en", "segments": [...] }
        """
        # Pre-check: confirm the file actually has an audio stream before
        # invoking whisper. Without this, faster-whisper crashes with a
        # cryptic "tuple index out of range" when given a silent file
        # (e.g. video-only mp4 from a downloader that missed the audio
        # track).
        from backend.services.ffmpeg_service import has_audio_stream
        if not await has_audio_stream(Path(audio_path)):
            raise ValueError(f"No audio stream found in {audio_path}")

        model = self.load()

        def _run():
            segments_gen, info = model.transcribe(
                str(audio_path),
                beam_size=5,
                language=language,
                word_timestamps=True,
            )
            segments = list(segments_gen)
            text = " ".join([s.text.strip() for s in segments])
            return {
                "text": text,
                "language": info.language,
                "language_probability": info.language_probability,
                "segments": [
                    {
                        "start": s.start,
                        "end": s.end,
                        "text": s.text.strip(),
                        "words": [
                            {"word": w.word, "start": float(w.start), "end": float(w.end), "probability": float(w.probability)}
                            for w in (s.words or [])
                        ],
                    }
                    for s in segments
                ],
            }

        return await asyncio.to_thread(_run)


whisper_service = WhisperService()
