"""Tiny pricing library used as the demo target."""


def apply_discount(price: float, percent: float) -> float:
    if percent < 0 or percent > 100:
        raise ValueError("percent must be between 0 and 100")
    return price - price * percent / 100


def shipping_cost(weight_kg: float, express: bool = False) -> float:
    if weight_kg <= 0:
        raise ValueError("weight must be positive")
    if weight_kg <= 1:
        cost = 4.0
    elif weight_kg <= 5:
        cost = 7.5
    else:
        cost = 7.5 + (weight_kg - 5) * 1.2
    if express:
        cost = cost * 2
    return round(cost, 2)


def is_free_shipping(order_total: float, member: bool) -> bool:
    return order_total >= 50 or (member and order_total >= 25)
