"""复盘边界的基础规则脱敏，不将买卖单价误作持仓总额。"""

import re
from decimal import Decimal


def _amount(match):
    amount = Decimal(match[2].replace(",", ""))
    amount *= {"": 1, "万": 10000, "亿": 100000000}[match[3] or ""]
    if amount <= 0:
        return match[1] + "[金额已脱敏]"
    magnitude = Decimal(10) ** amount.adjusted()
    rounded = (amount // magnitude) * magnitude
    unit, scale = ("亿", 100000000) if rounded >= 100000000 else ("万", 10000) if rounded >= 10000 else ("元", 1)
    return match[1] + f"{rounded / scale:f}{unit}+"


def redact_text(text: str) -> str:
    # 先处理长证件号，再处理手机号，避免截断长数字造成漏脱敏。
    text = re.sub(r"((?:银行卡号|银行卡|卡号)\s*[:：]?\s*)[0-9]{12,15}([0-9]{4})(?![0-9])", r"\1************\2", text)
    text = re.sub(r"(?<![0-9])([0-9]{4})[0-9]{10}([0-9]{3}[0-9Xx])(?![0-9])", r"\1**********\2", text)
    text = re.sub(r"(?<![0-9])[0-9]{12,15}([0-9]{4})(?![0-9])", r"************\1", text)
    text = re.sub(r"(?<![0-9])(1[3-9][0-9])[0-9]{4}([0-9]{4})(?![0-9])", r"\1****\2", text)
    text = re.sub(
        r"((?:姓名|户名|我叫)\s*[:：]?\s*)([\u4e00-\u9fff])([\u4e00-\u9fff]{1,3})(?=[，。；、\s]|$)",
        lambda m: m[1] + m[2] + "*" * len(m[3]), text,
    )
    text = re.sub(
        r"((?:持仓金额|持仓市值|账户余额|买入金额|卖出金额|成交金额|投入金额|总金额)\s*(?:为|是|约)?\s*[:：]?\s*)"
        r"([0-9][0-9,]*(?:\.[0-9]+)?)\s*([万亿]?)元?(?![0-9.,万亿元+])",
        _amount, text,
    )
    return text


def redact_value(value):
    if isinstance(value, str):
        return redact_text(value)
    if isinstance(value, list):
        return [redact_value(item) for item in value]
    if isinstance(value, dict):
        return {key: redact_value(item) for key, item in value.items()}
    return value
