import math
from decimal import Decimal
from typing import Union

Number = Union[int, float, Decimal]


def floor_val(value: Number) -> int:
    """游戏中所有小数向下取整"""
    if isinstance(value, int):
        return value
    if isinstance(value, Decimal):
        return math.floor(value)
    # 防止 100 * 1.15 = 114.99999999999999 这类浮点误差
    return math.floor(Decimal(str(value)))


def floor_mul(value: Number, numerator: Number, denominator: Number = 1) -> int:
    """精确比例乘法后向下取整，例如 floor_mul(dmg, 100 + pct, 100)"""
    d_val = Decimal(str(value))
    d_num = Decimal(str(numerator))
    d_den = Decimal(str(denominator))
    if d_den == 0:
        raise ZeroDivisionError("Denominator cannot be zero")
    return math.floor((d_val * d_num) / d_den)


def floor_div(numerator: Number, denominator: Number) -> int:
    """精确除法向下取整"""
    return floor_mul(numerator, 1, denominator)
