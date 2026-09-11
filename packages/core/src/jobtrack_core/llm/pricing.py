"""List prices for the models this system calls, and the cost estimate built from them.

Every number here is published pricing, not a measurement. It carries the page it came
from and the date it was read, because Vertex pricing changes and a stale constant
produces a cost report that is confidently wrong.

The estimate is what goes into `llm_calls.estimated_cost_usd`. It is an estimate in the
strict sense: it prices the tokens Vertex reported at the rate published for the model
and endpoint, and it will not match an invoice, which also carries committed-use
discounts, taxes and free-tier offsets.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

#: Source for every rate below.
PRICING_SOURCE = "https://docs.cloud.google.com/vertex-ai/generative-ai/pricing"
#: The date the rates were read from that page.
PRICING_RETRIEVED = "2026-09-11"

_MILLION = Decimal(1_000_000)


@dataclass(frozen=True, slots=True)
class ModelRates:
    """USD per million tokens, on the `global` endpoint."""

    input_usd: Decimal
    output_usd: Decimal
    cached_input_usd: Decimal


#: Regional endpoints price the generative models 10% above these. `global` is what
#: `VERTEX_LOCATION` defaults to, and ADR 2 records why.
RATES: dict[str, ModelRates] = {
    "gemini-3.5-flash": ModelRates(
        input_usd=Decimal("1.50"),
        output_usd=Decimal("9.00"),
        cached_input_usd=Decimal("0.15"),
    ),
    "gemini-3.5-flash-lite": ModelRates(
        input_usd=Decimal("0.30"),
        output_usd=Decimal("2.50"),
        cached_input_usd=Decimal("0.03"),
    ),
    "gemini-embedding-001": ModelRates(
        input_usd=Decimal("0.15"),
        output_usd=Decimal("0"),
        cached_input_usd=Decimal("0"),
    ),
}

#: Multiplier applied when a call is routed to a regional endpoint rather than `global`.
REGIONAL_SURCHARGE = Decimal("1.10")

#: Backends that run in this process and call nothing. Priced at zero, which is true.
LOCAL_MODELS = frozenset({"heuristic", "cassette"})


def estimate_cost_usd(
    model: str,
    *,
    input_tokens: int,
    output_tokens: int,
    cached_input_tokens: int = 0,
    regional: bool = False,
) -> Decimal:
    """Price one call.

    Unknown models cost zero rather than raising. A model id that is absent from
    :data:`RATES` is almost always one that was just rotated in through configuration,
    and refusing to record the call at all would lose the token counts too — which are
    the part that cannot be reconstructed later.
    """
    if model in LOCAL_MODELS:
        return Decimal(0)

    rates = RATES.get(model)
    if rates is None:
        return Decimal(0)

    # Cached input is billed at its own lower rate, and Vertex reports it as a subset
    # of the prompt token count rather than in addition to it.
    billable_input = max(input_tokens - cached_input_tokens, 0)
    total = (
        Decimal(billable_input) * rates.input_usd
        + Decimal(cached_input_tokens) * rates.cached_input_usd
        + Decimal(output_tokens) * rates.output_usd
    ) / _MILLION

    if regional:
        total *= REGIONAL_SURCHARGE

    # Eight places to match the column. One extraction costs a fraction of a cent, and
    # rounding to four would record most calls as free.
    return total.quantize(Decimal("0.00000001"))
