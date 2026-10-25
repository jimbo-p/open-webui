import { describe, expect, it } from 'vitest';
import { applyResponseStreamEvent } from './structuredOutput';

describe('native tool streaming', () => {
	it('updates a resolved dispatcher call and keeps its result separate', () => {
		const provider = { name: 'call_tool', arguments: '{"tool_name":"lookup","params":"{}"}' };
		const call = { type: 'function_call', id: 'dispatch', call_id: 'dispatch', ...provider };
		let output = applyResponseStreamEvent([call], {
			type: 'response.output_item.done',
			output_index: 0,
			item: { ...call, name: 'lookup', arguments: '{}', provider_function: provider }
		});
		output = applyResponseStreamEvent(output, {
			type: 'response.output_item.done',
			output_index: 1,
			item: {
				type: 'function_call_output',
				id: 'result',
				call_id: 'dispatch',
				status: 'completed',
				output: [{ type: 'text', text: 'Found the requested data.' }]
			}
		});
		expect(output).toHaveLength(2);
		expect(output[0].name).toBe('lookup');
		expect(output[0].provider_function).toEqual(provider);
		expect(output[1].type).toBe('function_call_output');
		expect(output[1].status).toBe('completed');
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
