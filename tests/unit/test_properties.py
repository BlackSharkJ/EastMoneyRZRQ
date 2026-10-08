"""hypothesis 属性测试: 上色映射, 坐标轴放宽与金额缩放的数学性质.

每个属性最多 40 例, 兼顾覆盖与 CI 时间.
"""

import math

import polars as pl
import pytest
from hypothesis import HealthCheck, assume, given, settings
from hypothesis import strategies as st

from eastmoneyrzrq import charts

# 单个属性最多 40 例, 避免 CI 太慢
FAST = settings(max_examples=40, deadline=None)
# 排除十进制平局点时会过滤掉少量样本, 单独关掉 filter_too_much 健康检查
FAST_WITH_FILTER = settings(
    max_examples=40,
    deadline=None,
    suppress_health_check=[HealthCheck.filter_too_much],
)


def _has_decimal_tie(value: float, divisor: float) -> bool:
    """判断 value / divisor 的第 3 位小数是否是恰好为 5 的平局点.

    polars 的 round 按十进制最短表示做 half_to_even, Python 的 round 按二进制精确值做
    half_to_even, 两者只在十进制平局点 (例如 0.005, 2.675) 上可能差一分钱. 属性测试
    排除这些点, 才能直接用 Python 的 round 写出期望值.
    """
    scaled = value / divisor
    if scaled == 0.0 or not math.isfinite(scaled):
        return False
    cents = scaled * 100.0
    return abs(cents - math.floor(cents) - 0.5) < 1e-6


_SIGN_VALUES = st.lists(
    st.floats(min_value=-1e6, max_value=1e6, allow_nan=False, allow_infinity=False),
    min_size=1,
    max_size=8,
)
_SCALE_VALUES = st.lists(
    st.floats(min_value=-1e9, max_value=1e9, allow_nan=False, allow_infinity=False),
    min_size=1,
    max_size=5,
)
_NON_NEGATIVE = st.floats(min_value=0, max_value=1e6, allow_nan=False, allow_infinity=False)
_DIVISORS = st.floats(min_value=0.01, max_value=1e9, allow_nan=False, allow_infinity=False)


# --------------------------------------------------------------------------- #
# sign_colored_data
# --------------------------------------------------------------------------- #


@FAST
@given(values=_SIGN_VALUES, positive_is_up=st.booleans())
def test_sign_colored_data_length_and_values(values: list[float], positive_is_up: bool) -> None:
    colored = charts.sign_colored_data(values, positive_is_up=positive_is_up)
    assert len(colored) == len(values)
    # value 原样透传, 没有算术运算, 所以可以直接用 == 逐个比较
    assert [item["value"] for item in colored] == values
    for item in colored:
        assert set(item.keys()) == {"value", "itemStyle"}


@FAST
@given(values=_SIGN_VALUES, positive_is_up=st.booleans())
def test_sign_colored_data_only_uses_two_colors(values: list[float], positive_is_up: bool) -> None:
    colored = charts.sign_colored_data(values, positive_is_up=positive_is_up)
    colors = {item["itemStyle"]["color"] for item in colored}
    assert colors <= {charts.UP_COLOR, charts.DOWN_COLOR}
    assert all(set(item["itemStyle"].keys()) == {"color"} for item in colored)


@FAST
@given(values=_SIGN_VALUES)
def test_sign_colored_data_maps_non_negative_to_up_color(values: list[float]) -> None:
    colored = charts.sign_colored_data(values, positive_is_up=True)
    for value, item in zip(values, colored, strict=True):
        expected = charts.UP_COLOR if value >= 0 else charts.DOWN_COLOR
        assert item["itemStyle"]["color"] == expected


@FAST
@given(values=_SIGN_VALUES)
def test_sign_colored_data_direction_is_mirrored(values: list[float]) -> None:
    up = charts.sign_colored_data(values, positive_is_up=True)
    down = charts.sign_colored_data(values, positive_is_up=False)
    for up_item, down_item in zip(up, down, strict=True):
        up_color = up_item["itemStyle"]["color"]
        down_color = down_item["itemStyle"]["color"]
        expected = charts.DOWN_COLOR if up_color == charts.UP_COLOR else charts.UP_COLOR
        assert down_color == expected


# --------------------------------------------------------------------------- #
# axis_bounds
# --------------------------------------------------------------------------- #


@FAST
@given(values=st.lists(_NON_NEGATIVE, min_size=1, max_size=6))
def test_axis_bounds_lands_on_half_thousand_grid(values: list[float]) -> None:
    lower, upper = charts.axis_bounds(values)
    assert lower % 1000 == 500
    assert upper % 1000 == 500
    assert lower <= upper


@FAST
@given(
    values=st.lists(_NON_NEGATIVE, min_size=1, max_size=6),
    extra=_NON_NEGATIVE,
)
def test_axis_bounds_upper_is_monotonic_in_max(values: list[float], extra: float) -> None:
    fixed_min = min(values)
    axis_min, upper_small = charts.axis_bounds([fixed_min, max(values)])
    axis_min_big, upper_big = charts.axis_bounds([fixed_min, max(values) + extra])
    assert axis_min == axis_min_big
    assert upper_big >= upper_small


# --------------------------------------------------------------------------- #
# scaled_values
# --------------------------------------------------------------------------- #


@FAST_WITH_FILTER
@given(values=_SCALE_VALUES, divisor=_DIVISORS)
def test_scaled_values_matches_python_round(values: list[float], divisor: float) -> None:
    assume(all(not _has_decimal_tie(value, divisor) for value in values))
    result = charts.scaled_values(pl.DataFrame({"v": values}), "v", divisor)
    assert result == [round(value / divisor, 2) for value in values]


@FAST
@given(values=_SCALE_VALUES, divisor=_DIVISORS)
def test_scaled_values_rounds_to_cent(values: list[float], divisor: float) -> None:
    result = charts.scaled_values(pl.DataFrame({"v": values}), "v", divisor)
    assert len(result) == len(values)
    for value, scaled in zip(values, result, strict=True):
        # 结果必须落在两位小数的格点上
        assert scaled == round(scaled, 2)
        # 且与真实缩放值的偏差不超过半分钱 (0.005) 加上浮点误差余量
        assert scaled == pytest.approx(value / divisor, abs=0.0051)
