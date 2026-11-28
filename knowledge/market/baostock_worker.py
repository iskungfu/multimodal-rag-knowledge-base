"""隔离BaoStock的进程级连接；只处理固定行情请求并输出JSON。"""

import contextlib
from datetime import date
import io
import json
import socket
import sys

FIELDS = "date,code,open,high,low,close,preclose,volume,amount,adjustflag,turn,tradestatus,pctChg,peTTM,pbMRQ,isST"


def collect(result, maximum=367):
    if result.error_code != "0":
        raise ValueError("来源请求失败")
    fields, rows = result.fields, []
    if not isinstance(fields, list) or not fields or len(set(fields)) != len(fields):
        raise ValueError("字段无效")
    while result.next():
        if result.error_code != "0":
            raise ValueError("来源分页失败")
        row = result.get_row_data()
        if len(row) != len(fields) or len(rows) >= maximum:
            raise ValueError("记录数量或字段无效")
        rows.append(dict(zip(fields, row)))
    if result.error_code != "0":
        raise ValueError("来源分页失败")
    return rows


def execute(request):
    from knowledge.market.models import market_symbol

    start, end = date.fromisoformat(request["start"]), date.fromisoformat(request["end"])
    if start > end or (end - start).days > 366 or request["action"] not in {"daily", "calendar"}:
        raise ValueError("请求范围无效")
    if request["action"] == "daily":
        symbol = market_symbol(request["code"])
        if symbol.startswith("bj"):
            raise ValueError("此来源未验证北交所")
        symbol = symbol[:2] + "." + symbol[2:]
    socket.setdefaulttimeout(5)
    import baostock as sdk

    try:
        if sdk.login().error_code != "0":
            raise ValueError("来源连接失败")
        if request["action"] == "calendar":
            return {"calendar": collect(sdk.query_trade_dates(start.isoformat(), end.isoformat()))}
        identity = collect(sdk.query_stock_basic(code=symbol), maximum=1)
        if len(identity) != 1 or identity[0].get("code") != symbol or identity[0].get("type") != "1":
            raise ValueError("证券身份无效")
        rows = collect(sdk.query_history_k_data_plus(
            symbol, FIELDS, start_date=start.isoformat(), end_date=end.isoformat(), frequency="d", adjustflag="3",
        ))
        return {"identity": identity[0], "quotes": rows}
    finally:
        sdk.logout()


def main():
    try:
        request = json.loads(sys.stdin.read(4096))
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            result = execute(request)
        response = {"ok": True, "data": result}
    except Exception:
        response = {"ok": False}
    sys.stdout.write(json.dumps(response, ensure_ascii=False))


if __name__ == "__main__":
    main()
