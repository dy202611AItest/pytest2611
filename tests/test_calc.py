import pytest

from calc import add, divide


def test_add():
    assert add(1, 2) == 3


def test_add_negative():
    assert add(-1, 1) == 0


@pytest.mark.parametrize(
    ("a", "b", "expected"),
    [
        (6, 3, 2),
        (1, 2, 0.5),
    ],
)
def test_divide(a, b, expected):
    assert divide(a, b) == expected


def test_divide_by_zero_raises():
    with pytest.raises(ValueError, match="cannot divide by zero"):
        divide(1, 0)


@pytest.mark.smoke
def test_smoke_path():
    """最小冒烟链路：CI 里可用 -m smoke 快速验证环境是否可用。"""
    assert divide(add(1, 1), 2) == 1
