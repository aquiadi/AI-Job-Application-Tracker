"""What this system has actually spent, from `llm_calls`.

Every row is written by the one module that makes model calls, with the token counts
the provider reported and the cost priced from the published rates in
`jobtrack_core.llm.pricing`. So this is arithmetic over recorded calls, not a
projection — and it says so, because the rates are list prices and an invoice also
carries discounts, taxes and free-tier offsets.

Reads across every tenant, so it connects as the owner rather than the application
role. That is correct for an operator's report and would be wrong anywhere else; it is
a script an operator runs, not a code path a request can reach.
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from datetime import UTC, datetime, timedelta
from decimal import Decimal

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import create_async_engine

from jobtrack_core.config import get_settings
from jobtrack_core.db.models import LlmCall
from jobtrack_core.db.session import create_sessionmaker
from jobtrack_core.llm.pricing import PRICING_RETRIEVED, PRICING_SOURCE


def owner_url() -> str:
    settings = get_settings()
    db = settings.database
    return (
        f"postgresql+asyncpg://{db.owner_user}:{db.owner_password.get_secret_value()}"
        f"@{db.host}:{db.port}/{db.name}"
    )


async def report(days: int) -> int:
    since = datetime.now(UTC) - timedelta(days=days)
    engine = create_async_engine(owner_url())
    sessions = create_sessionmaker(engine)

    try:
        async with sessions() as session:
            rows = (
                await session.execute(
                    select(
                        LlmCall.model,
                        LlmCall.operation,
                        func.count().label("calls"),
                        func.sum(LlmCall.input_tokens),
                        func.sum(LlmCall.output_tokens),
                        func.sum(LlmCall.cached_input_tokens),
                        func.sum(LlmCall.estimated_cost_usd),
                        func.avg(LlmCall.latency_ms),
                    )
                    .where(LlmCall.created_at >= since)
                    .group_by(LlmCall.model, LlmCall.operation)
                    .order_by(func.sum(LlmCall.estimated_cost_usd).desc())
                )
            ).all()

            outcomes = (
                await session.execute(
                    select(LlmCall.outcome, func.count())
                    .where(LlmCall.created_at >= since)
                    .group_by(LlmCall.outcome)
                )
            ).all()
    finally:
        await engine.dispose()

    if not rows:
        print(f"No model calls recorded in the last {days} days.")
        print("Nothing has been spent, which is different from having no data.")
        return 0

    print(f"Model calls in the last {days} days\n")
    header = (
        f"{'model':24} {'operation':16} {'calls':>6} {'in':>10} "
        f"{'out':>9} {'mean ms':>8} {'USD':>11}"
    )
    print(header)
    print("-" * len(header))

    total = Decimal(0)
    total_calls = 0
    for model, operation, calls, tokens_in, tokens_out, _cached, cost, latency in rows:
        total += cost or Decimal(0)
        total_calls += calls
        print(
            f"{model:24} {operation.value:16} {calls:>6} {tokens_in or 0:>10,} "
            f"{tokens_out or 0:>9,} {int(latency or 0):>8,} {cost or Decimal(0):>11.6f}"
        )

    print("-" * len(header))
    print(f"{'total':24} {'':16} {total_calls:>6} {'':>10} {'':>9} {'':>8} {total:>11.6f}")

    if total_calls:
        print(f"\nMean cost per call: ${total / total_calls:.6f}")

    print("\nOutcomes: " + ", ".join(f"{outcome.value}={count}" for outcome, count in outcomes))
    print(f"\nRates: {PRICING_SOURCE} (read {PRICING_RETRIEVED}).")
    print(
        "These are list prices applied to recorded token counts. An invoice also "
        "carries discounts, taxes and free-tier offsets, so this will not match it."
    )
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--days", type=int, default=30, help="How far back to report")
    arguments = parser.parse_args()
    return asyncio.run(report(arguments.days))


if __name__ == "__main__":
    sys.exit(main())
