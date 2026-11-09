import { afterEach, describe, expect, it } from 'vitest';
import { get } from 'svelte/store';
import {
	DEFAULT_KEYBINDINGS,
	keybindings,
	loadKeybindings,
	matchKeybinding,
	resetKeybindings,
	Shortcut,
	shortcuts
} from './shortcuts';

const escape = { key: 'Escape' } as KeyboardEvent;
const customClear = { key: 'x', altKey: true, shiftKey: true } as KeyboardEvent;

afterEach(resetKeybindings);

describe('clearing chat input selections', () => {
	it('leaves Escape available for dialogs without clearing selections by default', () => {
		expect(matchKeybinding(escape)).toBe(Shortcut.CLOSE_MODAL);
		expect(get(keybindings)[Shortcut.CLEAR_INPUT_SELECTIONS]).toBe('');
		expect(shortcuts[Shortcut.CLEAR_INPUT_SELECTIONS]?.keys).toEqual([]);
		expect(matchKeybinding({ key: 'Escape', shiftKey: true } as KeyboardEvent)).toBe(
			Shortcut.FOCUS_INPUT
		);
	});

	it('disables the legacy Escape binding saved with a full shortcut map', () => {
		const saved = {
			...DEFAULT_KEYBINDINGS,
			[Shortcut.CLEAR_INPUT_SELECTIONS]: 'Escape',
			[Shortcut.SEARCH]: 'Cmd+Shift+F'
		};
		loadKeybindings(saved);

		expect(matchKeybinding(escape)).toBe(Shortcut.CLOSE_MODAL);
		expect(get(keybindings)[Shortcut.CLEAR_INPUT_SELECTIONS]).toBe('');
		expect(get(keybindings)[Shortcut.SEARCH]).toBe('Cmd+Shift+F');
		expect(saved[Shortcut.CLEAR_INPUT_SELECTIONS]).toBe('Escape');
	});

	it('preserves an explicitly chosen alternative shortcut', () => {
		loadKeybindings({ [Shortcut.CLEAR_INPUT_SELECTIONS]: 'Alt+Shift+X' });

		expect(matchKeybinding(customClear)).toBe(Shortcut.CLEAR_INPUT_SELECTIONS);
		expect(matchKeybinding(escape)).toBe(Shortcut.CLOSE_MODAL);
	});

	it('keeps clearing unassigned after loading a disabled binding or resetting defaults', () => {
		loadKeybindings({ [Shortcut.CLEAR_INPUT_SELECTIONS]: '' });
		expect(matchKeybinding(escape)).toBe(Shortcut.CLOSE_MODAL);

		loadKeybindings({ [Shortcut.CLEAR_INPUT_SELECTIONS]: 'Alt+Shift+X' });
		resetKeybindings();
		expect(matchKeybinding(customClear)).toBeNull();
		expect(matchKeybinding(escape)).toBe(Shortcut.CLOSE_MODAL);
	});
});
