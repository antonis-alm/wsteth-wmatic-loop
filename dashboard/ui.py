from decimal import Decimal
from typing import Any

import streamlit as st

from almanak.framework.dashboard.templates import (
    get_aave_v3_config,
    prepare_lending_session_state,
    render_lending_dashboard,
)


def _to_decimal(value: Any, default: str) -> Decimal:
    try:
        return Decimal(str(value))
    except Exception:
        return Decimal(default)


def _render_health_status(health_factor: Decimal, hf_lower: Decimal, hf_upper: Decimal, emergency_floor: Decimal) -> None:
    if health_factor <= emergency_floor:
        st.error("Health factor is below emergency floor. Strategy should prioritize repay actions.")
        return
    if health_factor < hf_lower:
        st.warning("Health factor is below the target band. Repay intent is expected.")
        return
    if health_factor > hf_upper:
        st.info("Health factor is above the target band. Borrow intent is expected.")
        return
    st.success("Health factor is inside the target band. Strategy should hold.")


def render_custom_dashboard(
    deployment_id: str,
    strategy_config: dict[str, Any],
    api_client: Any,
    session_state: dict[str, Any],
) -> None:
    st.title("wstETH / USDC Aave V3 Polygon HF Manager")

    collateral_token = str(strategy_config.get("collateral_token", "wstETH"))
    borrow_token = str(strategy_config.get("borrow_token", "USDC"))
    chain = str(strategy_config.get("chain", "polygon"))
    hf_lower = _to_decimal(strategy_config.get("hf_lower", "1.2"), "1.2")
    hf_upper = _to_decimal(strategy_config.get("hf_upper", "1.4"), "1.4")
    emergency_floor = _to_decimal(strategy_config.get("emergency_hf_floor", "1.15"), "1.15")

    health_factor = _to_decimal(session_state.get("health_factor", hf_upper), str(hf_upper))
    last_action = str(session_state.get("last_intent_type", session_state.get("last_action", "HOLD"))).upper()

    col1, col2, col3 = st.columns(3)
    col1.metric("HF Target Band", f"{hf_lower:.2f} - {hf_upper:.2f}")
    col2.metric("Current Health Factor", f"{health_factor:.2f}")
    col3.metric("Latest Intent", last_action)

    st.caption(
        f"{collateral_token} collateral / {borrow_token} debt on {chain} via Aave V3"
    )
    _render_health_status(health_factor, hf_lower, hf_upper, emergency_floor)

    lending_config = get_aave_v3_config(
        collateral_token=collateral_token,
        borrow_token=borrow_token,
        chain=chain,
    )
    lending_config.safe_threshold = float(hf_upper)
    lending_config.liquidation_threshold = float(emergency_floor)

    hydrated_session_state = dict(session_state)
    try:
        hydrated_session_state = prepare_lending_session_state(
            api_client,
            session_state=hydrated_session_state,
            config=lending_config,
            strategy_config=strategy_config,
        )
    except Exception:
        st.warning("Live lending position details are temporarily unavailable. Showing raw strategy state.")

    render_lending_dashboard(
        deployment_id,
        strategy_config,
        hydrated_session_state,
        lending_config,
    )
