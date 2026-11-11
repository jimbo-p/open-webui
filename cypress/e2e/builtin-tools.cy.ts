/// <reference types="cypress" />

// Run against the candidate frontend with a disposable administrator session:
// OXYGPT_BROWSER_SESSION_TOKEN=<session JWT> CYPRESS_BASE_URL=<candidate URL> npx cypress run
// Model/default requests are intercepted; these tests never change shared model defaults.
type ToolMap = Record<string, boolean>;

const modelId = 'builtin-tools-editor-regression';
const baseModelId = 'builtin-tools-regression-base';
const modelUrl = `/workspace/models/edit?id=${modelId}`;
const switchFor = (label: string) => cy.get(`[role="switch"][aria-label="${label}"]`);
const openBuiltinTools = () => cy.contains('summary', /^Builtin Tools\b/).click();

describe('built-in tool category inheritance in the model editor', () => {
	let defaults: ToolMap;
	let savedModel: {
		id: string;
		name: string;
		base_model_id: string;
		write_access: boolean;
		is_active: boolean;
		params: Record<string, unknown>;
		meta: Record<string, unknown> & { builtinTools?: ToolMap };
	};

	const openEditor = () => {
		cy.visit(modelUrl);
		cy.location('pathname').should('equal', '/workspace/models/edit');
		cy.wait('@fixtureModel').its('response.statusCode').should('equal', 200);
		cy.wait('@modelDefaults').its('response.statusCode').should('equal', 200);
		cy.get('input[aria-label="Model Name"]').should(($input) => {
			expect($input.val()).to.equal(savedModel.name);
		});
		openBuiltinTools();
		switchFor('Notes').should('be.visible');
	};

	const saveAndReopen = (expected: ToolMap | undefined) => {
		cy.contains('button', 'Save & Update').should('be.enabled').click();
		cy.wait('@saveModel').then(({ request }) => {
			expect(request.body.meta.builtinTools).to.deep.equal(expected);
		});
		cy.location('pathname').should('equal', '/workspace/models');
		openEditor();
	};

	beforeEach(() => {
		defaults = { notes: false, chats: false, subagents: false };
		savedModel = {
			id: modelId,
			name: 'Builtin tools regression',
			base_model_id: baseModelId,
			write_access: true,
			is_active: true,
			params: { function_calling: 'native' },
			meta: {
				capabilities: { builtin_tools: true, file_upload: true, file_context: false },
				builtinTools: { time: true, future_category: false }
			}
		};
		cy.intercept('GET', '**/api/config', (request) => {
			request.continue((response) => {
				response.body.features = {
					...response.body.features,
					enable_notes: true,
					enable_subagents: true
				};
			});
		});
		cy.intercept('GET', '**/api/v1/users/user/settings*', { ui: { showChangelog: false } });
		cy.intercept('GET', '**/api/v1/tools/?*', []);
		cy.intercept('GET', '**/api/v1/functions/', []);
		cy.intercept('GET', '**/api/v1/models/tags', []);
		cy.intercept('GET', '**/api/v1/audio/voices', { voices: [] });
		cy.intercept('GET', '**/api/models*', {
			data: [{ id: baseModelId, name: 'Regression base model', owned_by: 'openai' }]
		});
		cy.intercept('GET', '**/api/v1/configs/models/defaults', (request) => {
			request.reply({
				DEFAULT_MODEL_METADATA: {
					capabilities: { builtin_tools: true },
					builtinTools: defaults
				},
				DEFAULT_FUNCTION_CALLING: 'native'
			});
		}).as('modelDefaults');
		cy.intercept('GET', '**/api/v1/models/model?*', (request) => {
			expect(new URL(request.url).searchParams.get('id')).to.equal(modelId);
			request.reply(savedModel);
		}).as('fixtureModel');
		cy.intercept('GET', '**/api/v1/auths/', (request) => request.continue()).as('sessionUser');
		cy.intercept('POST', '**/api/v1/models/model/update', (request) => {
			expect(request.body.id).to.equal(modelId);
			savedModel = { ...savedModel, ...request.body, write_access: true };
			request.reply(savedModel);
		}).as('saveModel');
		cy.task<string>('browserSessionToken', null, { log: false }).then((token) => {
			cy.visit('/', {
				onBeforeLoad(window) {
					window.localStorage.setItem('token', token);
					window.localStorage.setItem('locale', 'en-US');
					window.localStorage.setItem('settings', JSON.stringify({ showChangelog: false }));
				}
			});
		});
		// Vite's lazy route compilation can continue after the document's load event.
		cy.wait('@sessionUser', { requestTimeout: 120000 })
			.its('response.statusCode')
			.should('equal', 200);
	});

	afterEach(() => {
		cy.clearAllLocalStorage({ log: false });
		cy.clearAllSessionStorage({ log: false });
		cy.clearAllCookies({ log: false });
	});

	it('shows inherited disabled categories beside unrelated model overrides', () => {
		openEditor();
		switchFor('Notes').should('have.attr', 'aria-checked', 'false');
		switchFor('Chat History').should('have.attr', 'aria-checked', 'false');
		switchFor('Sub-agents').should('have.attr', 'aria-checked', 'false');
		cy.contains('summary', /^Sub-agents\b/).should('not.exist');
	});

	it('saves explicit true when enabling a category and retains it after OFF then ON', () => {
		openEditor();
		switchFor('Notes').click().should('have.attr', 'aria-checked', 'true');
		saveAndReopen({ time: true, future_category: false, notes: true });
		switchFor('Notes').should('have.attr', 'aria-checked', 'true').click();
		switchFor('Notes').should('have.attr', 'aria-checked', 'false').click();
		// Another change makes this a real save even though Notes returns to its prior state.
		cy.get('input[aria-label="Model Name"]').type(' updated');
		saveAndReopen({ time: true, future_category: false, notes: true });
		switchFor('Notes').should('have.attr', 'aria-checked', 'true');
	});

	it('honors explicit false over enabled defaults and explicit true over disabled defaults', () => {
		defaults = { notes: true, chats: false, subagents: false };
		savedModel.meta.builtinTools = { notes: false, chats: true, subagents: true };
		openEditor();
		switchFor('Notes').should('have.attr', 'aria-checked', 'false');
		switchFor('Chat History').should('have.attr', 'aria-checked', 'true');
		cy.contains('summary', /^Sub-agents\b/).should('exist');
		switchFor('Chat History').click();
		saveAndReopen({ notes: false, chats: false, subagents: true });
		switchFor('Chat History').should('have.attr', 'aria-checked', 'false');
	});

	it('keeps inherited categories sparse on unrelated saves and follows later default changes', () => {
		openEditor();
		cy.get('input[aria-label="Model Name"]').type(' renamed');
		saveAndReopen({ time: true, future_category: false });
		switchFor('Notes').should('have.attr', 'aria-checked', 'false');
		cy.then(() => {
			defaults = { notes: true, chats: true, subagents: false };
		});
		cy.reload();
		openBuiltinTools();
		switchFor('Notes').should('have.attr', 'aria-checked', 'true');
		switchFor('Chat History').should('have.attr', 'aria-checked', 'true');
	});

	for (const overrides of [undefined, {}]) {
		it(`preserves inheritance when a model starts with ${overrides ? 'an empty map' : 'no map'}`, () => {
			if (overrides) savedModel.meta.builtinTools = overrides;
			else delete savedModel.meta.builtinTools;
			openEditor();
			switchFor('Notes').should('have.attr', 'aria-checked', 'false');
			cy.get('input[aria-label="Model Name"]').type(' renamed');
			saveAndReopen(undefined);
			switchFor('Notes').should('have.attr', 'aria-checked', 'false');
		});
	}

	it('keeps categories enabled when neither administrator nor model configures them', () => {
		defaults = {};
		delete savedModel.meta.builtinTools;
		openEditor();
		switchFor('Notes').should('have.attr', 'aria-checked', 'true');
		switchFor('Chat History').should('have.attr', 'aria-checked', 'true');
		switchFor('Notes').click();
		saveAndReopen({ notes: false });
		switchFor('Notes').should('have.attr', 'aria-checked', 'false');
	});
});
