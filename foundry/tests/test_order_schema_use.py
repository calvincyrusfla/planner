"""Foundry's limits come from the generated schema (schema/order.json), not from hand-kept copies."""
from __future__ import annotations

from foundry import board_dispatch, builder_worker, order_schema


def test_order_details_cap_is_the_schema_builder_order_limit() -> None:
    assert builder_worker.MAX_ORDER_DETAILS_CHARS == order_schema.LIMIT_BUILDER_ORDER


def test_board_error_limit_is_the_schema_limit() -> None:
    assert board_dispatch.LIMIT_ERROR is order_schema.LIMIT_ERROR


def test_board_help_limit_is_the_schema_limit() -> None:
    assert board_dispatch.LIMIT_HELP is order_schema.LIMIT_HELP
