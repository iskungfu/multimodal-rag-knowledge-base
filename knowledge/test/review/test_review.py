import json
from unittest.mock import Mock
from uuid import uuid4

import mongomock
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from pymongo.errors import AutoReconnect

from knowledge.api.review_api import get_review_service, router
from knowledge.review.extraction import ExtractionUnavailable, parse_extraction
from knowledge.review.privacy import redact_text
from knowledge.review.repository import ReviewRepository
from knowledge.service.review_service import DEFAULT_OWNER, DISCLAIMER, ReviewService


TEXT = "今天以 12.5 元买入 000001，因为突破后追高。反思应该等回踩，当时很着急。"
EXTRACTION = {
    "events": [{"status": "executed", "stock_code": "000001", "stock_name": None,
                "direction": "buy", "price": 12.5, "reason": "突破后追高",
                "evidence": "今天以 12.5 元买入 000001，因为突破后追高。", "identity_conflict": False}],
    "lesson": "应该等回踩", "emotions": ["很着急"],
}


def client_for(service):
    app = FastAPI()
    app.include_router(router)
    app.dependency_overrides[get_review_service] = lambda: service
    return TestClient(app)


@pytest.fixture
def components():
    collection = mongomock.MongoClient().review_tests.trade_reviews
    extractor = Mock()
    extractor.extract.return_value = parse_extraction(json.dumps(EXTRACTION), TEXT)
    service = ReviewService(ReviewRepository(collection), extractor)
    return collection, extractor, service


@pytest.mark.parametrize("wrapper", ["{}", "```json\n{}\n```", "抽取结果如下：\n{}\n请核对。"])
def test_json_wrappers_and_source_evidence(wrapper):
    parsed = parse_extraction(wrapper.format(json.dumps(EXTRACTION)), TEXT)
    assert parsed["events"][0]["price"] == 12.5
    assert parsed["events"][0]["mentioned_stock_code"] == "000001"
    assert parsed["lesson"] == "应该等回踩"


@pytest.mark.parametrize("content", [
    "not JSON", "{}", "[]", '[{"events": [], "lesson": null, "emotions": []}]',
    '{"events":[], "lesson":null, "emotions":[]} {}',
    '{"events":[], "events":[], "lesson":null, "emotions":[]}',
    '{"events":[',
])
def test_invalid_json_does_not_pass_as_extraction(content):
    with pytest.raises(ValueError):
        parse_extraction(content, TEXT)


@pytest.mark.parametrize("field,value", [
    ("stock_code", "600519"), ("stock_name", "平安银行"), ("price", 99.0),
    ("price", True), ("price", -1), ("price", float("nan")),
    ("reason", "看好基本面"), ("evidence", "模型编造的成交"), ("status", "bought"),
])
def test_ungrounded_or_invalid_event_is_rejected(field, value):
    data = json.loads(json.dumps(EXTRACTION))
    data["events"][0][field] = value
    with pytest.raises(ValueError):
        parse_extraction(json.dumps(data), TEXT)


@pytest.mark.parametrize("changes", [{"lesson": "必须止损"}, {"emotions": ["焦虑"]}])
def test_unstated_reflection_or_emotion_is_rejected(changes):
    with pytest.raises(ValueError):
        parse_extraction(json.dumps({**EXTRACTION, **changes}), TEXT)


def test_conflicting_identity_keeps_mentioned_code_without_stock_link():
    text = "今天买入了 000001 上证指数 ETF。"
    data = {"events": [{"status": "executed", "stock_code": "000001", "stock_name": "上证指数 ETF",
                        "direction": "buy", "price": None, "reason": None, "evidence": text,
                        "identity_conflict": True}], "lesson": None, "emotions": []}
    event = parse_extraction(json.dumps(data), text)["events"][0]
    assert event["stock_code"] is None and event["mentioned_stock_code"] == "000001"
    assert event["identity_conflict"] is True


def test_record_identifiers_are_not_treated_as_private_phone_numbers(components):
    _, _, service = components
    request_id = "13900001-1234-4000-8000-139000012345"
    with client_for(service) as client:
        response = client.post("/reviews", json={"text": TEXT, "request_id": request_id})
        assert response.json()["id"] == request_id
        assert client.get(f"/reviews/{request_id}").json()["id"] == request_id


def test_save_read_idempotency_and_owner_filter(components):
    collection, extractor, service = components
    request_id = str(uuid4())

    def extract_after_save(text):
        stored = collection.find_one({"_id": request_id})
        assert stored["original_text"] == text and stored["extraction_status"] == "pending"
        return parse_extraction(json.dumps(EXTRACTION), text)

    extractor.extract.side_effect = extract_after_save
    with client_for(service) as client:
        payload = {"text": TEXT, "request_id": request_id}
        response = client.post("/reviews", json=payload)
        assert response.status_code == 201
        body = response.json()
        assert body["original_text"] == TEXT and body["extraction_status"] == "succeeded"
        assert body["disclaimer"] == DISCLAIMER
        assert client.post("/reviews", json=payload).status_code == 201
        assert extractor.extract.call_count == 1 and collection.count_documents({}) == 1
        assert client.get(f"/reviews/{request_id}").json()["extraction"] == body["extraction"]
        assert client.post("/reviews", json={**payload, "text": "不同复盘"}).status_code == 409
        other_id = str(uuid4())
        collection.insert_one({"_id": other_id, "owner_id": "another_user", "original_text": "不可见"})
        assert len(client.get("/reviews").json()["items"]) == 1
        assert client.get(f"/reviews/{other_id}").status_code == 404


@pytest.mark.parametrize("error,code", [
    (ValueError("private model output"), "invalid_extraction"),
    (ExtractionUnavailable("private endpoint and token"), "model_unavailable"),
])
def test_extraction_failure_still_saves_and_reads_original(components, error, code):
    collection, extractor, service = components
    extractor.extract.side_effect = error
    with client_for(service) as client:
        body = client.post("/reviews", json={"text": TEXT}).json()
        assert body["extraction_status"] == "failed" and body["extraction"] is None
        assert body["extraction_error"] == code and "private" not in json.dumps(body)
        assert client.get(f'/reviews/{body["id"]}').json()["original_text"] == TEXT
        assert collection.find_one()["original_text"] == TEXT


def test_insert_failure_does_not_call_model_or_claim_success(components):
    collection, extractor, service = components
    service.repository.create = Mock(side_effect=AutoReconnect("private address"))
    with client_for(service) as client:
        response = client.post("/reviews", json={"text": TEXT})
    assert response.status_code == 503 and "private" not in response.text
    extractor.extract.assert_not_called()
    assert collection.count_documents({}) == 0


def test_result_update_failure_keeps_pending_original(components):
    collection, _, service = components
    service.repository.finish = Mock(side_effect=AutoReconnect("private address"))
    with client_for(service) as client:
        response = client.post("/reviews", json={"text": TEXT})
    assert response.status_code == 201
    assert response.json()["extraction_status"] == "pending"
    assert response.json()["warning"] and "private" not in response.text
    assert collection.find_one()["original_text"] == TEXT


@pytest.mark.parametrize("payload", [{"text": " "}, {"text": "a" * 10001},
                                        {"text": TEXT, "owner_id": "other"},
                                        {"text": TEXT, "request_id": "bad-id"}])
def test_input_validation_does_not_save(components, payload):
    collection, extractor, service = components
    with client_for(service) as client:
        assert client.post("/reviews", json=payload).status_code == 422
        assert client.get("/reviews?limit=101").status_code == 422
    assert collection.count_documents({}) == 0
    extractor.extract.assert_not_called()


def test_redaction_before_model_storage_and_read(components):
    collection, extractor, service = components
    source = "姓名：张小甲，手机号 13900001234，身份证 110101199001011234，银行卡 6222000011112222，持仓金额 523456 元。买入单价 12.5 元，代码 000001。"
    safe = redact_text(source)
    for private in ["张小甲", "13900001234", "110101199001011234", "6222000011112222", "523456"]:
        assert private not in safe
    assert "50万+" in safe and "12.5" in safe and "000001" in safe
    assert redact_text(safe) == safe
    extractor.extract.side_effect = ExtractionUnavailable()
    with client_for(service) as client:
        body = client.post("/reviews", json={"text": source}).json()
        assert body["original_text"] == safe
        extractor.extract.assert_called_once_with(safe)
        assert collection.find_one()["original_text"] == safe
        collection.update_one({"_id": body["id"], "owner_id": DEFAULT_OWNER}, {"$set": {"original_text": source}})
        assert client.get(f'/reviews/{body["id"]}').json()["original_text"] == safe
