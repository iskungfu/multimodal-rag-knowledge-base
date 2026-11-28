from datetime import datetime, timezone
from unittest.mock import Mock
from uuid import uuid4

import mongomock
import pytest
from fastapi.testclient import TestClient

from knowledge.api.query_api import create_app
from knowledge.api.review_api import get_review_service
from knowledge.review.extraction import ExtractionUnavailable
from knowledge.review.repository import ReviewRepository
from knowledge.service.review_service import ReviewService

OWNER = "default_user"


@pytest.fixture
def records():
    collection = mongomock.MongoClient(tz_aware=True).test.reviews
    repository = ReviewRepository(collection)
    extractor = Mock()
    extractor.extract.return_value = {"events": [], "lesson": None, "emotions": []}
    service = ReviewService(repository, extractor)
    original = service.save("今天计划买入，尚未成交。", uuid4())
    app = create_app(summary_enabled=False)
    app.dependency_overrides[get_review_service] = lambda: service
    with TestClient(app) as client:
        yield repository, extractor, service, original, client


def test_edit_original_is_redacted_before_model_and_survives_model_failure(records):
    repository, extractor, service, original, client = records
    extractor.extract.side_effect = ExtractionUnavailable("private model token")
    payload = {"text": "更正：尚未成交。手机号13900001234。", "revision": 1, "request_id": str(uuid4())}
    response = client.put('/reviews/' + original['id'], json=payload)
    assert response.status_code == 200
    result = response.json()
    assert result['revision'] == 2 and result['extraction_status'] == 'failed'
    assert result['extraction'] is None and result['index_status'] == 'pending'
    assert '13900001234' not in response.text and 'private' not in response.text
    assert repository.get(original['id'], OWNER)['original_text'] == result['original_text']
    assert extractor.extract.call_args.args == (result['original_text'],)
    assert client.put('/reviews/' + original['id'], json=payload).json()['revision'] == 2
    assert extractor.extract.call_count == 2
    assert client.put('/reviews/' + original['id'], json={**payload, 'text': '其他纠错'}).status_code == 409


def test_stale_revision_cannot_overwrite_and_legacy_record_can_be_corrected(records):
    repository, extractor, service, original, client = records
    repository.collection.update_one({'_id': original['id']}, {'$unset': {'revision': ''}})
    payload = {'text': '尚未成交，纠正计划。', 'revision': 1, 'request_id': str(uuid4())}
    assert client.put('/reviews/' + original['id'], json=payload).json()['revision'] == 2
    assert client.put('/reviews/' + original['id'], json={**payload, 'request_id': str(uuid4())}).status_code == 409
    assert repository.get(original['id'], OWNER)['original_text'] == payload['text']
    other_id = str(uuid4())
    repository.create(other_id, 'another_owner', '其他用户复盘')
    assert client.put('/reviews/' + other_id, json=payload).status_code == 404


def test_old_extraction_cannot_overwrite_newer_correction(records):
    repository, extractor, service, original, client = records
    def delayed_extraction(text):
        repository.edit(original['id'], OWNER, '第二次纠正：挂单已撤销。', 2, str(uuid4()))
        return {'events': [], 'lesson': '过时结果', 'emotions': []}
    extractor.extract.side_effect = delayed_extraction
    result = service.edit(original['id'], '第一次纠正。', 1, uuid4())
    assert result['revision'] == 3 and result['original_text'] == '第二次纠正：挂单已撤销。'
    assert result['extraction'] is None and result['extraction_status'] == 'pending'


def test_edit_immediately_unpublishes_old_index_and_fences_running_worker(records):
    repository, extractor, service, original, client = records
    now = datetime.now(timezone.utc)
    claim = repository.claim_index(original['id'], OWNER, now)
    assert repository.finish_index(claim, now, {'chunk': 'hash'})
    assert repository.published(original['id'], OWNER, claim['index_token'])
    def verify_invalidation(text):
        assert repository.published(original['id'], OWNER, claim['index_token']) is None
        assert not repository.finish_index(claim, now, {'old': 'hash'})
        return {'events': [], 'lesson': None, 'emotions': []}
    extractor.extract.side_effect = verify_invalidation
    service.edit(original['id'], '更正：计划未成交。', 1, uuid4())
    running = repository.claim_index(original['id'], OWNER, now)
    repository.edit(original['id'], OWNER, '再次更正。', 2, str(uuid4()))
    assert not repository.finish_index(running, now, {'stale': 'hash'})


@pytest.mark.parametrize('changes', [{'revision': 0}, {'revision': True}, {'owner_id': 'other'},
                                    {'text': '13900001234' * 1000}, {'request_id': '13900001234'}])
def test_edit_validation_does_not_echo_sensitive_input(records, changes):
    repository, extractor, service, original, client = records
    response = client.put('/reviews/' + original['id'], json={
        'text': '计划未成交。', 'revision': 1, 'request_id': str(uuid4()), **changes})
    assert response.status_code == 422 and '13900001234' not in response.text


@pytest.mark.parametrize('edit', [False, True])
def test_redaction_growth_rejected_before_write_or_invalidating_published_index(records, edit):
    repository, extractor, service, original, client = records
    now = datetime.now(timezone.utc)
    claim = repository.claim_index(original['id'], OWNER, now)
    assert repository.finish_index(claim, now, {'chunk': 'hash'})
    before = list(repository.collection.find())
    calls = extractor.extract.call_count
    raw = '甲' * 9984 + '13900001234持仓金额0'
    assert len(raw) == 10000
    payload = {'text': raw, 'request_id': str(uuid4())}
    response = (client.put('/reviews/' + original['id'], json={**payload, 'revision': 1})
                if edit else client.post('/reviews', json=payload))
    assert response.status_code == 422
    assert '脱敏后' in response.json()['detail'] and '13900001234' not in response.text
    assert list(repository.collection.find()) == before
    assert repository.published(original['id'], OWNER, claim['index_token'])
    assert extractor.extract.call_count == calls
    with pytest.raises(ValueError, match='脱敏后'):
        if edit:
            service.edit(original['id'], raw, 1, uuid4())
        else:
            service.save(raw, uuid4())
    assert list(repository.collection.find()) == before
    assert repository.get(original['id'], OWNER)['revision'] == 1
