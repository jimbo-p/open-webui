"""Exercise native deferred tools through LiteLLM using synthetic, read-only tools.

Credentials come from the LITELLM_MASTER_KEY environment variable.
No real tool is executed. Reports include routing, cache usage and protocol types,
never credentials. Production configuration is never changed.
"""

from __future__ import annotations

import argparse
import copy
import json
import os
import ssl
import sys
import tempfile
import time
import urllib.error
import urllib.request
from pathlib import Path

SPEC = {
    'name': 'lookup_synthetic_order',
    'description': 'Read the status of a synthetic test order. No external side effects.',
    'parameters': {
        'type': 'object',
        'properties': {'order_id': {'type': 'string'}},
        'required': ['order_id'],
        'additionalProperties': False,
    },
}
SEARCH_SCHEMA = {
    'type': 'object',
    'properties': {'query': {'type': 'string'}},
    'required': ['query'],
    'additionalProperties': False,
}
PROMPT = 'Find the synthetic order lookup tool, then call it for order_id TEST-42. Do not guess its result.'


class Proxy:
    def __init__(self, base_url, key, timeout, stream=False):
        self.base_url = base_url.rstrip('/')
        self.key = key
        self.timeout = timeout
        self.records = []
        self.stream = stream
        self.ssl = ssl.create_default_context()
        bundle = Path(__file__).resolve().parents[1] / 'corp-ca.crt'
        if bundle.exists():
            self.ssl.load_verify_locations(str(bundle))

    def request(self, path, body=None):
        if body is not None:
            body = {**body, 'disable_fallbacks': True}
            if self.stream:
                body['stream'] = True
                if path == '/chat/completions':
                    body['stream_options'] = {'include_usage': True}
        headers = {
            'Authorization': f'Bearer {self.key}',
            'Content-Type': 'application/json',
        }
        if path == '/messages':
            headers['anthropic-version'] = '2023-06-01'
        req = urllib.request.Request(
            self.base_url + path,
            data=json.dumps(body).encode() if body is not None else None,
            headers=headers,
        )
        started = time.monotonic()
        try:
            with urllib.request.urlopen(req, timeout=self.timeout, context=self.ssl) as response:
                payload = self.read_stream(response, path) if self.stream and body is not None else json.load(response)
                record = {
                    'status': response.status,
                    'elapsed_s': round(time.monotonic() - started, 2),
                    'route': {
                        k: v
                        for k, v in response.headers.items()
                        if k.lower()
                        in {
                            'x-litellm-model-id',
                            'x-litellm-model-api-base',
                            'x-litellm-model-group',
                            'x-litellm-attempted-fallbacks',
                            'x-litellm-version',
                        }
                    },
                    'model': payload.get('model'),
                    'usage': payload.get('usage'),
                    'output_types': [i.get('type') for i in payload.get('output', [])],
                    'output_fields': [list(i) for i in payload.get('output', [])],
                    'output_namespaces': [
                        i.get('namespace') for i in payload.get('output', []) if i.get('type') == 'function_call'
                    ],
                    'content_types': [i.get('type') for i in payload.get('content', []) if isinstance(i, dict)],
                }
                self.records.append(record)
                print(json.dumps(record), flush=True)
                return payload
        except urllib.error.HTTPError as error:
            detail = error.read().decode('utf-8', 'replace')
            for secret in [
                self.key,
                *(
                    os.getenv(k)
                    for k in (
                        'AWS_ACCESS_KEY_ID',
                        'AWS_SECRET_ACCESS_KEY',
                        'AWS_SESSION_TOKEN',
                    )
                ),
            ]:
                if secret:
                    detail = detail.replace(secret, '[redacted]')
            record = {
                'status': error.code,
                'elapsed_s': round(time.monotonic() - started, 2),
                'error': detail[:1800],
            }
            self.records.append(record)
            print(json.dumps(record), flush=True)
            raise RuntimeError(f'HTTP {error.code}') from None

    @staticmethod
    def merge_chat_delta(message, calls, delta):
        for key in ('content', 'reasoning_content'):
            if delta.get(key):
                message[key] = message.get(key, '') + delta[key]
        for call in delta.get('tool_calls') or []:
            current = calls.setdefault(call['index'], {'type': 'function', 'function': {'arguments': ''}})
            if call.get('id'):
                current['id'] = call['id']
            function = call.get('function') or {}
            if function.get('name'):
                current['function']['name'] = function['name']
            current['function']['arguments'] += function.get('arguments') or ''

    @staticmethod
    def stream_events(response):
        for raw_line in response:
            line = raw_line.decode('utf-8').strip()
            if not line.startswith('data:') or line == 'data: [DONE]':
                continue
            event = json.loads(line.removeprefix('data:'))
            if event.get('error'):
                raise RuntimeError('Provider returned a streaming error')
            yield event

    @staticmethod
    def read_stream(response, path):
        message = {'role': 'assistant', 'content': ''}
        calls = {}
        result = {}
        completed = None
        for event in Proxy.stream_events(response):
            if path == '/responses':
                if event.get('type') == 'response.completed':
                    completed = event['response']
                continue
            result['model'] = event.get('model', result.get('model'))
            if event.get('usage'):
                result['usage'] = event['usage']
            for choice in event.get('choices') or []:
                Proxy.merge_chat_delta(message, calls, choice.get('delta') or {})
        if path == '/responses':
            if completed is None:
                raise RuntimeError('Responses stream ended without response.completed')
            return completed
        if calls:
            message['tool_calls'] = [calls[index] for index in sorted(calls)]
        return {**result, 'choices': [{'message': message}]}


def run_responses(proxy, model, hosted=False, padding=0, additional=False):
    function = {'type': 'function', **SPEC, 'defer_loading': True, 'strict': True}
    search = {
        'type': 'tool_search',
        'execution': 'client',
        'description': 'Find a synthetic order tool.',
        'parameters': SEARCH_SCHEMA,
    }
    tools = [function, {'type': 'tool_search'}] if hosted else [search]
    if additional:
        tools = [
            {
                'type': 'function',
                'name': 'search_tools',
                'description': 'Find a synthetic order tool.',
                'parameters': SEARCH_SCHEMA,
                'strict': False,
            }
        ]
    history = [{'role': 'user', 'content': PROMPT}]
    instructions = 'Use tools for the requested lookup. Report the returned status concisely.'
    if padding:
        instructions += '\n' + '\n'.join(
            f'Synthetic reference row {i}: this is stable test context, not an order status.' for i in range(padding)
        )
    base = {
        'model': model,
        'tools': tools,
        'max_output_tokens': 1024,
        'store': False,
        'instructions': instructions,
    }
    found_search = False
    found_call = False
    for _ in range(4):
        response = proxy.request('/responses', {**base, 'input': history})
        output = response.get('output') or []
        history.extend({k: v for k, v in item.items() if k not in {'created_by', 'status'}} for item in output)
        outputs = []
        for item in output:
            if item.get('type') == 'tool_search_call':
                found_search = True
                if item.get('execution') == 'client':
                    outputs.append(
                        {
                            'type': 'tool_search_output',
                            'execution': 'client',
                            'call_id': item['call_id'],
                            'status': 'completed',
                            'tools': [function],
                        }
                    )
            elif item.get('type') == 'function_call':
                if additional and item.get('name') == 'search_tools':
                    found_search = True
                    outputs.extend(
                        [
                            {
                                'type': 'function_call_output',
                                'call_id': item['call_id'],
                                'output': json.dumps({'tools': [SPEC]}),
                            },
                            {
                                'type': 'additional_tools',
                                'role': 'developer',
                                'tools': [{'type': 'function', **SPEC, 'strict': False}],
                            },
                        ]
                    )
                    continue
                assert item.get('name') == SPEC['name'], item.get('name')
                assert json.loads(item['arguments']) == {'order_id': 'TEST-42'}
                found_call = True
                outputs.append(
                    {
                        'type': 'function_call_output',
                        'call_id': item['call_id'],
                        'output': '{"status":"READY_TEST_ONLY"}',
                    }
                )
        if not outputs:
            break
        history.extend(outputs)
    assert found_search and found_call, f'search={found_search}, call={found_call}'
    assert any('READY_TEST_ONLY' in part.get('text', '') for item in output for part in item.get('content', [])), (
        'Missing final synthetic result'
    )
    return {
        'native_search': found_search,
        'synthetic_tool_called': found_call,
        'tools_unchanged': True,
    }


def run_anthropic(proxy, model, chat=False):
    if chat:
        tools = [
            {
                'type': 'function',
                'function': {
                    'name': 'search_tools',
                    'description': 'Find the synthetic order lookup tool.',
                    'parameters': SEARCH_SCHEMA,
                },
            },
            {'type': 'function', 'function': {**SPEC, 'defer_loading': True}},
        ]
    else:
        tools = [
            {
                'name': 'search_tools',
                'description': 'Find the synthetic order lookup tool.',
                'input_schema': SEARCH_SCHEMA,
            },
            {
                'name': SPEC['name'],
                'description': SPEC['description'],
                'input_schema': SPEC['parameters'],
                'defer_loading': True,
            },
        ]
    history = [{'role': 'user', 'content': PROMPT}]
    base = {'model': model, 'tools': tools, 'max_tokens': 1024, 'stream': False}
    found_search = False
    found_call = False
    for _ in range(4):
        response = proxy.request('/chat/completions' if chat else '/messages', {**base, 'messages': history})
        if chat:
            message = response['choices'][0]['message']
            history.append(message)
            calls = [
                (c['id'], c['function']['name'], json.loads(c['function']['arguments']))
                for c in message.get('tool_calls') or []
            ]
        else:
            history.append({'role': 'assistant', 'content': response['content']})
            calls = [(c['id'], c['name'], c['input']) for c in response['content'] if c['type'] == 'tool_use']
        results = []
        for call_id, name, args in calls:
            if name == 'search_tools':
                found_search = True
                content = [{'type': 'tool_reference', 'tool_name': SPEC['name']}]
            else:
                assert name == SPEC['name'] and args == {'order_id': 'TEST-42'}, (
                    name,
                    args,
                )
                found_call = True
                content = 'READY_TEST_ONLY'
            if chat:
                history.append({'role': 'tool', 'tool_call_id': call_id, 'content': content})
            else:
                results.append({'type': 'tool_result', 'tool_use_id': call_id, 'content': content})
        if not calls:
            break
        if not chat:
            history.append({'role': 'user', 'content': results})
    assert found_search and found_call, f'search={found_search}, call={found_call}'
    final_text = message.get('content') if chat else ''.join(c.get('text', '') for c in response['content'])
    assert 'READY_TEST_ONLY' in (final_text or ''), 'Missing final synthetic result'
    return {
        'native_reference': found_search,
        'synthetic_tool_called': found_call,
        'tools_unchanged': True,
    }


def run_webui(proxy, model, source, mode, padding):
    """Exercise the actual local WebUI adapter against the selected proxy."""
    if not source:
        raise ValueError('--webui-source is required for webui modes')
    sys.path.insert(0, str(source.resolve() / 'backend'))
    with tempfile.TemporaryDirectory(prefix='owui-native-probe-', ignore_cleanup_errors=True) as directory:
        os.environ.update(
            DATA_DIR=directory,
            STATIC_DIR=str(Path(directory) / 'static'),
            DATABASE_URL='sqlite:///' + str(Path(directory) / 'probe.db').replace('\\', '/'),
            ENABLE_DB_MIGRATIONS='false',
            OFFLINE_MODE='true',
            WEBUI_SECRET_KEY='synthetic-native-tool-probe-only',
        )
        Path(os.environ['STATIC_DIR']).mkdir()
        from open_webui.routers.openai import convert_to_responses_payload
        from open_webui.utils.tool_search import (
            add_discovered_tools,
            apply_native_tool_search,
            build_deferred_tools_manifest,
            search_tool_definitions,
        )

        registry = {SPEC['name']: {'type': 'external', 'spec': SPEC}}
        metadata = {'tools': registry, 'deferred_tools': list(registry)}
        instructions = 'Use tools for the requested lookup. Report the returned status concisely.\n'
        instructions += build_deferred_tools_manifest(registry, list(registry))
        instructions += '\n' + '\n'.join(
            f'Synthetic reference row {i}: this is stable test context, not an order status.' for i in range(padding)
        )
        messages = [
            {'role': 'system', 'content': instructions},
            {'role': 'user', 'content': PROMPT},
        ]
        base = {
            'model': model,
            'max_tokens': 1024,
            'tools': [
                {
                    'type': 'function',
                    'function': {
                        'name': 'search_tools',
                        'description': 'Find a synthetic order tool.',
                        'parameters': SEARCH_SCHEMA,
                    },
                }
            ],
        }
        initial_tools = None
        found_search = found_call = False
        for _ in range(4):
            payload = {**copy.deepcopy(base), 'messages': copy.deepcopy(messages)}
            add_discovered_tools(payload, metadata)
            if mode == 'openai':
                payload = convert_to_responses_payload(payload)
                payload['store'] = False
            apply_native_tool_search(payload, metadata, mode, is_responses=mode == 'openai')
            if initial_tools is None:
                initial_tools = payload['tools']
            assert payload['tools'] == initial_tools, 'Top-level tool definitions changed'
            response = proxy.request('/responses' if mode == 'openai' else '/chat/completions', payload)
            if mode == 'openai':
                # Replay the same ordinary calls/results WebUI saves between streaming rounds.
                output = response.get('output') or []
                calls = [
                    {
                        'id': item['call_id'],
                        'type': 'function',
                        'function': {
                            'name': item['name'],
                            'arguments': item['arguments'],
                        },
                    }
                    for item in output
                    if item.get('type') == 'function_call'
                ]
                final_text = ''.join(part.get('text', '') for item in output for part in item.get('content', []))
                messages.append({'role': 'assistant', 'content': final_text, 'tool_calls': calls})
            else:
                message = response['choices'][0]['message']
                messages.append(message)
                calls = message.get('tool_calls') or []
                final_text = message.get('content') or ''
            if not calls:
                break
            for call in calls:
                function = call['function']
                arguments = json.loads(function['arguments'])
                if function['name'] == 'search_tools':
                    result = search_tool_definitions(arguments['query'], 5, metadata)
                    found_search = True
                else:
                    assert function['name'] == SPEC['name'] and arguments == {'order_id': 'TEST-42'}
                    result = '{"status":"READY_TEST_ONLY"}'
                    found_call = True
                messages.append({'role': 'tool', 'tool_call_id': call['id'], 'content': result})
        assert found_search and found_call and 'READY_TEST_ONLY' in final_text
        # SQLite engines may retain handles on Windows until explicitly disposed.
        from open_webui.internal.db import engine

        engine.dispose()
        return {
            'webui_adapter': mode,
            'synthetic_tool_called': found_call,
            'tools_unchanged': True,
        }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        '--mode',
        choices=[
            'inspect',
            'responses-client',
            'responses-hosted',
            'responses-additional',
            'anthropic-messages',
            'anthropic-chat',
            'webui-openai',
            'webui-anthropic',
        ],
        required=True,
    )
    parser.add_argument('--model', default='gpt-6-sol-low')
    parser.add_argument('--base-url', required=True)
    parser.add_argument('--timeout', type=float, default=90)
    parser.add_argument('--padding', type=int, default=0)
    parser.add_argument('--webui-source', type=Path, default=Path(__file__).resolve().parents[1])
    parser.add_argument('--stream', action='store_true')
    parser.add_argument('--report', type=Path)
    args = parser.parse_args()
    key = os.environ.get('LITELLM_MASTER_KEY')
    if not key:
        raise SystemExit('Set LITELLM_MASTER_KEY in the environment before running the probe')
    if args.stream and args.mode == 'anthropic-messages':
        parser.error('--stream uses the Responses or Chat Completions wire format')
    proxy = Proxy(args.base_url, key, args.timeout, args.stream)
    summary = {
        'mode': args.mode,
        'model': args.model,
        'base_url': args.base_url,
        'stream': args.stream,
    }
    try:
        if args.mode == 'inspect':
            models = proxy.request('/models')
            summary['models'] = [m['id'] for m in models.get('data', []) if 'gpt-6' in m['id'] or 'claude' in m['id']]
        elif args.mode.startswith('responses'):
            summary.update(
                run_responses(
                    proxy,
                    args.model,
                    args.mode == 'responses-hosted',
                    args.padding,
                    args.mode == 'responses-additional',
                )
            )
        elif args.mode.startswith('webui-'):
            summary.update(
                run_webui(
                    proxy,
                    args.model,
                    args.webui_source,
                    args.mode.removeprefix('webui-'),
                    args.padding,
                )
            )
        else:
            summary.update(run_anthropic(proxy, args.model, args.mode == 'anthropic-chat'))
        if args.mode != 'inspect':
            assert all(
                r.get('route', {}).get('x-litellm-attempted-fallbacks') == '0'
                and r.get('route', {}).get('x-litellm-model-group') == args.model
                for r in proxy.records
            ), 'Proxy used a fallback or did not prove the requested route'
        summary['passed'] = True
    except Exception as error:  # noqa: BLE001 - CLI reports all probe failures and exits nonzero.
        summary.update(passed=False, error=f'{type(error).__name__}: {error}')
    summary['requests'] = proxy.records
    if args.report:
        args.report.parent.mkdir(parents=True, exist_ok=True)
        args.report.write_text(json.dumps(summary, indent=2) + '\n', encoding='utf-8')
    print(json.dumps(summary), flush=True)
    return 0 if summary['passed'] else 1


if __name__ == '__main__':
    raise SystemExit(main())
