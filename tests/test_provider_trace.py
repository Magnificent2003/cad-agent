import pytest

from agent.launcher import build_config
from agent.provider import required_credentials, select_provider
from agent.trace import TaskTrace


def test_selection_does_not_mix_provider_credentials_or_protocol():
    env = {'LLM_PROVIDER': 'aiapi', 'AIAPI_MODEL': 'custom', 'AIAPI_API_KEY': 'fake-aiapi-key',
           'AIAPI_BASE_URL': 'https://example.invalid/v1/', 'AGENT_SERVER_PASSWORD': 'fake-password',
           'DEEPSEEK_BASE_URL': 'https://other.invalid', 'DEEPSEEK_MODEL': 'other'}
    selected = build_config(env)
    assert selected['enabled_providers'] == ['aiapi-cad']
    assert selected['model'] == selected['small_model'] == 'aiapi-cad/custom'
    assert selected['agent']['cad']['model'] == selected['model']
    provider = selected['provider']['aiapi-cad']
    assert provider['options'] == {'baseURL': 'https://example.invalid/v1', 'apiKey': '{env:AIAPI_API_KEY}'}
    assert 'interleaved' not in provider['models']['custom']
    assert required_credentials(env) == []
    env['LLM_PROVIDER'] = 'deepseek'
    assert required_credentials(env) == ['DEEPSEEK_API_KEY']
    deepseek = build_config(env)['provider']['deepseek-cad']
    assert deepseek['models']['other']['interleaved'] == {'field': 'reasoning_content'}
    with pytest.raises(ValueError, match='LLM_PROVIDER'):
        select_provider({'LLM_PROVIDER': 'typo'})


def test_transcript_keeps_full_tool_output_after_compaction_and_redacts(tmp_path):
    trace = TaskTrace(tmp_path, {'id': 't', 'session_id': 's', 'status': 'running'}, '真实图纸布置', ['key-for-test'])
    trace.previous = {'old'}
    trace.reconcile([{'info': {'id': 'old', 'role': 'assistant'}, 'parts': [{'id': 'old-p', 'type': 'text', 'text': '旧轮不应混入'}]}])
    trace.message({'id': 'm', 'role': 'assistant', 'modelID': 'selected'})
    output = 'A' * 90000 + '\n完整返回末尾\nAuthorization: Bearer key-for-test'
    part = {'id': 'p', 'messageID': 'm', 'type': 'tool', 'tool': 'cad_test',
            'state': {'status': 'completed', 'input': {'code': 'print(1)', 'api_key': 'some-other-secret'}, 'output': output}}
    trace.part(part)
    trace.part({**part, 'state': {'status': 'completed', 'output': 'pruned', 'time': {'compacted': 1}}})
    trace.note('automatic_recovery', {'reason': 'length'})
    trace.flush({'status': 'completed'})
    text = trace.path.read_text()
    assert '完整返回末尾' in text and 'A' * 90000 in text
    assert '旧轮不应混入' not in text and 'pruned' not in text
    assert 'key-for-test' not in text and 'some-other-secret' not in text
    assert 'print(1)' in text and 'automatic_recovery' in text and '**completed**' in text
    assert '(t.md)' in (trace.directory / 'latest.md').read_text()
    assert trace.path.stat().st_mode & 0o777 == 0o600


def test_error_metadata_and_stream_delta_do_not_leak_secrets(tmp_path):
    trace = TaskTrace(tmp_path, {'id': 't', 'session_id': 's'}, 'request', ['split-secret'])
    trace.part({'id': 'p', 'messageID': 'm', 'type': 'reasoning', 'text': 'split-'})
    trace.observe({'type': 'message.part.delta', 'properties': {'messageID': 'm', 'partID': 'p', 'field': 'text', 'delta': 'secret'}})
    trace.note('session.error', {'headers': {'Authorization': 'Bearer unlisted-key'}, 'url': 'https://user:password@host/path'})
    trace.flush({'status': 'failed', 'error': 'upstream error'})
    text = trace.path.read_text()
    for secret in ('split-secret', 'unlisted-key', 'user:password'):
        assert secret not in text
    assert '模型返回的推理' in text and '**failed**' in text
    assert 'upstream error' in text
