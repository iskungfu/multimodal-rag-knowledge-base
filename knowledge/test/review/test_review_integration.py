"""显式启用的真实模型/MongoDB 验证，仅使用自建文本和独立测试集合。"""

import json
import os
from uuid import uuid4

import pytest
from pymongo import MongoClient
from pymongo.errors import PyMongoError

from knowledge.review.repository import ReviewRepository
from knowledge.service.review_service import ReviewService
from knowledge.test.review.test_review import client_for

pytestmark = pytest.mark.skipif(os.getenv("AFK_RUN_REVIEW_INTEGRATION") != "1", reason="未启用真实复盘集成验证")

SAMPLES = [
    ("executed", "今天以 12.5 元买入 000001 平安银行，因为突破后追高。反思应该等回踩，当时很着急。"),
    ("planned", "我准备明天以 185 元买入 300750 宁德时代，如果没有回落就不买，今天没有成交。"),
    ("name_only", "昨天以 185 元卖出了宁德时代，因为获利了结。反思卖早了，有点后悔。"),
    ("conflict", "今天买入了 000001 上证指数 ETF，低位布局，未记录单价。"),
    ("missing", "今天买入了 600519。"),
    ("mixed", "昨天以 12.5 元买入 000001；今天以 13 元卖出 000001，因为达到目标。计划明天以 12 元再买入 000001。"),
    ("unfilled", "今天挂单以 12 元买入 000001，但没有成交，已经撤单。"),
    ("reflection", "复盘教训：先制定计划，再按规则行动。今天没有买卖，也没有交易计划。"),
    ("quantity", "今天买入 600519 共 100 股，仓位 20%，收盘跌了 3%，没有记单价。"),
    ("position_plan", "现在仓位 80%，准备减仓到 40%，今天没有执行任何买卖。"),
]


@pytest.fixture
def collection():
    db_name = os.getenv("AFK_TEST_DB_NAME")
    assert db_name and db_name not in {os.getenv("MONGO_DB_NAME", "kb001"), "kb001", "admin", "local", "config"}, "必须使用独立 AFK_TEST_DB_NAME"
    assert os.getenv("MONGO_URL"), "缺少 MongoDB 配置"
    client = MongoClient(os.environ["MONGO_URL"], serverSelectionTimeoutMS=5000,
                         connectTimeoutMS=5000, socketTimeoutMS=5000, tz_aware=True)
    target = client[db_name]["trade_reviews_issue3_" + uuid4().hex]
    try:
        client.admin.command("ping")
        yield target
    except PyMongoError:
        pytest.fail("MongoDB 集成失败（连接详情已隐藏）", pytrace=False)
    finally:
        try:
            target.drop()
        except PyMongoError:
            pytest.fail("本次独立测试集合清理失败（连接详情已隐藏）", pytrace=False)
        finally:
            client.close()


@pytest.mark.parametrize("case,text", SAMPLES, ids=[case for case, _ in SAMPLES])
def test_live_model_api_and_mongodb(collection, case, text):
    for key in ("OPEN_API_BASE", "OPEN_API_KEY", "LLM_DEFAULT_MODEL"):
        assert os.getenv(key), f"缺少模型配置 {key}"
    service = ReviewService(ReviewRepository(collection))
    request_id = str(uuid4())
    with client_for(service) as client:
        response = client.post("/reviews", json={"text": text, "request_id": request_id})
        assert response.status_code == 201, "复盘 API 保存失败"
        body = response.json()
        assert body["original_text"] == text
        if body["extraction_status"] != "succeeded":
            pytest.fail(f"{case}: 真实抽取失败，原因={body['extraction_error']}，原文已保存", pytrace=False)
        stored = collection.find_one({"_id": request_id})
        assert stored["original_text"] == text and stored["extraction"] == body["extraction"]
        assert client.get(f"/reviews/{request_id}").json()["extraction"] == body["extraction"]
        assert client.get("/reviews").json()["items"][0]["id"] == request_id
        assert client.post("/reviews", json={"text": text, "request_id": request_id}).json()["extraction"] == body["extraction"]
        assert collection.count_documents({}) == 1
    extraction = body["extraction"]
    events = extraction["events"]
    print("真实样本", case, json.dumps(extraction, ensure_ascii=False))
    if case == "executed":
        assert len(events) == 1 and events[0]["status"] == "executed"
        assert events[0]["stock_code"] == "000001" and events[0]["direction"] == "buy"
        assert events[0]["price"] == 12.5 and events[0]["reason"]
        assert extraction["lesson"] and extraction["emotions"]
    elif case == "planned":
        assert len(events) == 1 and events[0]["status"] == "planned" and events[0]["price"] == 185
        assert events[0]["stock_code"] == "300750"
        assert extraction["emotions"] == []
    elif case == "name_only":
        assert len(events) == 1 and events[0]["stock_code"] is None
        assert events[0]["status"] == "executed" and events[0]["direction"] == "sell" and events[0]["price"] == 185
        assert extraction["lesson"] and extraction["emotions"]
    elif case == "conflict":
        assert len(events) == 1 and events[0]["identity_conflict"]
        assert events[0]["stock_code"] is None and events[0]["mentioned_stock_code"] == "000001"
    elif case == "missing":
        assert len(events) == 1 and events[0]["stock_code"] == "600519"
        assert events[0]["stock_name"] is None and events[0]["price"] is None and events[0]["reason"] is None
        assert extraction["lesson"] is None and extraction["emotions"] == []
    elif case == "mixed":
        assert [(event["status"], event["direction"], event["price"]) for event in events] == [
            ("executed", "buy", 12.5), ("executed", "sell", 13), ("planned", "buy", 12),
        ]
    elif case == "unfilled":
        assert events and all(event["status"] != "executed" for event in events)
    elif case == "reflection":
        assert events == [] and extraction["lesson"]
    elif case == "quantity":
        assert len(events) == 1 and events[0]["status"] == "executed"
        assert events[0]["price"] is None and events[0]["stock_name"] is None
        assert extraction["lesson"] is None and extraction["emotions"] == []
    elif case == "position_plan":
        assert events and all(event["status"] == "planned" for event in events)
        assert all(event["price"] is None and event["stock_code"] is None for event in events)


def test_real_mongodb_preserves_text_on_bad_model_json(collection):
    from knowledge.review.extraction import parse_extraction

    class MalformedModel:
        def extract(self, text):
            return parse_extraction("```json\n{bad JSON\n```", text)

    service = ReviewService(ReviewRepository(collection), MalformedModel())
    with client_for(service) as client:
        response = client.post("/reviews", json={"text": "仅保存测试复盘。"})
        assert response.status_code == 201
        assert response.json()["extraction_status"] == "failed"
        assert collection.find_one()["original_text"] == "仅保存测试复盘。"
