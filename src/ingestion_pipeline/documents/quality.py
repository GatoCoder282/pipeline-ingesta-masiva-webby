"""Deterministic quality checks for OCR-derived product descriptions."""

from __future__ import annotations

import re
from collections.abc import Iterable
from dataclasses import asdict, dataclass

DEFAULT_NOISE_TOKENS = frozenset(
    {
        "aono",
        "ocrojojo",
        "ojsjojo",
        "fragance",
        "mineral cruelty",
        "paraben free",
        "oil free",
        "free free",
        "ree ree",
        "ree",
        "vaneue",
        "+ informacion",
        "+ información",
        "odo tipo de piel",
    }
)


@dataclass(frozen=True)
class DescriptionQuality:
    status: str
    score: int
    flags: tuple[str, ...] = ()
    details: tuple[str, ...] = ()
    layout_reconstructed: bool = False

    def as_json(self) -> dict[str, object]:
        return asdict(self) | {
            "flags": list(self.flags),
            "details": list(self.details),
        }


def _noise_matches(text: str, tokens: Iterable[str]) -> list[str]:
    folded = " ".join(text.casefold().split())
    return sorted(
        {
            token
            for token in tokens
            if token
            and re.search(
                rf"(?<!\w){re.escape(token.casefold())}(?!\w)",
                folded,
            )
        }
    )


def assess_description(
    value: str | None,
    *,
    engine: str = "native",
    layout_reconstructed: bool = False,
    strict_visual: bool = False,
    min_chars: int = 40,
    noise_tokens: Iterable[str] = (),
) -> DescriptionQuality:
    """Classify a description without changing its source-backed content."""
    text = " ".join((value or "").split())
    flags: list[str] = []
    details: list[str] = []
    configured_noise = set(DEFAULT_NOISE_TOKENS) | {str(token).casefold() for token in noise_tokens}

    if not text:
        flags.append("empty")
    elif len(text) < min_chars:
        flags.append("too_short")
    if any(marker in text for marker in ("Ã", "Â", "�")):
        flags.append("mojibake")
    matches = _noise_matches(text, configured_noise)
    if matches:
        flags.append("ocr_noise_tokens")
        details.append(f"Tokens sospechosos: {', '.join(matches)}")
    if re.search(r"\b(\w+)(?:\s+\1){1,}\b", text, re.IGNORECASE):
        flags.append("repeated_words")
    if re.search(r"\b\w+-\s+[a-záéíóúñ]", text, re.IGNORECASE):
        flags.append("split_word")
    if re.search(r"\b[a-záéíóúñü]+[A-ZÁÉÍÓÚÑÜ][a-záéíóúñü]+\b", text):
        flags.append("mixed_case_word")
    if len(text) >= min_chars and not re.search(r"[.!?]", text) and "\n" not in (value or ""):
        flags.append("no_sentence_structure")
    if strict_visual and engine in {"tesseract", "paddleocr", "ocr"} and not layout_reconstructed:
        flags.append("visual_layout_not_reconstructed")

    severe_flags = {"empty", "mojibake", "ocr_noise_tokens", "visual_layout_not_reconstructed"}
    if not text or "mojibake" in flags:
        status = "quarantine"
    elif severe_flags.intersection(flags):
        status = "reconstruct"
    elif flags:
        status = "reconstruct"
    else:
        status = "pass"
    score = max(0, 100 - len(flags) * 18)
    return DescriptionQuality(
        status=status,
        score=score,
        flags=tuple(flags),
        details=tuple(details),
        layout_reconstructed=layout_reconstructed,
    )
