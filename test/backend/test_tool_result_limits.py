"""Oversized tool results are cut to what a model can read instead of failing the turn."""

import json

import pytest
from open_webui import env
from open_webui.utils import middleware


def test_default_limit_is_above_every_configured_context_window():
    # 4M characters is roughly 1M tokens; every saved result above it ended in a failed turn.
    assert env.TOOL_RESULT_MAX_CHARS == 4_000_000


@pytest.mark.asyncio
async def test_results_within_the_limit_are_unchanged():
    result = 'x' * 1000
    processed, files, embeds = await middleware.process_tool_result(None, 'search_web', result, 'builtin')
    assert processed == result and files == [] and embeds == []


@pytest.mark.asyncio
async def test_an_oversized_result_keeps_its_start_and_says_how_much_was_cut(monkeypatch, caplog):
    monkeypatch.setattr(middleware, 'TOOL_RESULT_MAX_CHARS', 1000)
    result = {'rows': [f'row {index}' for index in range(5000)]}
    processed, _, _ = await middleware.process_tool_result(None, 'tool_search_microapp_post', result, 'external')
    serialized = json.dumps(result, indent=2, ensure_ascii=False)
    body, note = processed.split('\n\n[Tool result truncated: ', 1)
    assert body == serialized[:1000]
    assert note.startswith(f'only the first 1,000 of {len(serialized):,} characters are kept.')
    assert 'tool_search_microapp_post' in caplog.text


@pytest.mark.asyncio
async def test_a_truncated_file_read_adds_no_citation_copy(monkeypatch):
    monkeypatch.setattr(middleware, 'TOOL_RESULT_MAX_CHARS', 1000)
    result = json.dumps({'id': 'f1', 'filename': 'big.csv', 'content': 'a,b\n' * 10_000})
    processed, _, _ = await middleware.process_tool_result(None, 'view_file', result, 'builtin')
    assert len(processed) < 1200
    # The full file text is no longer duplicated into the message's sources.
    assert middleware.get_citation_source_from_tool_result('view_file', {}, processed) == []
    assert middleware.get_citation_source_from_tool_result('view_file', {}, result)[0]['document'] == ['a,b\n' * 10_000]
