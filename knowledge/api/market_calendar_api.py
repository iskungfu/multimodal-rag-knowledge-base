"""只读交易日历，来源失败返回未知状态，不能被解释为休市。"""

from datetime import date

from fastapi import APIRouter, Depends, HTTPException

from knowledge.market.calendar import TradingCalendar, get_trading_calendar

router = APIRouter(prefix="/market/calendar", tags=["market"])


@router.get("")
def calendar_status(date: date | None = None, calendar: TradingCalendar = Depends(get_trading_calendar)):
    try:
        return calendar.status(date)
    except ValueError:
        raise HTTPException(422, "交易日历仅支持当前日期前后366天") from None
    except Exception:
        raise HTTPException(503, "交易日历服务暂不可用") from None
