"""全市场收盘采集：持久进度、互斥租约、有限重试及真实日期核验。"""

from datetime import datetime, timedelta, timezone
import threading
import time
from uuid import uuid4

from pymongo import ReturnDocument
from pymongo.errors import DuplicateKeyError

from knowledge.market.batch import BatchQuotes
from knowledge.market.models import DailyQuote, SHANGHAI, date_window, security_name
from knowledge.market.universe import MarketUniverse


class CollectionBusy(RuntimeError):
    pass


class CollectionStopped(RuntimeError):
    pass


class CollectionRepository:
    def __init__(self, runs, locks):
        self.runs, self.locks = runs, locks
        self.runs.create_index("updated_at")

    def recover(self, now):
        for record in self.runs.find({"status": "running", "lease_until": {"$lte": now}}):
            if self.locks.find_one({"_id": "market", "token": record["token"], "lease_until": {"$gt": now}}):
                continue
            completed = set(record.get("completed_codes", []))
            failures = [{"code": item["code"], "reason": "进程中断，尚未确认已保存"}
                        for item in record.get("universe", []) if item["code"] not in completed]
            self.runs.update_one({"_id": record["_id"], "token": record["token"], "status": "running", "lease_until": {"$lte": now}}, {
                "$set": {"status": "failed", "error": "采集进程中断或租约过期，可重试未完成证券", "updated_at": now,
                         "failures": failures, "failed": len(failures), "pending": 0},
            })

    def claim(self, day, now, retry=False):
        self.recover(now)
        previous = self.runs.find_one({"_id": day.isoformat()})
        if previous and previous["status"] == "completed" and not retry:
            return None
        token = uuid4().hex
        lease = now + timedelta(seconds=180)
        try:
            lock = self.locks.find_one_and_update({"_id": "market", "lease_until": {"$lte": now}}, {
                "$set": {"token": token, "lease_until": lease},
            }, upsert=True, return_document=ReturnDocument.AFTER)
        except DuplicateKeyError:
            raise CollectionBusy("全市场采集正在进行，请查看进度") from None
        if not lock:
            raise CollectionBusy("全市场采集正在进行")
        try:
            query = {"_id": day.isoformat(), "token": previous["token"] if previous else {"$exists": False}}
            updated = self.runs.update_one(query, {
                "$set": {"date": day.isoformat(), "status": "running", "token": token, "lease_until": lease,
                         "error": "", "updated_at": now},
                "$setOnInsert": {"created_at": now, "total": 0, "succeeded": 0, "failed": 0,
                                 "completed_codes": [], "failures": [], "pending": 0, "universe_complete": False},
                "$inc": {"attempts": 1},
            }, upsert=previous is None)
            if not updated.matched_count and updated.upserted_id is None:
                raise CollectionStopped("采集任务已被新的请求接管")
            self.renew(day, token, now)
        except DuplicateKeyError:
            self.release(token)
            raise CollectionStopped("采集任务已被新的请求接管") from None
        except Exception:
            self.release(token)
            raise
        return token

    def renew(self, day, token, now):
        lease = now + timedelta(seconds=180)
        result = self.locks.update_one({"_id": "market", "token": token, "lease_until": {"$gt": now}},
                                       {"$set": {"lease_until": lease}})
        if result.matched_count != 1:
            raise CollectionStopped("采集租约失效")
        result = self.runs.update_one({"_id": day.isoformat(), "token": token, "status": "running"},
                                     {"$set": {"lease_until": lease, "updated_at": now}})
        if result.matched_count != 1:
            raise CollectionStopped("采集任务已失效")

    def save_progress(self, day, token, now, **fields):
        result = self.runs.update_one({"_id": day.isoformat(), "token": token, "status": "running", "lease_until": {"$gt": now}},
                                     {"$set": {**fields, "updated_at": now}})
        if result.matched_count != 1:
            raise CollectionStopped("采集任务已失效")

    def release(self, token):
        self.locks.delete_one({"_id": "market", "token": token})

    def get(self, day, now):
        self.recover(now)
        return self.runs.find_one({"_id": day.isoformat()}, {"_id": 0, "token": 0, "lease_until": 0,
                                                            "completed_codes": 0, "universe": 0})

    def latest(self, now):
        self.recover(now)
        return self.runs.find_one({}, {"_id": 0, "token": 0, "lease_until": 0,
                                      "completed_codes": 0, "universe": 0}, sort=[("updated_at", -1)])


class MarketCollectionService:
    def __init__(self, repository, quotes, *, universe=None, batch=None, now=None, clock=time.monotonic, budget=1200):
        self.repository, self.quotes = repository, quotes
        self.universe, self.batch = universe or MarketUniverse(), batch or BatchQuotes()
        self.now = now or (lambda: datetime.now(timezone.utc))
        self.clock, self.budget = clock, budget
        self.guard = threading.Lock()
        self.stop = threading.Event()
        self.worker = None

    def validate_day(self, day):
        now = self.now()
        day = day or now.astimezone(SHANGHAI).date()
        date_window(day, day, now)
        if (now.astimezone(SHANGHAI).date() - day).days > 366:
            raise ValueError("仅支持最近 366 天内已收盘日期")
        if day.weekday() > 4:
            raise ValueError("请选择已收盘的工作日，节假日仍须由实际行情核验")
        return day

    def start(self, day=None, retry=False):
        day = self.validate_day(day)
        with self.guard:
            if self.worker and self.worker.is_alive():
                raise CollectionBusy("全市场采集正在进行，请查看进度")
            token = self.repository.claim(day, self.now(), retry)
            if token is None:
                return {"started": False, "record": self.repository.get(day, self.now())}
            self.stop.clear()
            self.worker = threading.Thread(target=self.run, args=(day, token), daemon=True,
                                           name="market-collection")
            try:
                self.worker.start()
            except Exception:
                self.repository.save_progress(day, token, self.now(), status="failed", error="采集线程无法启动，请重试")
                self.repository.release(token)
                raise
        return {"started": True, "record": self.repository.get(day, self.now())}

    def run(self, day, token):
        started = self.clock()
        renewed = started - 30
        items, completed, failures = [], set(), {}
        incomplete = set()

        def checkpoint(force=False):
            nonlocal renewed
            if self.stop.is_set() or self.clock() - started >= self.budget:
                raise CollectionStopped("采集达到时间上限或服务正在停止，可重试剩余证券")
            if force or self.clock() - renewed >= 30:
                self.repository.renew(day, token, self.now())
                renewed = self.clock()

        try:
            universe = self.universe.fetch(checkpoint)
            items = universe["items"]
            codes = {item["code"] for item in items}
            checkpoint()
            names = {item["code"]: item["name"] for item in items}
            for row in self.quotes.collection.find({"date": day.isoformat(), "code": {"$in": list(codes)}}):
                try:
                    cached = DailyQuote.model_validate(row)
                    if security_name(cached.name) != security_name(names[cached.code]):
                        continue
                    completed.add(cached.code)
                    if any(getattr(cached, field) is None for field in ("pe", "pb", "turnover", "change_pct", "amount")):
                        incomplete.add(cached.code)
                except ValueError:
                    continue
            pending = [item for item in items if item["code"] not in completed]
            self.repository.save_progress(day, token, self.now(), total=len(items), succeeded=len(completed), failed=0, pending=len(pending),
                                          universe=items, universe_source=universe["source"],
                                          universe_complete=universe["complete"], completed_codes=sorted(completed), failures=[],
                                          incomplete_fields=len(incomplete), data_warning="部分已保存行情缺少估值或其他可选字段" if incomplete else "",
                                          reported_total=universe.get("reported_total", len(items)), excluded_codes=universe.get("excluded_codes", []),
                                          warning="" if universe["complete"] else "证券目录使用备用源或含未识别代码，覆盖范围未完全核实，不能视为全市场成功")
            for offset in range(0, len(pending), 50):
                checkpoint()
                group = pending[offset:offset + 50]
                quotes, errors = self.batch.fetch(group, day, self.now(), checkpoint)
                expected = {item["code"]: item["name"] for item in group}
                for quote in quotes:
                    if quote.code not in expected or quote.date != day or security_name(quote.name) != security_name(expected[quote.code]):
                        raise ValueError("批次返回不匹配行情")
                if len({quote.code for quote in quotes}) != len(quotes):
                    raise ValueError("批次返回重复行情")
                checkpoint(force=True)
                stored = self.quotes.save_missing(quotes)
                for row in stored:
                    try:
                        saved = DailyQuote.model_validate(row)
                        if saved.date != day or security_name(saved.name) != security_name(expected[saved.code]):
                            raise ValueError("已有行情与目录不符")
                        completed.add(saved.code)
                        if any(getattr(saved, field) is None for field in ("pe", "pb", "turnover", "change_pct", "amount")):
                            incomplete.add(saved.code)
                    except (ValueError, KeyError):
                        errors[row["code"]] = "已有记录格式或身份不符，需通过单股刷新核对"
                failures.update({code: errors.get(code, "无可核实的目标日行情") for code in expected if code not in completed})
                self.repository.save_progress(day, token, self.now(), succeeded=len(completed), failed=len(failures),
                                              pending=len(items) - len(completed) - len(failures), incomplete_fields=len(incomplete),
                                              data_warning="部分已保存行情缺少估值或其他可选字段" if incomplete else "",
                                              completed_codes=sorted(completed), failures=[{"code": code, "reason": reason} for code, reason in sorted(failures.items())])
            checkpoint()
            status = "completed" if len(completed) == len(items) and universe["complete"] else "partial"
            if not completed:
                status = "failed"
            self.repository.save_progress(day, token, self.now(), status=status, finished_at=self.now(),
                                          error="没有目标日收盘数据，可能休市或来源不可用" if not completed else "")
        except Exception as exc:
            message = str(exc) if isinstance(exc, CollectionStopped) else "全市场采集未完成，已入库行情保留，可重试"
            try:
                remaining = [{"code": item["code"], "reason": failures.get(item["code"], "本轮未完成，可继续补采")}
                             for item in items if item["code"] not in completed]
                fields = {"failed": len(remaining), "failures": remaining, "pending": 0} if items else {}
                self.repository.save_progress(day, token, self.now(), status="failed", error=message, **fields)
            except Exception:
                pass
        finally:
            try:
                self.repository.release(token)
            except Exception:
                pass
