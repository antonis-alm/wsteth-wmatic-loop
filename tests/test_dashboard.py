from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import MagicMock, patch

from dashboard.ui import render_custom_dashboard


def _mock_columns() -> tuple[MagicMock, MagicMock, MagicMock]:
    return (MagicMock(), MagicMock(), MagicMock())


def test_dashboard_imports() -> None:
    assert callable(render_custom_dashboard)


def test_render_uses_lending_template_with_hydrated_state() -> None:
    strategy_config = {
        "chain": "polygon",
        "collateral_token": "wstETH",
        "borrow_token": "USDC",
        "hf_lower": "1.2",
        "hf_upper": "1.4",
        "emergency_hf_floor": "1.15",
    }
    session_state = {"health_factor": "1.33", "last_intent_type": "hold"}

    api_client = object()
    template_config = MagicMock()
    hydrated_state = {"health_factor": "1.33", "collateral_amount": "0.05", "borrowed_amount": "100"}

    with (
        patch("dashboard.ui.get_aave_v3_config", return_value=template_config) as get_config,
        patch("dashboard.ui.prepare_lending_session_state", return_value=hydrated_state) as hydrate,
        patch("dashboard.ui.render_lending_dashboard") as render_template,
        patch("dashboard.ui.st.title"),
        patch("dashboard.ui.st.columns", return_value=_mock_columns()),
        patch("dashboard.ui.st.caption"),
        patch("dashboard.ui.st.success") as success,
        patch("dashboard.ui.st.info"),
        patch("dashboard.ui.st.warning"),
        patch("dashboard.ui.st.error"),
    ):
        render_custom_dashboard("dep-1", strategy_config, api_client, session_state)

    get_config.assert_called_once_with(
        collateral_token="wstETH",
        borrow_token="USDC",
        chain="polygon",
    )
    assert template_config.safe_threshold == 1.4
    assert template_config.liquidation_threshold == 1.15

    hydrate.assert_called_once_with(
        api_client,
        session_state=dict(session_state),
        config=template_config,
        strategy_config=strategy_config,
    )
    render_template.assert_called_once_with("dep-1", strategy_config, hydrated_state, template_config)
    success.assert_called_once()


def test_prepare_failure_falls_back_to_raw_state() -> None:
    strategy_config = {
        "chain": "polygon",
        "collateral_token": "wstETH",
        "borrow_token": "USDC",
        "hf_lower": "1.2",
        "hf_upper": "1.4",
        "emergency_hf_floor": "1.15",
    }
    session_state = {"health_factor": "1.10", "last_intent_type": "repay"}

    template_config = MagicMock()

    with (
        patch("dashboard.ui.get_aave_v3_config", return_value=template_config),
        patch("dashboard.ui.prepare_lending_session_state", side_effect=RuntimeError("gateway down")),
        patch("dashboard.ui.render_lending_dashboard") as render_template,
        patch("dashboard.ui.st.title"),
        patch("dashboard.ui.st.columns", return_value=_mock_columns()),
        patch("dashboard.ui.st.caption"),
        patch("dashboard.ui.st.success"),
        patch("dashboard.ui.st.info"),
        patch("dashboard.ui.st.warning") as warning,
        patch("dashboard.ui.st.error") as error,
    ):
        render_custom_dashboard("dep-2", strategy_config, object(), session_state)

    render_template.assert_called_once_with("dep-2", strategy_config, dict(session_state), template_config)
    warning.assert_called_once()
    error.assert_called_once()


def test_metadata_has_required_fields() -> None:
    metadata = json.loads(Path("dashboard/metadata.json").read_text())
    assert metadata["display_name"]
    assert metadata["description"]
    assert metadata["icon"]
