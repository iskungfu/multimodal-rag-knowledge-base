from copy import deepcopy
from unittest.mock import Mock
from uuid import uuid4

import mongomock
import pytest
from fastapi.testclient import TestClient
from pymongo.errors import AutoReconnect

from knowledge.api.query_api import create_app
from knowledge.api.settings_api import get_repository
from knowledge.personalization.settings import SettingsRepository, SettingsRequest, settings_context
from knowledge.service.investment_service import InvestmentAssistant
from knowledge.utils.answer_policy import DISCLAIMER


@pytest.fixture
def settings():
    database = mongomock.MongoClient().test
    repository = SettingsRepository(database.settings, database.documents)
    identifier = str(uuid4())
    database.documents.insert_one({'_id': identifier, 'owner_id': 'default_user', 'doc_type': 'personal',
                                   'status': 'completed', 'title': '测试策略'})
    app = create_app(summary_enabled=False)
    app.dependency_overrides[get_repository] = lambda: repository
    with TestClient(app) as client:
        yield repository, identifier, client


def test_settings_persist_modes_redaction_and_revision_conflict(settings):
    repository, document_id, client = settings
    assert client.get('/investor-settings').json()['revision'] == 0
    mode_id = str(uuid4())
    payload = {'revision': 0, 'profile': {'risk_preference': 'cautious', 'notes': '手机号13900001234'},
               'modes': [{'id': mode_id, 'name': '谨慎策略', 'document_ids': [document_id]}], 'active_mode_id': mode_id}
    result = client.put('/investor-settings', json=payload)
    assert result.status_code == 200 and result.json()['revision'] == 1
    assert '13900001234' not in result.text and '13900001234' not in str(repository.collection.find_one())
    assert client.get('/investor-settings').json() == result.json()
    assert client.put('/investor-settings', json=payload).status_code == 409
    assert settings_context(result.json())['document_ids'] == [document_id]
    assert client.put('/investor-settings', json={'revision': 1}).json()['active_mode_id'] is None


def test_imported_hex_document_id_is_not_reformatted(settings):
    repository, document_id, client = settings
    imported_id = uuid4().hex
    repository.documents.insert_one({'_id': imported_id, 'owner_id': 'default_user',
                                      'doc_type': 'personal', 'status': 'completed'})
    mode_id = str(uuid4())
    response = client.put('/investor-settings', json={'revision': 0, 'active_mode_id': mode_id,
        'modes': [{'id': mode_id, 'name': '真实导入编号', 'document_ids': [imported_id]}]})
    assert response.status_code == 200
    assert settings_context(response.json())['document_ids'] == [imported_id]


@pytest.mark.parametrize('kind', ['missing', 'foreign', 'processing', 'public'])
def test_mode_rejects_unavailable_or_other_owner_documents(settings, kind):
    repository, document_id, client = settings
    if kind == 'missing':
        repository.documents.delete_one({'_id': document_id})
    else:
        values = {'foreign': {'owner_id': 'other'}, 'processing': {'status': 'processing'},
                  'public': {'owner_id': 'public', 'doc_type': 'knowledge'}}[kind]
        repository.documents.update_one({'_id': document_id}, {'$set': values})
    payload = {'revision': 0, 'modes': [{'id': str(uuid4()), 'name': '策略', 'document_ids': [document_id]}]}
    assert client.put('/investor-settings', json=payload).status_code == 422
    assert not client.get('/investor-settings/documents').json()['items']
    assert repository.collection.count_documents({}) == 0


def test_unknown_mode_and_private_validation_errors_do_not_write(settings):
    repository, document_id, client = settings
    for payload in ({'revision': 0, 'active_mode_id': str(uuid4())},
                    {'revision': 0, 'profile': {'risk_preference': '13900001234'}},
                    {'revision': 0, 'profile': {'notes': '13900001234' * 100}},
                    {'revision': 0, 'owner_id': '13900001234'}):
        response = client.put('/investor-settings', json=payload)
        assert response.status_code == 422 and '13900001234' not in response.text
    assert repository.collection.count_documents({}) == 0


def test_settings_storage_failure_does_not_echo_credentials(settings, monkeypatch):
    repository, document_id, client = settings
    monkeypatch.setattr(repository, 'save', Mock(side_effect=AutoReconnect('private-token')))
    response = client.put('/investor-settings', json={'revision': 0})
    assert response.status_code == 503 and 'private-token' not in response.text


@pytest.mark.parametrize('profile,modes', [
    ({'notes': '甲' * 993 + '持仓金额：0元'}, []),
    ({'preferred_sectors': ['甲' * 33 + '持仓金额：0元']}, []),
    ({}, [{'id': str(uuid4()), 'name': '甲' * 53 + '持仓金额：0元', 'document_ids': []}]),
])
def test_redaction_growth_is_rejected_before_writing_and_settings_remain_readable(settings, profile, modes):
    repository, document_id, client = settings
    existing = client.put('/investor-settings', json={'revision': 0, 'profile': {'notes': '已保存的偏好'}}).json()
    response = client.put('/investor-settings', json={'revision': 1, 'profile': profile, 'modes': modes})
    assert response.status_code == 422 and '脱敏后的内容' in response.json()['detail']
    assert client.get('/investor-settings').json() == existing


def test_assistant_reads_new_mode_each_turn_and_does_not_change_risk(settings):
    repository, document_id, client = settings
    mode_id = str(uuid4())
    saved = repository.save(SettingsRequest.model_validate({'revision': 0,
        'profile': {'risk_preference': 'cautious', 'horizon': 'long'},
        'modes': [{'id': mode_id, 'name': '只用这一套', 'document_ids': [document_id]}], 'active_mode_id': mode_id}))
    retriever = Mock()
    retriever.retrieve.return_value = {kind: {'status': 'empty', 'items': []} for kind in ('personal', 'knowledge', 'review')}
    assistant = InvestmentAssistant(retriever, reranker=lambda query, docs: (docs, 'empty'), settings=repository.read)
    market = [{'id': 'stock:000001', 'text': '高风险，测试日期2026-11-27'}]
    result = assistant.answer('核对我的风险', market)
    assert retriever.retrieve.call_args.kwargs['document_ids'] == [document_id]
    assert '谨慎' in retriever.retrieve.call_args.args[0] and '长期' in retriever.retrieve.call_args.args[0]
    assert '高风险' in result['answer'] and result['answer'].count(DISCLAIMER) == 1
    assert result['personalization']['mode_name'] == '只用这一套'
    updated = deepcopy(saved)
    updated['modes'][0]['document_ids'] = []
    repository.save(SettingsRequest.model_validate(updated))
    assistant.answer('再核对一次', market)
    assert retriever.retrieve.call_args.kwargs['document_ids'] == []


def test_profile_failure_never_silently_reverts_to_all_strategies():
    retriever = Mock()
    assistant = InvestmentAssistant(retriever, settings=Mock(side_effect=RuntimeError('unavailable')))
    with pytest.raises(RuntimeError):
        assistant.answer('我的策略')
    retriever.retrieve.assert_not_called()
