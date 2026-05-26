from __future__ import annotations

import logging
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

from almanak.framework.intents import Intent
from almanak.framework.market import MarketSnapshot
from almanak.framework.market.errors import (
    BalanceUnavailableError,
    HealthUnavailableError,
    PriceUnavailableError,
)
from almanak.framework.strategies import IntentStrategy, almanak_strategy

logger = logging.getLogger(__name__)


@almanak_strategy(
    name="wst_e_t_h_wmatic_loop",
    description="Aave V3 Polygon health-factor band manager for wstETH/USDC",
    version="1.0.0",
    author="Almanak",
    tags=["lending", "aave_v3", "polygon", "health-factor"],
    supported_chains=["polygon"],
    supported_protocols=["aave_v3"],
    intent_types=["SUPPLY", "BORROW", "REPAY", "WITHDRAW", "HOLD"],
    default_chain="polygon",
)
class WstETHWmaticLoopStrategy(IntentStrategy):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)

        self.collateral_token = str(self.get_config("collateral_token", "wstETH"))
        self.borrow_token = str(self.get_config("borrow_token", "USDC"))
        self.lending_protocol = str(self.get_config("lending_protocol", "aave_v3"))
        self.lending_market = str(self.get_config("lending_market", "aave_v3_polygon"))
        self.interest_rate_mode = str(self.get_config("interest_rate_mode", "variable"))

        self.initial_supply_amount = Decimal(str(self.get_config("initial_supply_amount", "0.05")))

        self.hf_lower = Decimal(str(self.get_config("hf_lower", "1.2")))
        self.hf_upper = Decimal(str(self.get_config("hf_upper", "1.4")))
        self.target_hf_after_borrow = Decimal(str(self.get_config("target_hf_after_borrow", "1.3")))
        self.target_hf_after_repay = Decimal(str(self.get_config("target_hf_after_repay", "1.3")))
        self.hf_safety_buffer = Decimal(str(self.get_config("hf_safety_buffer", "0.03")))
        self.emergency_hf_floor = Decimal(str(self.get_config("emergency_hf_floor", "1.15")))

        self.rebalance_cooldown_seconds = int(self.get_config("rebalance_cooldown_seconds", 300))
        self.min_borrow_adjustment = Decimal(str(self.get_config("min_borrow_adjustment", "1")))
        self.min_repay_adjustment = Decimal(str(self.get_config("min_repay_adjustment", "1")))
        self.max_borrow_step = Decimal(str(self.get_config("max_borrow_step", "200")))
        self.max_repay_step = Decimal(str(self.get_config("max_repay_step", "200")))

        self.force_action = str(self.get_config("force_action", "") or "").lower()

        if not (self.hf_lower > Decimal("1.0") and self.hf_upper > self.hf_lower):
            raise ValueError("hf bounds are invalid")
        if self.target_hf_after_borrow <= self.hf_lower + self.hf_safety_buffer:
            raise ValueError("target_hf_after_borrow must stay above lower bound safety buffer")
        if self.target_hf_after_repay <= self.hf_lower + self.hf_safety_buffer:
            raise ValueError("target_hf_after_repay must stay above lower bound safety buffer")
        if self.target_hf_after_repay >= self.hf_upper:
            raise ValueError("target_hf_after_repay must be below hf_upper")
        if self.target_hf_after_borrow >= self.hf_upper:
            raise ValueError("target_hf_after_borrow must be below hf_upper")
        if self.emergency_hf_floor >= self.hf_lower:
            raise ValueError("emergency_hf_floor must be below hf_lower")
        if self.initial_supply_amount <= Decimal("0"):
            raise ValueError("initial_supply_amount must be positive")

        self._last_rebalance_at: datetime | None = None
        self._has_supplied = False

    def _position_health(self, market: MarketSnapshot):
        return market.position_health(protocol=self.lending_protocol, market_id=self.lending_market)

    def _in_cooldown(self) -> bool:
        if self._last_rebalance_at is None:
            return False
        return (datetime.now(UTC) - self._last_rebalance_at).total_seconds() < self.rebalance_cooldown_seconds

    def _safe_target_hf_for_borrow(self) -> Decimal:
        return max(self.target_hf_after_borrow, self.hf_lower + self.hf_safety_buffer)

    def _safe_target_hf_for_repay(self) -> Decimal:
        return max(self.target_hf_after_repay, self.hf_lower + self.hf_safety_buffer)

    def _borrow_delta_tokens(self, *, hf: Decimal, debt_usd: Decimal, borrow_price: Decimal) -> Decimal:
        debt_constant = hf * debt_usd
        target_hf = self._safe_target_hf_for_borrow()
        target_debt_usd = debt_constant / target_hf
        delta_usd = max(Decimal("0"), target_debt_usd - debt_usd)
        return delta_usd / borrow_price

    def _repay_delta_tokens(self, *, hf: Decimal, debt_usd: Decimal, borrow_price: Decimal) -> Decimal:
        debt_constant = hf * debt_usd
        target_hf = self._safe_target_hf_for_repay()
        target_debt_usd = debt_constant / target_hf
        delta_usd = max(Decimal("0"), debt_usd - target_debt_usd)
        return delta_usd / borrow_price

    def _forced_intent(self, market: MarketSnapshot) -> Intent:
        if self.force_action == "supply":
            return Intent.supply(
                protocol=self.lending_protocol,
                token=self.collateral_token,
                amount=self.initial_supply_amount,
                use_as_collateral=True,
                chain=self.chain,
            )

        if self.force_action == "borrow":
            force_amount = max(self.min_borrow_adjustment, self.max_borrow_step)
            return Intent.borrow(
                protocol=self.lending_protocol,
                collateral_token=self.collateral_token,
                collateral_amount=Decimal("0"),
                borrow_token=self.borrow_token,
                borrow_amount=force_amount,
                interest_rate_mode=self.interest_rate_mode,
                chain=self.chain,
            )

        if self.force_action == "repay":
            force_amount = max(self.min_repay_adjustment, self.max_repay_step)
            return Intent.repay(
                protocol=self.lending_protocol,
                token=self.borrow_token,
                amount=force_amount,
                interest_rate_mode=self.interest_rate_mode,
                chain=self.chain,
            )

        raise ValueError(f"Unknown force_action: {self.force_action!r}")

    def decide(self, market: MarketSnapshot) -> Intent:
        if self.force_action:
            return self._forced_intent(market)

        try:
            health = self._position_health(market)
        except (HealthUnavailableError, ValueError):
            health = None

        try:
            collateral_balance = market.balance(self.collateral_token).balance
        except BalanceUnavailableError:
            return Intent.hold(reason=f"{self.collateral_token} balance unavailable")

        if health is None:
            if self._has_supplied:
                return Intent.hold(reason="position health unavailable")
            if collateral_balance < self.initial_supply_amount:
                return Intent.hold(
                    reason=f"insufficient {self.collateral_token} for initial supply"
                )
            return Intent.supply(
                protocol=self.lending_protocol,
                token=self.collateral_token,
                amount=self.initial_supply_amount,
                use_as_collateral=True,
                chain=self.chain,
            )

        hf = Decimal(str(health.health_factor))
        debt_usd = Decimal(str(getattr(health, "debt_value_usd", Decimal("0"))))
        collateral_usd = Decimal(str(getattr(health, "collateral_value_usd", Decimal("0"))))

        if collateral_usd > Decimal("0"):
            self._has_supplied = True

        if debt_usd <= Decimal("0"):
            return Intent.hold(reason="no debt yet")

        if hf <= self.emergency_hf_floor:
            return Intent.repay(
                protocol=self.lending_protocol,
                token=self.borrow_token,
                repay_full=True,
                interest_rate_mode=self.interest_rate_mode,
                chain=self.chain,
            )

        if self._in_cooldown():
            return Intent.hold(reason="rebalance cooldown active")

        try:
            borrow_price = Decimal(str(market.price(self.borrow_token)))
        except PriceUnavailableError:
            return Intent.hold(reason=f"{self.borrow_token} price unavailable")

        if borrow_price <= Decimal("0"):
            return Intent.hold(reason=f"invalid {self.borrow_token} price")

        if hf > self.hf_upper:
            borrow_amount = self._borrow_delta_tokens(hf=hf, debt_usd=debt_usd, borrow_price=borrow_price)
            borrow_amount = min(borrow_amount, self.max_borrow_step)
            if borrow_amount < self.min_borrow_adjustment:
                return Intent.hold(reason="borrow adjustment below minimum")
            return Intent.borrow(
                protocol=self.lending_protocol,
                collateral_token=self.collateral_token,
                collateral_amount=Decimal("0"),
                borrow_token=self.borrow_token,
                borrow_amount=borrow_amount,
                interest_rate_mode=self.interest_rate_mode,
                chain=self.chain,
            )

        if hf < self.hf_lower:
            repay_amount = self._repay_delta_tokens(hf=hf, debt_usd=debt_usd, borrow_price=borrow_price)
            repay_amount = min(repay_amount, self.max_repay_step)
            if repay_amount < self.min_repay_adjustment:
                return Intent.hold(reason="repay adjustment below minimum")
            try:
                wallet_borrow = market.balance(self.borrow_token).balance
            except BalanceUnavailableError:
                return Intent.hold(reason=f"{self.borrow_token} balance unavailable")
            repay_amount = min(repay_amount, Decimal(str(wallet_borrow)))
            if repay_amount < self.min_repay_adjustment:
                return Intent.hold(reason=f"insufficient {self.borrow_token} to repay")
            return Intent.repay(
                protocol=self.lending_protocol,
                token=self.borrow_token,
                amount=repay_amount,
                interest_rate_mode=self.interest_rate_mode,
                chain=self.chain,
            )

        return Intent.hold(reason="HF in target band")

    def on_intent_executed(self, intent: Intent, success: bool, result: Any) -> None:
        if not success:
            return
        if intent.intent_type.value in {"SUPPLY", "BORROW", "REPAY"}:
            self._last_rebalance_at = datetime.now(UTC)
        if intent.intent_type.value == "SUPPLY":
            self._has_supplied = True

    def get_persistent_state(self) -> dict[str, str]:
        return {
            "has_supplied": str(self._has_supplied),
            "last_rebalance_at": self._last_rebalance_at.isoformat() if self._last_rebalance_at else "",
        }

    def load_persistent_state(self, state: dict[str, str]) -> None:
        if not state:
            return
        self._has_supplied = str(state.get("has_supplied", "False")).lower() == "true"
        raw_ts = str(state.get("last_rebalance_at", ""))
        self._last_rebalance_at = datetime.fromisoformat(raw_ts) if raw_ts else None

    def supports_teardown(self) -> bool:
        return True

    def get_open_positions(self):
        from almanak.framework.teardown import PositionInfo, PositionType, TeardownPositionSummary

        positions = []
        snapshot = self.create_market_snapshot()
        try:
            health = self._position_health(snapshot)
        except (HealthUnavailableError, ValueError):
            health = None

        if health is not None:
            debt_usd = Decimal(str(getattr(health, "debt_value_usd", Decimal("0"))))
            collateral_usd = Decimal(str(getattr(health, "collateral_value_usd", Decimal("0"))))
            if debt_usd > Decimal("0"):
                positions.append(
                    PositionInfo(
                        position_type=PositionType.BORROW,
                        position_id=f"{self.STRATEGY_NAME}-borrow",
                        chain=self.chain,
                        protocol=self.lending_protocol,
                        value_usd=debt_usd,
                        details={"asset": self.borrow_token},
                    )
                )
            if collateral_usd > Decimal("0"):
                positions.append(
                    PositionInfo(
                        position_type=PositionType.SUPPLY,
                        position_id=f"{self.STRATEGY_NAME}-supply",
                        chain=self.chain,
                        protocol=self.lending_protocol,
                        value_usd=collateral_usd,
                        details={"asset": self.collateral_token},
                    )
                )

        return TeardownPositionSummary(
            deployment_id=getattr(self, "deployment_id", self.STRATEGY_NAME),
            timestamp=datetime.now(UTC),
            positions=positions,
        )

    def generate_teardown_intents(self, mode, market: MarketSnapshot | None = None) -> list[Intent]:
        intents: list[Intent] = []

        snapshot = market if market is not None else self.create_market_snapshot()
        try:
            health = self._position_health(snapshot)
        except (HealthUnavailableError, ValueError):
            return intents

        debt_usd = Decimal(str(getattr(health, "debt_value_usd", Decimal("0"))))
        collateral_usd = Decimal(str(getattr(health, "collateral_value_usd", Decimal("0"))))

        if debt_usd > Decimal("0"):
            intents.append(
                Intent.repay(
                    protocol=self.lending_protocol,
                    token=self.borrow_token,
                    repay_full=True,
                    interest_rate_mode=self.interest_rate_mode,
                    chain=self.chain,
                )
            )

        if collateral_usd > Decimal("0"):
            intents.append(
                Intent.withdraw(
                    protocol=self.lending_protocol,
                    token=self.collateral_token,
                    amount=Decimal("0"),
                    withdraw_all=True,
                    chain=self.chain,
                )
            )

        return intents
