import { describe, expect, it } from 'vitest';
import { applyResponseStreamEvent } from './structuredOutput';

describe('corporate streaming overlay', () => {
	it('keeps a tool call and result that share identifiers as separate items', () => {
		let output = applyResponseStreamEvent([], {
			type: 'response.output_item.added',
			output_index: 0,
			item: {
				type: 'function_call',
				id: 'call-1',
				call_id: 'call-1',
				name: 'lookup',
				arguments: '{}'
			}
		});
		output = applyResponseStreamEvent(output, {
			type: 'response.output_item.done',
			output_index: 1,
			item: {
				type: 'function_call_output',
				id: 'call-1',
				call_id: 'call-1',
				output: [],
				status: 'completed'
			}
		});
		expect(output.map((item) => item.type)).toEqual(['function_call', 'function_call_output']);
	});

	it('preserves absolute positions when tool events arrive after unreported text', () => {
		let output = applyResponseStreamEvent([], {
			type: 'response.output_item.added',
			output_index: 2,
			item: {
				type: 'function_call',
				id: 'call-2',
				call_id: 'call-2',
				name: 'lookup',
				arguments: '{}'
			}
		});
		expect(output).toHaveLength(3);
		output = applyResponseStreamEvent(output, {
			type: 'response.output_item.done',
			output_index: 0,
			item: {
				type: 'message',
				id: 'text-1',
				content: [{ type: 'output_text', text: 'Checking...' }]
			}
		});
		expect(output[0].id).toBe('text-1');
		expect(output[2].id).toBe('call-2');
	});

	it('keeps earlier tool rounds when a subsequent response completes', () => {
		const output = applyResponseStreamEvent(
			[
				{ type: 'function_call', id: 'old-call', call_id: 'old-call' },
				{ type: 'function_call_output', id: 'old-result', call_id: 'old-call' }
			],
			{
				type: 'response.completed',
				response: { output: [{ type: 'message', id: 'answer', content: [] }] }
			}
		);
		expect(output.map((item) => item.id)).toEqual(['old-call', 'old-result', 'answer']);
	});
});
