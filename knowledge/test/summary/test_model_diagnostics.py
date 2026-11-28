"""用模型协议故障定位摘要失败；诊断不包含模型原文或底层异常。"""
import json
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from knowledge.summary import strategies

ITEMS = [{"chunk_id": "source-1", "content": "如果风险较高，先核对仓位上限。"}]
EVIDENCE = [{"id": "index:sh000001", "text": "高风险，2026-11-27"}]
CHECK = {"chunk_id": "source-1", "quote": "先核对仓位上限", "evidence_ids": ["index:sh000001"]}


@pytest.fixture
def model_client(monkeypatch):
    client = Mock()
    client.__enter__ = Mock(return_value=client)
    client.__exit__ = Mock(return_value=False)
    monkeypatch.setattr(strategies, "OpenAI", lambda **kwargs: client)
    for key in ("OPEN_API_BASE", "OPEN_API_KEY", "LLM_DEFAULT_MODEL"):
        monkeypatch.setenv(key, "test-only")
    return client


@pytest.mark.parametrize("content,finish_reason,code", [
    ('private-model-text', 'length', 'model_incomplete'),
    (None, 'stop', 'model_invalid_response'),
    ('private-model-text', 'stop', 'model_invalid_json'),
    ('{"checks":[],"checks":[]}', 'stop', 'model_invalid_json'),
    (json.dumps({'checks': [{**CHECK, 'advice': 'private-model-text'}]}), 'stop', 'model_invalid_schema'),
    (json.dumps({'checks': [{**CHECK, 'chunk_id': 'private-model-text'}]}), 'stop', 'model_unknown_source'),
    (json.dumps({'checks': [{**CHECK, 'quote': 'private-model-text'}]}), 'stop', 'model_quote_mismatch'),
    (json.dumps({'checks': [{**CHECK, 'evidence_ids': ['private-model-text']}]}), 'stop', 'model_unknown_evidence'),
])
def test_failed_model_output_reports_fixed_reason_without_private_content(model_client, content, finish_reason, code):
    model_client.chat.completions.create.return_value = SimpleNamespace(choices=[
        SimpleNamespace(finish_reason=finish_reason, message=SimpleNamespace(content=content))])
    with pytest.raises(ValueError) as failure:
        strategies.StrategyChecklistModel().select(ITEMS, EVIDENCE)
    assert failure.value.code == code
    assert 'private-model-text' not in str(failure.value)
    model_client.chat.completions.create.assert_called_once()


@pytest.mark.parametrize('response', [SimpleNamespace(choices=[]), SimpleNamespace(), None])
def test_missing_response_shape_has_safe_reason(model_client, response):
    model_client.chat.completions.create.return_value = response
    with pytest.raises(ValueError) as failure:
        strategies.StrategyChecklistModel().select(ITEMS, EVIDENCE)
    assert failure.value.code == 'model_invalid_response'


def test_model_transport_error_is_classified_without_connection_details(model_client):
    model_client.chat.completions.create.side_effect = RuntimeError('private-url-secret')
    with pytest.raises(ValueError) as failure:
        strategies.StrategyChecklistModel().select(ITEMS, EVIDENCE)
    assert failure.value.code == 'model_unavailable'
    assert 'private-url-secret' not in str(failure.value)


def test_model_timeout_has_distinct_safe_reason(model_client):
    model_client.chat.completions.create.side_effect = TimeoutError('private-url-secret')
    with pytest.raises(ValueError) as failure:
        strategies.StrategyChecklistModel().select(ITEMS, EVIDENCE)
    assert failure.value.code == 'model_timeout'
    assert 'private-url-secret' not in str(failure.value)


def test_valid_selection_still_keeps_full_original_conditions(model_client):
    model_client.chat.completions.create.return_value = SimpleNamespace(choices=[
        SimpleNamespace(finish_reason='stop', message=SimpleNamespace(content=json.dumps({'checks': [CHECK]})))])
    assert strategies.StrategyChecklistModel().select(ITEMS, EVIDENCE) == [{**ITEMS[0], 'evidence': EVIDENCE}]
