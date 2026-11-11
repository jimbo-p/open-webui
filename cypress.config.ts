import { defineConfig } from 'cypress';

export default defineConfig({
	video: false,
	screenshotOnRunFailure: false,
	viewportWidth: 1440,
	viewportHeight: 1100,
	e2e: {
		baseUrl: 'http://localhost:5173',
		supportFile: false,
		specPattern: 'cypress/e2e/**/*.cy.ts',
		setupNodeEvents(on) {
			on('task', {
				browserSessionToken() {
					const token = process.env.OXYGPT_BROWSER_SESSION_TOKEN;
					if (!token) {
						throw new Error(
							'Set OXYGPT_BROWSER_SESSION_TOKEN to a disposable administrator session JWT.'
						);
					}
					return token;
				}
			});
		}
	}
});
