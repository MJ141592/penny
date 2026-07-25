"""Model prices, in one place, so a spend figure is never a guess.

Reasoning tokens bill as OUTPUT — at $30/Mtok they are the expensive half of an extraction
call, which is why the gateway notches reasoning effort down before it doubles a budget.
"""

from decimal import ROUND_HALF_UP, Decimal

# model -> ($ per million input tokens, $ per million output tokens)
PRICES: dict[str, tuple[Decimal, Decimal]] = {
    "gpt-5.5-2026-04-23": (Decimal("5"), Decimal("30")),
}

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

    def __init__(self, model: str) -> None:
        super().__init__(f"No price for model {model!r}. Add it to app.llm.pricing.PRICES.")
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
