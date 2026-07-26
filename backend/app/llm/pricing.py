"""Model prices, in one place, so a spend figure is never a guess.

Reasoning tokens bill as OUTPUT — at $30/Mtok they are the expensive half of an extraction
call, which is why the gateway notches reasoning effort down before it doubles a budget.

TWO BILLING UNITS LIVE HERE, AND THEY ARE NOT INTERCHANGEABLE. Text models bill per TOKEN
(`PRICES` / `cost_usd`). Speech-to-text bills per MINUTE OF AUDIO (`AUDIO_PRICES_PER_MINUTE` /
`audio_cost_usd`): the transcription endpoint's default JSON response reports no token usage at
all, so a transcription costed through the token path would record $0 and quietly subtract
nothing from the household's monthly budget. Hence a second function rather than a fake token
count — the input to a transcription price is a duration, and the type says so.
"""

from decimal import ROUND_HALF_UP, Decimal

# model -> ($ per million input tokens, $ per million output tokens)
PRICES: dict[str, tuple[Decimal, Decimal]] = {
    "gpt-5.5-2026-04-23": (Decimal("5"), Decimal("30")),
}

# model -> $ per minute of audio, the unit OpenAI publishes speech-to-text in.
#
# The gpt-4o transcribe family is metered underneath in AUDIO INPUT TOKENS, and its response
# reports `usage.type="tokens"` rather than a duration. These are the per-minute equivalents
# OpenAI publishes for exactly that reason: audio tokens cannot be turned back into seconds
# from here, so a minute of audio is the only unit both sides of this arithmetic can agree on.
# whisper-1 really is billed per minute and reports `usage.type="duration"`.
#
# `gpt-4o-transcribe-diarize` is DELIBERATELY ABSENT even though the key can call it: its
# per-minute rate was not verified, and an unverified price is worse than no price. Selecting
# it raises `UnpricedModelError`, which `app.transcription` turns into "leave it as a voice
# note" plus a loud log — a refusal, not a guess that lands in the budget arithmetic.
AUDIO_PRICES_PER_MINUTE: dict[str, Decimal] = {
    "gpt-4o-mini-transcribe": Decimal("0.003"),
    "gpt-4o-transcribe": Decimal("0.006"),
    "whisper-1": Decimal("0.006"),
}

_SECONDS_PER_MINUTE = Decimal("60")

# The billing fallback when nothing tells us how long the audio was. WhatsApp voice notes are
# mono Opus in the region of 16 kbit/s, and this assumption is deliberately at the LOW end:
# assuming fewer bits per second infers a LONGER clip from the same bytes, so the estimate errs
# towards over-billing. A duration we actually know always wins over this.
_ASSUMED_AUDIO_BITS_PER_SECOND = Decimal("16000")

# Cached input bills at a tenth of the input rate. We set no cache parameters (one-shot
# extraction of distinct chunks has no stable prefix), so any hit is free upside we do not
# budget for — but the arithmetic still has to be right when automatic caching happens.
CACHED_INPUT_RATE = Decimal("0.1")

_PER_MILLION = Decimal("1000000")
# llm_runs.cost_usd is numeric(10,6); rounding here keeps the recorded row and the summed
# monthly budget in exact agreement.
_QUANTUM = Decimal("0.000001")


class UnpricedModelError(KeyError):
    """A model with no price cannot be billed, budgeted, or shipped."""

    def __init__(self, model: str, table: str = "PRICES") -> None:
        super().__init__(f"No price for model {model!r}. Add it to app.llm.pricing.{table}.")
        self.model = model


def cost_usd(
    model: str,
    input_tokens: int,
    output_tokens: int,
    cached_input_tokens: int = 0,
) -> Decimal:
    """Exact cost of one call.

    `input_tokens` INCLUDES `cached_input_tokens`, which is how the API reports usage.
    """
    try:
        input_price, output_price = PRICES[model]
    except KeyError:
        raise UnpricedModelError(model) from None
    fresh_input = max(input_tokens - cached_input_tokens, 0)
    total = (
        fresh_input * input_price
        + cached_input_tokens * input_price * CACHED_INPUT_RATE
        + output_tokens * output_price
    ) / _PER_MILLION
    return total.quantize(_QUANTUM, rounding=ROUND_HALF_UP)


def is_priced_audio_model(model: str) -> bool:
    """Ask BEFORE downloading audio or calling the API — an unpriced model must cost nothing."""
    return model in AUDIO_PRICES_PER_MINUTE


def audio_cost_usd(model: str, duration_seconds: float | Decimal) -> Decimal:
    """Exact cost of one transcription. THE INPUT IS SECONDS OF AUDIO, NOT TOKENS.

    There is no token count to pass: the transcription endpoint's default response carries
    text and nothing else. Anything that needs a duration it does not have should get one from
    `estimate_audio_seconds` and record that it was an estimate — never pass 0 to make the
    arithmetic go through, because 0 records a call that spent nothing.
    """
    try:
        per_minute = AUDIO_PRICES_PER_MINUTE[model]
    except KeyError:
        raise UnpricedModelError(model, "AUDIO_PRICES_PER_MINUTE") from None
    seconds = Decimal(str(duration_seconds))
    if seconds < 0:
        seconds = Decimal("0")
    return (seconds / _SECONDS_PER_MINUTE * per_minute).quantize(_QUANTUM, rounding=ROUND_HALF_UP)


def estimate_audio_seconds(byte_count: int) -> Decimal:
    """A duration inferred from file size, for BILLING ONLY when the real one is unknown.

    Never present this to a person or store it as the clip's length: it is an assumption about
    an encoder (`_ASSUMED_AUDIO_BITS_PER_SECOND`), chosen to over-estimate rather than
    under-charge, and it is wrong for any file WhatsApp did not encode.
    """
    bits = Decimal(max(byte_count, 0)) * 8
    return bits / _ASSUMED_AUDIO_BITS_PER_SECOND
