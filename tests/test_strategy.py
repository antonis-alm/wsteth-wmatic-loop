from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from unittest.mock import MagicMock

import pytest

from almanak.framework.market.errors import (
    BalanceUnavailableError,
    HealthUnavailableError,
    PriceUnavailableError,
)
from strategy import WstETHWmaticLoopStrategy


@dataclass
class FakeTokenBalance:
    balance: Decimal


@dataclass
class FakePositionHealth:
    health_factor: Decimal
    debt_value_usd: Decimal
    collateral_value_usd: Decimal


@pytest.fixture
def config() -> dict:
    return {
        "chain": "polygon",
        "collateral_token": "wstETH",
        "borrow_token": "USDC",
        "lending_protocol": "aave_v3",
        "lending_market": "aave_v3_polygon",
        "interest_rate_mode": "variable",
        "initial_supply_amount": "0.05",
        "hf_lower": "1.2",
        "hf_upper": "1.4",
        "target_hf_after_borrow": "1.3",
        "target_hf_after_repay": "1.3",
        "hf_safety_buffer": "0.03",
        "rebalance_cooldown_seconds": 300,
        "min_borrow_adjustment": "1",
        "min_repay_adjustment": "1",
        "max_borrow_step": "200",
        "max_repay_step": "200",
        "emergency_hf_floor": "1.15",
        "force_action": "",
    }


@pytest.fixture
def strategy(config: dict) -> WstETHWmaticLoopStrategy:
    return WstETHWmaticLoopStrategy(
        config=config,
        chain="polygon",
        wallet_address="0x" + "1" * 40,
    )


def _market(
    *,
    health: FakePositionHealth | None,
    collateral_balance: Decimal = Decimal("1"),
    borrow_balance: Decimal = Decimal("100"),
    borrow_price: Decimal = Decimal("1"),
) -> MagicMock:
    market = MagicMock()

    if health is None:
        market.position_health.side_effect = ValueError("no position")
    else:
        market.position_health.return_value = health

    def _balance(token: str):
        if token == "wstETH":
            return FakeTokenBalance(balance=collateral_balance)
        if token == "USDC":
            return FakeTokenBalance(balance=borrow_balance)
        raise ValueError(f"unknown token {token}")

    market.balance.side_effect = _balance
    market.price.side_effect = lambda token: borrow_price if token == "USDC" else Decimal("2500")
    return market


def test_bootstrap_supply_when_no_position_health(strategy: WstETHWmaticLoopStrategy) -> None:
    intent = strategy.decide(_market(health=None, collateral_balance=Decimal("1")))
    assert intent.intent_type.value == "SUPPLY"


def test_hold_if_no_position_and_not_enough_collateral(strategy: WstETHWmaticLoopStrategy) -> None:
    intent = strategy.decide(_market(health=None, collateral_balance=Decimal("0.01")))
    assert intent.intent_type.value == "HOLD"


def test_borrow_when_hf_above_band(strategy: WstETHWmaticLoopStrategy) -> None:
    health = FakePositionHealth(
        health_factor=Decimal("1.50"),
        debt_value_usd=Decimal("100"),
        collateral_value_usd=Decimal("220"),
    )
    intent = strategy.decide(_market(health=health))
    assert intent.intent_type.value == "BORROW"
    assert intent.borrow_amount > Decimal("0")


def test_hold_when_hf_in_band(strategy: WstETHWmaticLoopStrategy) -> None:
    health = FakePositionHealth(
        health_factor=Decimal("1.30"),
        debt_value_usd=Decimal("100"),
        collateral_value_usd=Decimal("220"),
    )
    intent = strategy.decide(_market(health=health))
    assert intent.intent_type.value == "HOLD"


def test_repay_when_hf_below_band(strategy: WstETHWmaticLoopStrategy) -> None:
    health = FakePositionHealth(
        health_factor=Decimal("1.10"),
        debt_value_usd=Decimal("100"),
        collateral_value_usd=Decimal("220"),
    )
    intent = strategy.decide(_market(health=health, borrow_balance=Decimal("50")))
    assert intent.intent_type.value == "REPAY"


def test_emergency_repay_full_when_hf_too_low(strategy: WstETHWmaticLoopStrategy) -> None:
    health = FakePositionHealth(
        health_factor=Decimal("1.12"),
        debt_value_usd=Decimal("100"),
        collateral_value_usd=Decimal("220"),
    )
    intent = strategy.decide(_market(health=health))
    assert intent.intent_type.value == "REPAY"
    assert intent.repay_full is True


def test_tiny_borrow_adjustment_holds(strategy: WstETHWmaticLoopStrategy, config: dict) -> None:
    config["min_borrow_adjustment"] = "50"
    config["max_borrow_step"] = "200"
    local_strategy = WstETHWmaticLoopStrategy(config=config, chain="polygon", wallet_address="0x" + "2" * 40)

    health = FakePositionHealth(
        health_factor=Decimal("1.401"),
        debt_value_usd=Decimal("100"),
        collateral_value_usd=Decimal("220"),
    )
    intent = local_strategy.decide(_market(health=health))
    assert intent.intent_type.value == "HOLD"


def test_cooldown_blocks_rebalance(strategy: WstETHWmaticLoopStrategy) -> None:
    strategy._last_rebalance_at = datetime.now(UTC)
    health = FakePositionHealth(
        health_factor=Decimal("1.50"),
        debt_value_usd=Decimal("100"),
        collateral_value_usd=Decimal("220"),
    )
    intent = strategy.decide(_market(health=health))
    assert intent.intent_type.value == "HOLD"


def test_force_action_supply(strategy: WstETHWmaticLoopStrategy) -> None:
    strategy.force_action = "supply"
    market = MagicMock()
    market.position_health.side_effect = HealthUnavailableError("health unavailable")
    intent = strategy.decide(market)
    assert intent.intent_type.value == "SUPPLY"


def test_force_action_borrow(strategy: WstETHWmaticLoopStrategy) -> None:
    strategy.force_action = "borrow"
    intent = strategy.decide(MagicMock())
    assert intent.intent_type.value == "BORROW"


def test_force_action_repay(strategy: WstETHWmaticLoopStrategy) -> None:
    strategy.force_action = "repay"
    intent = strategy.decide(MagicMock())
    assert intent.intent_type.value == "REPAY"


def test_price_unavailable_holds(strategy: WstETHWmaticLoopStrategy) -> None:
    health = FakePositionHealth(
        health_factor=Decimal("1.50"),
        debt_value_usd=Decimal("100"),
        collateral_value_usd=Decimal("220"),
    )
    market = _market(health=health)
    market.price.side_effect = PriceUnavailableError("USDC", "missing")
    intent = strategy.decide(market)
    assert intent.intent_type.value == "HOLD"


def test_balance_unavailable_holds(strategy: WstETHWmaticLoopStrategy) -> None:
    market = _market(health=None)
    market.balance.side_effect = BalanceUnavailableError("wstETH", "missing")
    intent = strategy.decide(market)
    assert intent.intent_type.value == "HOLD"


def test_teardown_intents_order(strategy: WstETHWmaticLoopStrategy) -> None:
    market = _market(
        health=FakePositionHealth(
            health_factor=Decimal("1.3"),
            debt_value_usd=Decimal("100"),
            collateral_value_usd=Decimal("200"),
        )
    )
    intents = strategy.generate_teardown_intents(mode=None, market=market)
    assert [intent.intent_type.value for intent in intents] == ["REPAY", "WITHDRAW"]


def test_get_open_positions_reports_borrow_and_supply(strategy: WstETHWmaticLoopStrategy) -> None:
    snapshot = _market(
        health=FakePositionHealth(
            health_factor=Decimal("1.3"),
            debt_value_usd=Decimal("100"),
            collateral_value_usd=Decimal("200"),
        )
    )
    strategy.create_market_snapshot = MagicMock(return_value=snapshot)

    summary = strategy.get_open_positions()
    position_types = [position.position_type.value for position in summary.positions]
    assert position_types == ["BORROW", "SUPPLY"]
