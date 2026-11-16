"""预算核算的货币与周期基础工具。

所有金额在系统内部一律换算为「最小货币单位」的整数（例如人民币 12.34 元 -> 1234 分），
分摊、冲销、汇总全部基于整数运算，杜绝二进制浮点带来的尾差；展示时再按币种精度还原。
"""
from __future__ import annotations

from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from typing import Any

# 各币种保留的小数位；未列出的币种默认 2 位。
CURRENCY_SCALE: dict[str, int] = {
    "CNY": 2,
    "USD": 2,
    "EUR": 2,
    "JPY": 0,
    "KRW": 0,
}

DEFAULT_SCALE = 2
# 比例允许的误差口径：精确到小数点后 6 位，避免字符串小数的二进制表示问题。
RATIO_QUANT = Decimal("0.000001")


def currency_scale(currency: str) -> int:
    return CURRENCY_SCALE.get(str(currency or "").strip().upper(), DEFAULT_SCALE)


def to_minor(amount: Any, currency: str) -> int | None:
    """把外部金额转换为最小货币单位整数；无法解析或为负时返回 None 表示金额无效。"""
    if amount is None or str(amount).strip() == "":
        return None
    try:
        value = Decimal(str(amount).strip())
    except (InvalidOperation, ValueError):
        return None
    if not value.is_finite() or value < 0:
        return None
    scale = currency_scale(currency)
    quant = Decimal(1).scaleb(-scale)
    return int(value.quantize(quant, rounding=ROUND_HALF_UP).scaleb(scale))


def to_decimal(minor_amount: int, currency: str) -> Decimal:
    """最小单位整数还原为十进制展示金额。"""
    scale = currency_scale(currency)
    return Decimal(int(minor_amount)).scaleb(-scale)


def format_amount(minor_amount: int, currency: str) -> str:
    """按币种精度格式化金额，供接口与附件展示。"""
    scale = currency_scale(currency)
    text = f"{to_decimal(minor_amount, currency):.{scale}f}"
    return f"{text} {str(currency or '').strip().upper()}"


def parse_ratio(value: Any) -> Decimal | None:
    """解析分摊比例；接受 0-1 小数或百分数字符串，非法值返回 None。"""
    if value is None or str(value).strip() == "":
        return None
    raw = str(value).strip()
    try:
        if raw.endswith("%"):
            ratio = Decimal(raw[:-1].strip()) / Decimal(100)
        else:
            ratio = Decimal(raw)
    except (InvalidOperation, ValueError):
        return None
    if not ratio.is_finite() or ratio < 0:
        return None
    return ratio.quantize(RATIO_QUANT, rounding=ROUND_HALF_UP)


def allocate_by_ratio(total_minor: int, ratios: list[Decimal]) -> list[int]:
    """按比例把总额拆成最小单位整数，最大余数法保证各份之和与原费用严格相等。

    1. 每份先向下取整；
    2. 计算取整产生的剩余单位；
    3. 按小数余数从大到小（并列时按行序）把剩余单位逐分补回。
    返回与 ratios 等长的整数列表，sum(返回值) == total_minor 恒成立。
    """
    parts = len(ratios)
    if parts == 0:
        return []
    ratio_sum = sum(ratios, Decimal(0))
    if ratio_sum == 0:
        # 全零比例时退化为平均分摊，仍保证闭合。
        base, remainder = divmod(total_minor, parts)
        return [base + (1 if i < remainder else 0) for i in range(parts)]

    raw = [Decimal(total_minor) * ratio / ratio_sum for ratio in ratios]
    floors = [int(part) for part in raw]  # Decimal -> int 为截断（向零），值均非负即向下取整
    remainder = total_minor - sum(floors)
    # 余数 = 原始值 - 向下取整值，越大说明该份越该先补一个最小单位。
    order = sorted(range(parts), key=lambda idx: (raw[idx] - floors[idx], -idx), reverse=True)
    result = list(floors)
    for idx in order[:remainder]:
        result[idx] += 1
    return result


def period_bounds(period: str) -> tuple[str, str] | None:
    """把预算周期标识解析为闭区间 [起始日期, 结束日期]。

    支持 ``YYYY``（年度）、``YYYY-Qn``（季度）、``YYYY-MM``（月度）三种口径；
    无法识别时返回 None，由调用方登记异常而不是静默纳入。
    """
    text = str(period or "").strip().upper()
    try:
        if "Q" in text:
            year_text, quarter_text = text.split("-Q", 1)
            year, quarter = int(year_text), int(quarter_text)
            if not 1 <= quarter <= 4:
                return None
            start_month = (quarter - 1) * 3 + 1
            end_month = start_month + 2
            return (f"{year:04d}-{start_month:02d}-01", f"{year:04d}-{end_month:02d}-{_month_end(year, end_month)}")
        if "-" in text:
            year_text, month_text = text.split("-", 1)
            year, month = int(year_text), int(month_text)
            if not 1 <= month <= 12:
                return None
            return (f"{year:04d}-{month:02d}-01", f"{year:04d}-{month:02d}-{_month_end(year, month)}")
        year = int(text)
        return (f"{year:04d}-01-01", f"{year:04d}-12-31")
    except (ValueError, IndexError):
        return None


def resolve_period(occur_date: str, granularity: str = "quarter") -> str | None:
    """根据费用发生日期反查所属预算周期标识。"""
    try:
        year_text, month_text, _day_text = str(occur_date).split("-", 2)
        year, month = int(year_text), int(month_text)
        if not 1 <= month <= 12:
            return None
    except (ValueError, AttributeError):
        return None
    if granularity == "month":
        return f"{year:04d}-{month:02d}"
    if granularity == "year":
        return f"{year:04d}"
    return f"{year:04d}-Q{(month - 1) // 3 + 1}"


def _month_end(year: int, month: int) -> int:
    if month == 12:
        next_month_first = f"{year + 1:04d}-01-01"
    else:
        next_month_first = f"{year:04d}-{month + 1:02d}-01"
    from datetime import date, timedelta

    first = date.fromisoformat(next_month_first)
    return (first - timedelta(days=1)).day
