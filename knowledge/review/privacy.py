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


def _redact_inline(text: str) -> str:
    formatting = r"(?:[\s*_`~【】]|(?i:</?(?:strong|b|em|i|span|td|th)(?:\s[^>]*?)?>))*"
    text = re.sub(r"((?:银行卡号|银行卡|卡号)" + formatting + r"[:：|]?" + formatting + r")[0-9]{12,15}([0-9]{4})(?![0-9])", r"\1************\2", text)
    text = re.sub(r"(?<![0-9])([0-9]{4})[0-9]{10}([0-9]{3}[0-9Xx])(?![0-9])", r"\1**********\2", text)
    text = re.sub(r"(?<![0-9])[0-9]{12,15}([0-9]{4})(?![0-9])", r"************\1", text)
    text = re.sub(r"(?<![0-9])(1[3-9][0-9])[0-9]{4}([0-9]{4})(?![0-9])", r"\1****\2", text)
    text = re.sub(
        r"((?:姓名|户名|我叫)" + formatting + r"[:：|]?" + formatting + r")([\u4e00-\u9fff])([\u4e00-\u9fff]{1,3})(?=[，。；、\s*_`~|<]|$)",
        lambda match: match[1] + match[2] + "*" * len(match[3]), text,
    )
    text = re.sub(
        r"((?:持仓金额|持仓市值|账户余额|买入金额|卖出金额|成交金额|投入金额|总金额)" + formatting + r"(?:为|是|约)?" + formatting + r"[:：|]?" + formatting + r")"
        r"([0-9][0-9,]*(?:\.[0-9]+)?)\s*([万亿]?)元?(?![0-9.,万亿元+])",
        _amount, text,
    )
    return text


def redact_text(text: str) -> str:
    field_labels = {"姓名", "户名", "我叫", "银行卡号", "银行卡", "卡号", "持仓金额", "持仓市值", "账户余额", "买入金额", "卖出金额", "成交金额", "投入金额", "总金额"}
    if "<table" in text.lower():
        from bs4 import BeautifulSoup

        def replace_table(match):
            table = BeautifulSoup(match[0], "html.parser")
            rows = [[cell.get_text(" ", strip=True).replace("|", " ") for cell in row.find_all(["td", "th"])]
                    for row in table.find_all("tr")]
            rows = [row for row in rows if row]
            if not rows:
                return table.get_text(" ", strip=True)
            labels = field_labels | {"买入单价", "卖出单价", "单价", "代码", "股票代码", "日期"}
            if all(len(row) == 2 and row[0].strip("：:") in labels for row in rows):
                return "\n" + "\n".join(label + "：" + value for label, value in rows) + "\n"
            lines = ["| " + " | ".join(row) + " |" for row in rows]
            lines.insert(1, "| " + " | ".join("---" for cell in rows[0]) + " |")
            return "\n" + "\n".join(lines) + "\n"

        text = re.sub(r"<table\b[^>]*>.*?</table\s*>", replace_table, text, flags=re.I | re.S)
    lines = text.splitlines(keepends=True)
    headers = None
    header_lines = set()

    def cells_of(line):
        return line.strip().strip("|").split("|")

    def label_of(cell):
        return re.sub(r"[*_`~【】]", "", cell).strip().rstrip("：:")

    def redact_cells(cells, labels=()):
        result = [_redact_inline(cell) for cell in cells]
        for cell_index, label in enumerate(labels[:len(cells)]):
            if label in field_labels:
                prefix = label + "："
                result[cell_index] = _redact_inline(prefix + result[cell_index])[len(prefix):]
        for cell_index in range(len(cells) - 1):
            label = label_of(cells[cell_index])
            if label in field_labels and label_of(cells[cell_index + 1]) not in field_labels:
                prefix = label + "："
                result[cell_index + 1] = _redact_inline(prefix + result[cell_index + 1])[len(prefix):]
        return "|" + "|".join(result) + "|"

    for index, line in enumerate(lines):
        if "|" not in line:
            headers = None
            continue
        cells = cells_of(line)
        populated = [cell.strip() for cell in cells if cell.strip()]
        if populated and all(re.fullmatch(r":?-{3,}:?", cell) for cell in populated):
            previous = cells_of(lines[index - 1]) if index else []
            headers = [label_of(cell) for cell in previous]
            if index:
                ending = lines[index - 1][len(lines[index - 1].rstrip("\r\n")):]
                lines[index - 1] = redact_cells(previous) + ending
            header_lines.update({index - 1, index})
            continue
        if headers is not None:
            ending = line[len(line.rstrip("\r\n")):]
            lines[index] = redact_cells(cells, headers) + ending
            header_lines.add(index)
    result, pending = [], []
    for index, line in enumerate(lines):
        if index in header_lines:
            result.extend([_redact_inline("".join(pending)), line])
            pending = []
        else:
            pending.append(line)
    result.append(_redact_inline("".join(pending)))
    return "".join(result)


def redact_value(value):
    if isinstance(value, str):
        return redact_text(value)
    if isinstance(value, list):
        return [redact_value(item) for item in value]
    if isinstance(value, dict):
        identifiers = {"_id", "id", "task_id", "owner_id", "document_id", "review_id", "session_id", "chunk_id"}
        return {key: item if key in identifiers else redact_value(item) for key, item in value.items()}
    return value
