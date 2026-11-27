"""显式启用真实外网 + MongoDB 验证；只使用指定测试库内的独立集合。"""

import os
import uuid
from datetime import datetime, timezone

import pytest
from pymongo import MongoClient
from pymongo.errors import DuplicateKeyError, PyMongoError

from knowledge.market.adapters import EastmoneyAdapter, TencentAdapter
from knowledge.market.models import date_window
from knowledge.market.repository import MarketQuoteRepository
from knowledge.service.market_service import MarketService
from knowledge.test.market.test_market import NOW, START, END, client_for, eastmoney

pytestmark = pytest.mark.skipif(os.getenv("AFK_RUN_MARKET_INTEGRATION") != "1", reason="须显式启用真实行情/MongoDB 验证")


@pytest.fixture
def collection():
    db_name = os.getenv("AFK_TEST_DB_NAME")
    assert db_name and db_name not in {os.getenv("MONGO_DB_NAME", "kb001"), "admin", "config", "local", "kb001"}, "必须配置独立 AFK_TEST_DB_NAME"
    assert os.getenv("MONGO_URL"), "缺少 MongoDB 连接配置"
    client = MongoClient(os.environ["MONGO_URL"], serverSelectionTimeoutMS=5000, connectTimeoutMS=5000, socketTimeoutMS=5000)
    collection = client[db_name]["market_quotes_issue2_" + uuid.uuid4().hex]
    try:
        client.admin.command("ping")
        yield collection
    except PyMongoError:
        pytest.fail("MongoDB 集成验证失败（连接详情已隐藏）", pytrace=False)
    finally:
        try:
            collection.drop()  # 仅删除本次 UUID 集合，绝不清空数据库或业务集合
        except PyMongoError:
            pytest.fail("独立测试集合清理失败（连接详情已隐藏）", pytrace=False)
        finally:
            client.close()


def test_real_mongodb_unique_index_upsert_and_retained_valuation(collection):
    repository = MarketQuoteRepository(collection)
    quotes = eastmoney().fetch_daily("600519", START, END, NOW)
    repository.save(quotes)
    repository.save(quotes)
    assert collection.count_documents({}) == 3
    stored = collection.find_one({"code": "600519", "date": "2026-11-27"})
    assert stored["pe"] == 21.41 and stored["raw"]["snapshot"]["data"]["f162"] == 2141
    with pytest.raises(DuplicateKeyError):
        collection.insert_one({"code": "600519", "date": "2026-11-27"})
    quotes[-1].pe = None
    quotes[-1].raw.pop("snapshot")
    repository.save([quotes[-1]])
    assert repository.read("600519", START, END)[-1]["pe"] == 21.41
    assert collection.find_one({"code": "600519", "date": "2026-11-27"})["raw"]["snapshot"]["data"]["f162"] == 2141


@pytest.mark.parametrize("adapter_type", [EastmoneyAdapter, TencentAdapter])
def test_live_source_to_api_to_mongodb(collection, adapter_type):
    service = MarketService(MarketQuoteRepository(collection), [adapter_type()])
    with client_for(service) as client:
        response = client.post("/market/quotes/600519/refresh")
        assert response.status_code == 200, response.text
        body = response.json()
        assert body["source"] == adapter_type.name
        assert body["quotes"] and body["quotes"][-1]["close"] > 0
        assert body["quotes"][-1]["pe"] is not None
        assert collection.count_documents({}) == len(body["quotes"])
        again = client.post("/market/quotes/600519/refresh")
        assert again.json()["cached"] is True
        assert collection.count_documents({}) == len(body["quotes"])
        assert client.get("/market/quotes/600519").json()["quotes"] == body["quotes"]


def test_live_shenzhen_and_cross_source_units():
    now = datetime.now(timezone.utc)
    start, end = date_window(None, None, now)
    primary = EastmoneyAdapter().fetch_daily("000001", start, end, now)[-1]
    backup = TencentAdapter().fetch_daily("000001", start, end, now)[-1]
    assert primary.code == backup.code == "000001"
    assert primary.name == backup.name == "平安银行"
    assert primary.date == backup.date
    for key in ("close", "volume", "change_pct", "turnover", "pe", "pb"):
        assert getattr(primary, key) == pytest.approx(getattr(backup, key))
