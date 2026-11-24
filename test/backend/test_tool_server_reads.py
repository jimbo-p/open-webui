"""Tool servers attached to a model are resolved from one registry read per request."""

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from open_webui.utils import tools as tool_utils


@pytest.mark.asyncio
async def test_attached_tool_servers_are_read_once_per_request(monkeypatch):
    servers = [
        {'id': f'server-{index}', 'idx': index, 'url': f'http://tools/{index}', 'specs': [{'name': f'tool_{index}'}]}
        for index in range(13)
    ]
    connections = [{'url': server['url'], 'config': {'enable': True}} for server in servers]
    get_servers = AsyncMock(return_value=servers)
    config_keys = []

    async def config_get(key, default=None):
        config_keys.append(key)
        return connections if key == 'tool_server.connections' else default

    monkeypatch.setattr(tool_utils, 'get_tool_servers', get_servers)
    monkeypatch.setattr(tool_utils.Config, 'get', config_get)
    monkeypatch.setattr(tool_utils.Groups, 'get_groups_by_member_id', AsyncMock(return_value=[]))
    monkeypatch.setattr(tool_utils, 'has_connection_access', AsyncMock(return_value=True))
    user = SimpleNamespace(id='user-1', role='user')
    request = SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace()), state=SimpleNamespace())

    tool_ids = [f'server:{server["id"]}' for server in servers] + ['server:missing']
    tools = await tool_utils.get_tools(request, tool_ids, user, {})
    assert sorted(tools) == sorted(f'tool_{index}' for index in range(13))
    assert get_servers.await_count == 1
    assert config_keys.count('tool_server.connections') == 1
    assert {tool['tool_id'] for tool in tools.values()} == set(tool_ids[:-1])


@pytest.mark.asyncio
async def test_requests_without_tool_servers_do_not_read_the_registry(monkeypatch):
    get_servers = AsyncMock(return_value=[])
    monkeypatch.setattr(tool_utils, 'get_tool_servers', get_servers)
    monkeypatch.setattr(tool_utils.Groups, 'get_groups_by_member_id', AsyncMock(return_value=[]))
    monkeypatch.setattr(tool_utils.Tools, 'get_tools_by_ids', AsyncMock(return_value={}))
    user = SimpleNamespace(id='user-1', role='user')
    request = SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace()), state=SimpleNamespace())
    assert await tool_utils.get_tools(request, ['local_tool'], user, {}) == {}
    assert get_servers.await_count == 0
