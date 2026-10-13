"""Deliberately weak tests: they pass, but miss most bugs."""

from pricing import apply_discount, is_free_shipping, shipping_cost


def test_discount_basic():
    assert apply_discount(100, 10) == 90


def test_shipping_light_parcel():
    assert shipping_cost(0.5) == 4.0


def test_free_shipping_for_big_order():
    assert is_free_shipping(100, False) is True
