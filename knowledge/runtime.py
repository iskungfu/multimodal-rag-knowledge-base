"""查询运行装配：每次应用运行拥有自己的实例、存储命名和退出清理。"""
from dataclasses import dataclass, field
from functools import cache
from contextlib import contextmanager
import os
import re
import threading
import time
from urllib.parse import unquote, urlsplit
from uuid import uuid4


MONGO_NAMES = {
    "documents": "strategy_documents", "reviews": "trade_reviews", "history": "chat_history",
    "stocks": "market_quotes", "indices": "index_quotes", "summaries": "daily_summaries",
    "watches": "watchlists", "settings": "investor_settings",
    "runs": "market_collection_runs", "locks": "market_collection_locks",
}
QUERY_FEATURES = frozenset({"query", "market", "calendar", "review", "summary", "settings", "collection"})
ACCEPTANCE_FEATURES = {
    "stock": frozenset({"query", "market", "calendar"}),
    "summary": frozenset({"query", "market", "calendar", "review", "summary", "strategy"}),
    "investment": frozenset({"query", "market", "calendar", "review", "summary", "strategy", "settings"}),
}


def enabled(name, default="0"):
    return os.getenv(name, default).lower() in {"1", "true", "yes"}


@dataclass(frozen=True)
class RuntimeConfig:
    database: str
    mongo_url: str = field(repr=False)
    milvus_url: str = field(repr=False)
    mongo_names: dict
    vector_names: dict
    features: frozenset = QUERY_FEATURES
    temporary: bool = False
    borrowed_vectors: frozenset = frozenset()
    vector_dimension: int = 1024
    summary_enabled: bool = False
    collection_enabled: bool = False
    summary_hour: int = 15
    summary_minute: int = 30

    @classmethod
    def from_env(cls, *, summary_enabled=None):
        active = enabled("DAILY_SUMMARY_ENABLED", "1") if summary_enabled is None else summary_enabled
        return cls(os.getenv("MONGO_DB_NAME", "kb001"), os.getenv("MONGO_URL", ""),
                   os.getenv("MILVUS_URL", ""), dict(MONGO_NAMES), {
                       "personal": os.getenv("STRATEGY_COLLECTION", "strategy_chunks_v1"),
                       "knowledge": os.getenv("CHUNKS_COLLECTION", ""),
                       "review": os.getenv("REVIEW_COLLECTION", "review_chunks_v1"),
                       "items": os.getenv("ITEM_NAME_COLLECTION", ""),
                   }, summary_enabled=active,
                   collection_enabled=enabled("MARKET_COLLECTION_ENABLED", "1" if active else "0"),
                   summary_hour=int(os.getenv("DAILY_SUMMARY_HOUR", "15")),
                   summary_minute=int(os.getenv("DAILY_SUMMARY_MINUTE", "30")))

    @classmethod
    def acceptance(cls, profile, prefix, *, shared_public=False, vector_dimension=1024):
        base = cls.from_env()
        database = os.getenv("AFK_TEST_DB_NAME", "")
        uri_database = unquote(urlsplit(base.mongo_url).path.lstrip("/").split("/")[0])
        forbidden = {base.database.casefold(), uri_database.casefold(), "kb001", "admin", "local", "config"}
        if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_-]{0,62}", database) or database.casefold() in forbidden:
            raise ValueError("必须设置与业务库不同的 AFK_TEST_DB_NAME")
        if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]{0,100}", prefix):
            raise ValueError("验收资源前缀无效")
        vectors = {name: prefix + "_" + name for name in base.vector_names}
        borrowed = frozenset()
        if shared_public:
            for name in ("knowledge", "items"):
                vectors[name] = base.vector_names[name]
            borrowed = frozenset(value for key, value in vectors.items() if key in {"knowledge", "items"} and value)
        return cls(database, base.mongo_url, base.milvus_url,
                   {key: prefix + "_" + key for key in MONGO_NAMES}, vectors,
                   features=ACCEPTANCE_FEATURES[profile], temporary=True,
                   borrowed_vectors=borrowed, vector_dimension=vector_dimension)


class QueryRuntime:
    def __init__(self, config=None, *, mongo_factory=None, milvus_factory=None):
        self.config = config if config is not None else RuntimeConfig.from_env()
        self._mongo_factory, self._milvus_factory = mongo_factory, milvus_factory
        self._instances = {}
        self._guard = threading.RLock()
        # HTTP 活动计数不等待可能正在连接存储的装配锁，避免阻塞事件循环。
        self._condition = threading.Condition()
        self._close_guard = threading.Lock()
        self._active = 0
        self._stopping = False
        self._mongo_owned, self._vectors_owned = set(), set()
        self._tasks = set()
        self._schedulers = []
        self._closed = False
        self._cleanup = {}

    def _get(self, name, factory):
        with self._guard:
            if self._closed:
                raise RuntimeError("运行资源已关闭")
            if name not in self._instances:
                self._instances[name] = factory()
            return self._instances[name]

    @property
    def mongo(self):
        def connect():
            if self._mongo_factory is not None:
                return self._mongo_factory()
            from pymongo import MongoClient
            if not self.config.mongo_url:
                raise ValueError("未配置 MongoDB")
            return MongoClient(self.config.mongo_url, serverSelectionTimeoutMS=5000,
                               connectTimeoutMS=5000, socketTimeoutMS=15000, tz_aware=True)
        return self._get("mongo", connect)

    @property
    def milvus(self):
        def connect():
            if self._milvus_factory is not None:
                return self._milvus_factory()
            from pymilvus import MilvusClient
            if not self.config.milvus_url:
                raise ValueError("未配置 Milvus")
            # 同 URI 的默认客户端可能共用底层连接；本轮关闭不能影响其他应用。
            return MilvusClient(uri=self.config.milvus_url, timeout=15, alias="query_runtime_" + uuid4().hex)
        return self._get("milvus", connect)

    def collection(self, role):
        def allocate():
            name = self.config.mongo_names[role]
            database = self.mongo[self.config.database]
            if self.config.temporary:
                if database.list_collection_names(filter={"name": name}):
                    raise ValueError("本轮 MongoDB 集合已存在")
                self._mongo_owned.add(name)
            return database[name]
        return self._get("mongo:" + role, allocate)

    def vector_name(self, kind):
        name = self.config.vector_names[kind]
        with self._guard:
            if self._closed:
                raise RuntimeError("运行资源已关闭")
            if self.config.temporary and name and name not in self.config.borrowed_vectors and name not in self._vectors_owned:
                if self.milvus.has_collection(name, timeout=10):
                    raise ValueError("本轮向量集合已存在")
                self._vectors_owned.add(name)
        return name

    @property
    def documents(self):
        return self.collection("documents")

    @property
    def history(self):
        return self.collection("history")

    def vectors(self, kind):
        def build():
            from knowledge.review.vectors import ReviewVectors
            from knowledge.strategy.vectors import StrategyVectors
            cls = ReviewVectors if kind == "review" else StrategyVectors
            return cls(self.milvus, self.vector_name(kind), self.config.vector_dimension)
        return self._get("vectors:" + kind, build)

    @property
    def market(self):
        def build():
            from knowledge.market.repository import MarketQuoteRepository
            from knowledge.service.market_service import MarketService
            return MarketService(MarketQuoteRepository(self.collection("stocks")))
        return self._get("market", build)

    @property
    def indices(self):
        def build():
            from knowledge.summary.indices import IndexRepository, IndexService
            return IndexService(IndexRepository(self.collection("indices")))
        return self._get("indices", build)

    @property
    def settings(self):
        def build():
            from knowledge.personalization.settings import SettingsRepository
            return SettingsRepository(self.collection("settings"), self.documents)
        return self._get("settings", build)

    @property
    def review_index(self):
        def build():
            from knowledge.review.indexing import ReviewIndexer
            from knowledge.review.repository import ReviewRepository
            return ReviewIndexer(ReviewRepository(self.collection("reviews")),
                                 vectors_factory=lambda: self.vectors("review"),
                                 collection=self.config.vector_names["review"])
        return self._get("review_index", build)

    @property
    def reviews(self):
        def build():
            from knowledge.service.review_service import ReviewService
            return ReviewService(self.review_index.repository, indexer=self.review_index)
        return self._get("reviews", build)

    @property
    def strategy(self):
        def build():
            from knowledge.strategy.repository import StrategyRepository
            from knowledge.service.strategy_service import StrategyService
            return StrategyService(StrategyRepository(self.documents), self.vectors("personal"),
                                   register_task=self.register_task)
        return self._get("strategy", build)

    @property
    def retriever(self):
        def build():
            from knowledge.investment.retrieval import InvestmentRetriever
            return InvestmentRetriever(self.documents, self.review_index, collections=self.config.vector_names,
                                       client_factory=lambda: self.milvus,
                                       collection_name=self.vector_name)
        return self._get("retriever", build)

    @property
    def investment(self):
        def build():
            from knowledge.service.investment_service import InvestmentAssistant
            return InvestmentAssistant(self.retriever, settings=self.settings.read)
        return self._get("investment", build)

    @property
    def summary(self):
        def build():
            from knowledge.service.summary_service import SummaryService
            from knowledge.summary.repository import SummaryRepository, WatchlistRepository
            from knowledge.summary.strategies import PersonalStrategyRetriever
            from knowledge.summary.knowledge import PublicKnowledgeRetriever
            return SummaryService(SummaryRepository(self.collection("summaries")),
                                  WatchlistRepository(self.collection("watches")), self.indices, self.market,
                                  PersonalStrategyRetriever(self.documents, vectors_factory=lambda: self.vectors("personal")),
                                  knowledge=PublicKnowledgeRetriever(self.retriever), settings=self.settings.read)
        return self._get("summary", build)

    @property
    def query(self):
        def build():
            from knowledge.investment.market import index_context
            from knowledge.service.query_service import QueryService
            from knowledge.service.risk_service import RiskService
            return QueryService(risk=RiskService(lambda: self.market), investment=self.investment,
                                history=self.history, index_context_factory=lambda: index_context(self.indices),
                                graph_factory=self._query_graph)
        return self._get("query", build)

    def _query_graph(self):
        def build():
            from knowledge.processor.query_processor.config import QueryConfig
            from knowledge.processor.query_processor.main_graph import create_query_graph
            config = QueryConfig(chunks_collection=self.config.vector_names["knowledge"],
                                 item_name_collection=self.config.vector_names["items"])
            return create_query_graph(config=config, client_factory=lambda: self.milvus, history=self.history)
        return self._get("query_graph", build)

    @property
    def collection_job(self):
        def build():
            from knowledge.market.collection import CollectionRepository, MarketCollectionService
            return MarketCollectionService(CollectionRepository(self.collection("runs"), self.collection("locks")),
                                           self.market.repository)
        return self._get("collection_job", build)

    def register_task(self, task_id):
        with self._guard:
            if self._closed:
                raise RuntimeError("运行资源已关闭")
            self._tasks.add(task_id)

    def owns_task(self, task_id):
        with self._guard:
            return task_id in self._tasks

    def track_scheduler(self, scheduler):
        self._schedulers.append(scheduler)

    def start(self):
        with self._guard, self._condition:
            if self._closed or self._active:
                raise RuntimeError("运行资源尚未停止或已经关闭")
            job = self._instances.get("collection_job")
            if job is not None and job.worker is not None and job.worker.is_alive():
                raise RuntimeError("采集任务尚未停止")
            self._stopping = False

    @contextmanager
    def activity(self):
        with self._condition:
            if self._closed or self._stopping:
                raise RuntimeError("运行资源正在退出")
            self._active += 1
        try:
            yield
        finally:
            with self._condition:
                self._active -= 1
                self._condition.notify_all()

    def run_task(self, action, *args):
        # run_in_executor 的请求取消不会停止同步线程，因此另行追踪线程本身。
        with self.activity():
            return action(*args)

    def stop(self, timeout=10):
        with self._condition:
            self._stopping = True
        job = self._instances.get("collection_job")
        if job is not None:
            job.stop.set()
        for scheduler in self._schedulers:
            if scheduler.running:
                scheduler.shutdown(wait=True)
        self._schedulers.clear()
        deadline = time.monotonic() + timeout
        with self._condition:
            if not self._condition.wait_for(lambda: self._active == 0, timeout=timeout):
                return False
        # 在途请求可能刚刚启动采集；再次取得本轮任务并发出停止信号。
        job = self._instances.get("collection_job")
        if job is not None:
            job.stop.set()
        if job is not None and job.worker is not None and job.worker.is_alive():
            job.worker.join(timeout=max(0, deadline - time.monotonic()))
            if job.worker.is_alive():
                return False
        return True

    def manifest(self):
        return {"database": self.config.database, "mongo": sorted(self._mongo_owned),
                "vectors": sorted(self._vectors_owned), "borrowed_vectors": sorted(self.config.borrowed_vectors)}

    def close(self):
        with self._close_guard:
            return self._close()

    def _close(self):
        with self._guard:
            if self._closed:
                return dict(self._cleanup)
        if not self.stop():
            return {"workers_stopped": False}
        with self._guard, self._condition:
            self._closed = True
        result = {"workers_stopped": True}
        milvus = self._instances.get("milvus")
        mongo = self._instances.get("mongo")
        for name in sorted(self._vectors_owned):
            try:
                if milvus.has_collection(name, timeout=10):
                    milvus.drop_collection(name, timeout=30)
                result[name] = not milvus.has_collection(name, timeout=10)
            except Exception:
                result[name] = False
        for name in sorted(self._mongo_owned):
            try:
                database = mongo[self.config.database]
                database[name].drop()
                result[name] = not database.list_collection_names(filter={"name": name})
            except Exception:
                result[name] = False
        from knowledge.utils.task_util import forget_task
        from knowledge.utils.sse_util import remove_sse_queue
        for task_id in self._tasks:
            forget_task(task_id)
            remove_sse_queue(task_id)
        self._tasks.clear()
        for name in ("milvus", "mongo"):
            client = self._instances.get(name)
            if client is not None:
                try:
                    client.close()
                    result[name + "_close"] = True
                except Exception:
                    result[name + "_close"] = False
        with self._guard:
            self._closed, self._cleanup = True, result
        return dict(result)


class RuntimeRequests:
    """追踪完整 HTTP 请求及其后台工作，关闭时等待真正的写入结束。"""
    def __init__(self, app, runtime):
        self.app, self.runtime = app, runtime

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return await self.app(scope, receive, send)
        from starlette.responses import JSONResponse
        activity = self.runtime.activity()
        try:
            activity.__enter__()
        except RuntimeError:
            response = JSONResponse({"detail": "应用正在退出，请稍后重试"}, status_code=503)
            return await response(scope, receive, send)
        try:
            await self.app(scope, receive, send)
        finally:
            activity.__exit__(None, None, None)


@cache
def default_runtime():
    """仅供独立脚本和导入进程兼容调用；查询应用总是拥有自己的 runtime。"""
    return QueryRuntime()


def runtime_for(request=None):
    if request is not None and hasattr(request.app.state, "runtime"):
        return request.app.state.runtime
    return default_runtime()
