"""Effective builtin-tool defaults respect explicit model choices."""

import copy
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from open_webui.utils import models


@pytest.mark.asyncio
@pytest.mark.parametrize(
    'local,default,expected',
    [
        (None, False, False),
        ({}, False, False),
        ({'files': True}, False, False),
        ({'subagents': True}, False, True),
        ({'subagents': False}, True, False),
    ],
)
async def test_effective_defaults_merge_categories_without_rewriting_saved_models(
    monkeypatch, local, default, expected
):
    saved_meta = {'capabilities': {'vision': False}}
    if local is not None:
        saved_meta['builtinTools'] = local
    defaults = {
        'builtinTools': {'subagents': default, 'memories': False},
        'capabilities': {'vision': True, 'file_upload': True},
    }
    original_meta = copy.deepcopy(saved_meta)
    original_defaults = copy.deepcopy(defaults)
    record = SimpleNamespace(
        id='model',
        name='Model',
        base_model_id=None,
        is_active=True,
        params=SimpleNamespace(model_dump=lambda: {}),
        model_dump=lambda: {'id': 'model', 'meta': copy.deepcopy(saved_meta), 'params': {}},
    )
    request = SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(redis=None, MODELS={}, BASE_MODELS=[])))
    monkeypatch.setattr(models, 'ENABLE_FUNCTIONS', False)
    monkeypatch.setattr(models.Config, 'get_many', AsyncMock(return_value={'models.default_metadata': defaults}))
    monkeypatch.setattr(models, 'get_all_base_models', AsyncMock(return_value=[{'id': 'model', 'owned_by': 'openai'}]))
    monkeypatch.setattr(models, 'publish_model_availability_events', AsyncMock())
    monkeypatch.setattr(models.Models, 'get_all_models', AsyncMock(return_value=[record]))
    monkeypatch.setattr(models.Functions, 'get_functions_by_ids', AsyncMock(return_value=[]))
    monkeypatch.setattr(models.Functions, 'get_function_valves_by_ids', AsyncMock(return_value={}))
    monkeypatch.setattr(models, 'get_functions_cache', lambda request: {})

    effective = (await models.get_all_models(request))[0]['info']['meta']

    assert effective['builtinTools']['subagents'] is expected
    assert effective['builtinTools']['memories'] is False
    assert effective['capabilities'] == {'vision': False, 'file_upload': True}
    if local and 'files' in local:
        assert effective['builtinTools']['files'] is local['files']
    assert saved_meta == original_meta
    assert defaults == original_defaults
