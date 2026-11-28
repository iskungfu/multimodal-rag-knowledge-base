"""收盘摘要用确定性行情风险 + 有引用的条件式策略清单。"""
import threading
import time as clock
from datetime import datetime, time, timedelta, timezone

from knowledge.market.models import SHANGHAI, MarketUnavailable
from knowledge.risk.symbols import Security, SymbolResolver
from knowledge.service.risk_service import RiskService
from knowledge.summary.indices import INDICES, N, evaluate_index, rule_note
from knowledge.summary.repository import protect_result, SummaryInputError
from knowledge.summary.strategies import StrategyChecklistModel
from knowledge.investment.generation import EvidenceSelector, evidence_text, validate_selections
from knowledge.personalization.settings import settings_context

OWNER = "default_user"


def market_evidence(assessments, stocks):
    evidence = []
    for item in assessments:
        level = item["level"] + "风险" if item["level"] else "无法评级"
        limitation = "（有限依据）" if item["limited"] else ""
        change = f'{item["return_pct"]:.2f}%' if item["return_pct"] is not None else "缺失"
        ratio = f'{item["volume_ratio"]:.2f}倍' if item["volume_ratio"] is not None else "缺失"
        text = (f'{item["name"]}（{item["symbol"]}）{item["date"]}：{level}{limitation}；'
                f'近{N}日涨跌幅{change}，成交量比{ratio}，来源{item["latest"]["source"]}。')
        evidence.append({"id": "index:" + item["symbol"], "text": text + "；".join(item["warnings"])})
    for item in stocks:
        evidence.append({"id": "stock:" + item["code"], "text": item["assessment"]})
    return evidence


class SummaryBusy(Exception):
    pass


class SummaryService:
    def __init__(self, repository, watchlist, indices, market, strategies, *, resolver=None, model=None,
                 now=None, owner=OWNER, knowledge=None, knowledge_model=None, settings=None):
        self.repository, self.watchlist, self.indices = repository, watchlist, indices
        self.market, self.strategies = market, strategies
        self.resolver = resolver or SymbolResolver()
        self.model = model or StrategyChecklistModel()
        self.now = now or (lambda: datetime.now(timezone.utc))
        self.owner = owner
        self.slot = threading.BoundedSemaphore(1)
        self.knowledge, self.settings = knowledge, settings
        self.knowledge_model = knowledge_model if knowledge_model is not None else EvidenceSelector(timeout=45, attempts=1)

    def add_watch(self, query):
        resolution = self.resolver.resolve(query)
        if not resolution.security:
            raise SummaryInputError(resolution.message or "请提供一只股票的完整名称或代码")
        return self.watchlist.add(self.owner, resolution.security)

    def validate_day(self, day):
        local = self.now().astimezone(SHANGHAI)
        if day > local.date() or (day == local.date() and local.time() < time(15, 15)):
            raise SummaryInputError("只能为已收盘日期生成摘要，当天须等待 Asia/Shanghai 15:15 后")
        if day.weekday() >= 5:
            raise SummaryInputError("周末不生成正式收盘摘要，请选择已核实行情的交易日期")
        if (local.date() - day).days > 366:
            raise SummaryInputError("手动补生成仅支持最近 366 个自然日")

    def begin(self, day, retry=False):
        self.validate_day(day)
        if not self.slot.acquire(blocking=False):
            existing = self.repository.get(self.owner, day, self.now())
            if existing and existing["status"] == "processing":
                return None, existing
            raise SummaryBusy("本进程正在生成其他摘要，请稍后重试；超时后可重启服务恢复")
        try:
            claim = self.repository.claim(self.owner, day, self.now(), retry)
            if claim:
                return claim, self.repository.get(self.owner, day, self.now())
            record = self.repository.get(self.owner, day, self.now())
            self.slot.release()
            return None, record
        except Exception:
            self.slot.release()
            raise

    def run(self, claim):
        from datetime import date
        day = date.fromisoformat(claim["date"])
        started = clock.monotonic()
        error = "摘要生成失败，请稍后重试"

        def fail(message):
            try:
                self.repository.finish(claim, self.now(), error=message)
            except Exception:
                pass

        try:
            def check_budget():
                if clock.monotonic() - started > 240:
                    raise TimeoutError
            warnings, assessments = [], []
            error = "用户档案读取失败，未扩大个人策略范围；请稍后重试"
            context = settings_context(self.settings()) if self.settings is not None else {}
            error = "摘要生成失败，请稍后重试"
            start = day - timedelta(days=120)
            for symbol in INDICES:
                check_budget()
                rows, notices = self.indices.fetch(symbol, start, day, self.now())
                assessment = evaluate_index(rows, symbol, day)
                assessments.append(assessment)
                warnings.extend(notices + assessment["warnings"])
            check_budget()
            watch = self.watchlist.read(self.owner)
            risk = RiskService(lambda: self.market, now=lambda: datetime.combine(day, time(16), SHANGHAI))
            stocks = []
            for item in watch:
                check_budget()
                stocks.append({**item, "assessment": risk.answer(Security(**item))})
            if not stocks:
                warnings.append("尚未配置关注股票；关注列表不代表实际持仓")
            check_budget()
            error = "个人策略检索失败，未编造策略；请检查策略库后重试"
            evidence = market_evidence(assessments, stocks)
            retrieval_query = "收盘复核：结合以下风险与缺失信息，检索相关的个人风险控制、仓位约束及交易纪律。\n"
            retrieval_query += "\n".join(item["text"].split("\n\n")[0] for item in evidence)
            if context:
                retrieval_query += "\n用户档案资料：\n" + context["text"]
            identifiers = context.get("document_ids")
            options = {"document_ids": identifiers} if identifiers is not None else {}
            retrieved = self.strategies.retrieve(self.owner, retrieval_query, **options)
            strategies = []
            if retrieved["status"] == "retrieved":
                error = "策略模型调用或依据校验失败，未发布新摘要；请重试"
                selection_options = {"profile_context": context} if context else {}
                strategies = self.model.select(retrieved["items"], evidence, **selection_options)
                if not strategies:
                    raise ValueError
            elif retrieved["status"] == "empty":
                warnings.append("尚无已完成导入的个人策略，本摘要没有个性化操作提示")
            else:
                warnings.append("已导入策略但未检索到足够片段，本摘要没有个性化操作提示")
            check_budget()
            knowledge, knowledge_status = [], "unconfigured"
            if self.knowledge is not None:
                try:
                    public = self.knowledge.retrieve(retrieval_query + "\n公共投资知识中的风险控制、分散投资和决策依据。")
                    knowledge_status = public["status"]
                    if knowledge_status == "retrieved":
                        if any(item.get("source") != "knowledge" or item.get("owner_id") != "public"
                               for item in public["items"]):
                            raise ValueError("公共知识归属不符")
                        selected = self.knowledge_model.select(retrieval_query, public["items"], evidence)
                        knowledge = validate_selections(selected, public["items"], evidence)
                except Exception:
                    knowledge_status = "failed"
            if not knowledge:
                warnings.append("公共知识检索或整理暂不可用，本次未引用公共知识。" if knowledge_status == "failed"
                                else "暂无可引用的公共知识，可导入投资书籍后重新生成。")
            check_budget()
            lines = [f"每日投资摘要｜实际收盘日期：{day.isoformat()}",
                     "使用生成时的关注列表与个人策略；历史补生成不还原历史当日配置。", "大盘当前位置风险"]
            if context:
                lines.insert(2, evidence_text(context["text"]))
            for item in assessments:
                latest = item["latest"]
                level = f'{item["level"]}风险' if item["level"] else "无法评级"
                suffix = "（有限依据）" if item["limited"] else ""
                change = f'{item["return_pct"]:.2f}%' if item["return_pct"] is not None else "不足"
                ratio = f'{item["volume_ratio"]:.2f} 倍' if item["volume_ratio"] is not None else "不足"
                lines.append(f'{item["name"]}（{item["symbol"]}）：{level}{suffix}；收盘 {latest["close"]:g} 点，'
                             f'当日 {latest["change_pct"]:g}%；近{N}日涨跌幅 {change}，成交量比 {ratio}。'
                             f'日期 {item["date"]}，来源 {latest["source"]}；成交量 {latest["volume"]:g} 股。')
            lines += [rule_note(), "关注个股风险（关注不代表持仓）"]
            lines += [item["assessment"] for item in stocks] or ["尚未配置关注股票，可在页面添加。"]
            lines += ["个人策略操作提示（条件式检查清单）", "持仓与交易执行情况未知；以下前提尚未确认满足，请人工核对，不表示已成交。"]
            for item in strategies:
                related = "\n".join(f'{fact["id"]}：{fact["text"]}' for fact in item.get("evidence", []))
                if related:
                    lines.append("本条策略的行情复核依据（不表示完整前提已满足）：\n" + related)
                lines.append("□ 若计划采用以下策略，请先核对完整原文中的所有前提，仅在人工确认适用后考虑执行；"
                             "条件未知时暂不据此操作。\n策略原文（完整检索片段）：" + item["content"] +
                             f'\n来源：个人策略《{item["title"]}》，'
                             f'[chunk_id={item["chunk_id"]}]')
            if not strategies:
                lines.append("当前模式没有可引用的个人策略；请核对模式中的文档或导入策略后重新生成。")
            if knowledge:
                lines.append("公共知识参考：用于核对判断依据，书籍观点不证明当前策略适用或收益。")
                for item in knowledge:
                    lines.append("公共知识原文（完整检索片段）：\n" + evidence_text(item["content"]) +
                                 f'\n来源：公共知识《{evidence_text(item["title"])}》，'
                                 f'document_id={item.get("document_id") or "旧公共库未记录"}；'
                                 f'[chunk_id={item["chunk_id"]}]')
            lines += ["数据与限制："] + list(dict.fromkeys(warnings))
            result = protect_result({"date": day.isoformat(), "indices": assessments, "stocks": stocks,
                                     "strategies": strategies, "strategy_status": retrieved["status"],
                                     "knowledge": knowledge, "knowledge_status": knowledge_status,
                                     "personalization": context,
                                     "warnings": list(dict.fromkeys(warnings)), "answer": "\n\n".join(lines)})
            error = "摘要保存失败，请稍后重试"
            self.repository.finish(claim, self.now(), result=result)
        except MarketUnavailable:
            fail("双指数目标日收盘数据未核实，可能休市、延迟或源不可用；请稍后重试")
        except TimeoutError:
            fail("摘要生成超过时间预算，请重试")
        except Exception:
            fail(error)
        finally:
            self.slot.release()

    def generate(self, day=None, retry=False):
        day = day or self.now().astimezone(SHANGHAI).date()
        claim, record = self.begin(day, retry)
        if claim:
            self.run(claim)
        return self.repository.get(self.owner, day, self.now())

    def latest(self):
        today = self.now().astimezone(SHANGHAI).date()
        return {"today": today.isoformat(), "current": self.repository.get(self.owner, today, self.now()),
                "latest_success": self.repository.latest_success(self.owner, self.now())}
