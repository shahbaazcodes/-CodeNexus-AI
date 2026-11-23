"""usage_meter.py — Usage tracking & cost guard (singleton).

Exposes the ``UsageMeter`` singleton with:

    reset(), record_usage(provider, model, input_tokens, output_tokens)
    check_limits(estimated_input_tokens=..., estimated_output_tokens=...)
    estimate_cost(model, input_tokens=..., output_tokens=...)
    get_summary()
    reserve(), commit(), release()  # concurrency reservation helpers

No real API calls are made; all HTTP interactions are mocked in tests.
"""

import os
import asyncio
from dataclasses import dataclass
from typing import Optional, Dict, Tuple


@dataclass
class UsageRecord:
    provider: str
    model: str
    input_tokens: int
    output_tokens: int


@dataclass
class UsageSummary:
    total_requests: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    total_tokens: int = 0
    estimated_total_cost_usd: float = 0.0
    limit_reached: bool = False
    limit_reason: str = ""
    est_max_cost_per_session: str = ""


DEFAULT_PRICING: Dict[str, Dict[str, float]] = {
    "gemini-3.6-flash": {"input": 0.075, "output": 0.30},
    "openai/gpt-oss-120b": {"input": 0.10, "output": 0.20},
}


class UsageMeter:
    """Singleton usage meter & cost guard."""

    def __init__(self) -> None:
        self.enabled = os.getenv("AI_USAGE_LIMIT_ENABLED", "1") == "1"
        self.max_requests_per_session = int(os.getenv("AI_MAX_REQUESTS_PER_SESSION", "50"))
        self.max_estimated_cost_per_session = float(os.getenv("AI_MAX_ESTIMATED_COST_PER_SESSION", "1.00"))
        self.max_tokens_per_request = int(os.getenv("AI_MAX_TOKENS_PER_REQUEST", "0"))

        self._records: list = []
        self._total_requests = 0
        self._input_tokens = 0
        self._output_tokens = 0
        self._total_tokens = 0
        self._estimated_cost_usd = 0.0
        self._reserved_requests = 0
        self._last_recorded_provider: Optional[str] = None

    def _load_pricing_from_env(self) -> Dict[str, Dict[str, float]]:
        pricing = dict(DEFAULT_PRICING)
        for model in list(pricing.keys()):
            env_slug = model.replace("-", "_").replace(".", "_").replace("/", "_").upper()
            inp_env = os.getenv(f"AI_PRICE_{env_slug}_INPUT")
            out_env = os.getenv(f"AI_PRICE_{env_slug}_OUTPUT")
            if inp_env:
                pricing[model]["input"] = float(inp_env)
            if out_env:
                pricing[model]["output"] = float(out_env)
        return pricing

    def _price_for(self, model: str, is_output: bool) -> float:
        pricing = self._load_pricing_from_env()
        rates = pricing.get(model)
        if not rates:
            return 0.0
        return rates["output"] if is_output else rates["input"]
    def estimate_cost(self, model: str, input_tokens: int = 0, output_tokens: int = 0) -> Tuple[float, float, float]:
        inp_rate = self._price_for(model, False)
        out_rate = self._price_for(model, True)
        input_cost = (input_tokens / 1_000_000) * inp_rate
        output_cost = (output_tokens / 1_000_000) * out_rate
        total_cost = input_cost + output_cost
        return round(input_cost, 6), round(output_cost, 6), round(total_cost, 6)

    def check_limits(self, estimated_input_tokens: int = 0, estimated_output_tokens: int = 0) -> Optional[str]:
        if not self.enabled:
            return None
        if self.max_tokens_per_request is not None and (estimated_input_tokens + estimated_output_tokens) > self.max_tokens_per_request:
            return "max_tokens_per_request"
        if self.max_requests_per_session is not None and (self._total_requests + self._reserved_requests) >= self.max_requests_per_session:
            return "max_requests_per_session"
        if self.max_estimated_cost_per_session is not None:
            next_cost = (estimated_input_tokens / 1_000_000) * self._price_for("gemini-3.6-flash", False) + \
                        (estimated_output_tokens / 1_000_000) * self._price_for("gemini-3.6-flash", True)
            if self._estimated_cost_usd + next_cost > self.max_estimated_cost_per_session:
                return "max_estimated_cost_per_session"
        return None

    def record_usage(self, provider: str, model: str, input_tokens: int, output_tokens: int) -> None:
        self._records.append(UsageRecord(provider, model, input_tokens, output_tokens))
        self._total_requests += 1
        self._input_tokens += input_tokens
        self._output_tokens += output_tokens
        self._total_tokens += input_tokens + output_tokens
        self._estimated_cost_usd += (
            (input_tokens / 1_000_000) * self._price_for(model, False) +
            (output_tokens / 1_000_000) * self._price_for(model, True)
        )
        self._last_recorded_provider = provider

    def get_summary(self) -> UsageSummary:
        total_req = self._total_requests + self._reserved_requests
        limit_reached = False
        limit_reason = ""
        if self.max_requests_per_session is not None and total_req >= self.max_requests_per_session:
            limit_reached = True
            limit_reason = "max_requests_per_session"
        elif self.max_estimated_cost_per_session is not None and self._estimated_cost_usd >= self.max_estimated_cost_per_session:
            limit_reached = True
            limit_reason = "max_estimated_cost_per_session"
        summary = UsageSummary(
            total_requests=total_req,
            input_tokens=self._input_tokens,
            output_tokens=self._output_tokens,
            total_tokens=self._total_tokens,
            estimated_total_cost_usd=round(self._estimated_cost_usd, 6),
            limit_reached=limit_reached,
            limit_reason=limit_reason,
            est_max_cost_per_session="estimate_only_not_actual_billing",
        )
        return summary

    def reset(self) -> None:
        self._records.clear()
        self._total_requests = 0
        self._input_tokens = 0
        self._output_tokens = 0
        self._total_tokens = 0
        self._estimated_cost_usd = 0.0
        self._reserved_requests = 0
        self._last_recorded_provider = None

    def _can_acquire_slot(self, est_input: int, est_output: int) -> Optional[str]:
        if not self.enabled:
            return None
        if self.max_tokens_per_request is not None and (est_input + est_output) > self.max_tokens_per_request:
            return "max_tokens_per_request"
        if self.max_requests_per_session is not None and (self._total_requests + self._reserved_requests) >= self.max_requests_per_session:
            return "max_requests_per_session"
        if self.max_estimated_cost_per_session is not None:
            next_cost = (est_input / 1_000_000) * self._price_for("gemini-3.6-flash", False) + \
                        (est_output / 1_000_000) * self._price_for("gemini-3.6-flash", True)
            if self._estimated_cost_usd + next_cost > self.max_estimated_cost_per_session:
                return "max_estimated_cost_per_session"
        return None

    async def reserve(self, estimated_input_tokens: int = 0, estimated_output_tokens: int = 0) -> bool:
        if self._can_acquire_slot(estimated_input_tokens, estimated_output_tokens) is not None:
            return False
        self._reserved_requests += 1
        return True

    async def commit(self) -> None:
        """Finalize a reservation after a successful provider call.

        Usage is already recorded via ``record_usage`` (either inside
        ``call_gemini_raw`` or the Groq path in ``call_llm``).  Commit
        simply consumes the reserved slot.
        """
        if self._reserved_requests > 0:
            self._reserved_requests -= 1

    async def release(self) -> None:
        """Release a reserved slot without recording usage (provider call failed)."""
        if self._reserved_requests > 0:
            self._reserved_requests -= 1

    def __repr__(self) -> str:
        return (f"UsageMeter(enabled={self.enabled}, max_requests={self.max_requests_per_session}, "
                f"max_cost={self.max_estimated_cost_per_session}, max_tokens={self.max_tokens_per_request})")


usage_meter = UsageMeter()


async def reserve_slot_async(est_input: int = 0, est_output: int = 0) -> bool:
    return await usage_meter.reserve(est_input, est_output)


async def commit_slot_async() -> None:
    await usage_meter.commit()


async def release_slot_async() -> None:
    usage_meter.release()


if __name__ == "__main__":
    u = UsageMeter()
    print(u)
    u.record_usage("gemini", "gemini-3.6-flash", 10, 5)
    print(u.get_summary())