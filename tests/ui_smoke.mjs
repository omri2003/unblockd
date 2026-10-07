// UI smoke test: python server on a copy of example/, headless Chromium, 2 viewports x 2 themes.
// Local: NODE_PATH=$(npm root -g) node tests/ui_smoke.mjs   (CI: npm i --no-save playwright)
import { createRequire } from 'node:module';
import { spawn, execFileSync } from 'node:child_process';
import { mkdtemp, cp } from 'node:fs/promises';
import { createServer } from 'node:net';
import { tmpdir } from 'node:os';
import { join, dirname } from 'node:path';
import { fileURLToPath } from 'node:url';

const { chromium } = createRequire(import.meta.url)('playwright'); // require, not import: ESM ignores NODE_PATH
const repo = join(dirname(fileURLToPath(import.meta.url)), '..');
const t0 = Date.now();
let server, browser;
const die = async (msg) => { console.error('UI SMOKE FAIL: ' + msg); await cleanup(); process.exit(1); };
const cleanup = async () => { server?.kill(); await browser?.close().catch(() => {}); };
const ok = (c, msg) => { if (!c) throw new Error(msg); };

try {
	// 1. temp copy of example/ + a claim and a split, so claimed cards and activity exist
	const root = await mkdtemp(join(tmpdir(), 'unblockd-ui-'));
	await cp(join(repo, 'example'), join(root, 'example'), { recursive: true });
	const cli = (...a) => execFileSync('python3', ['-m', 'unblockd.cli', '--dir', join(root, 'example/tickets'), ...a], { cwd: repo, stdio: 'pipe' });
	cli('create', '--prefix', 'SM', '--title', 'smoke ticket', '--json', '{"owner":"Claude"}');
	cli('claim', '--agent', 'smoke-1', '--note', 'smoke');
	cli('split', 'DOC-1', '--title', 'part one', '--title', 'part two');

	// 2. server on a free port
	const port = await new Promise((r) => { const s = createServer().listen(0, () => { const p = s.address().port; s.close(() => r(p)); }); });
	server = spawn('python3', ['-m', 'unblockd.web'], { cwd: repo, env: { ...process.env, UNBLOCKD_ROOT: root, PORT: String(port) }, stdio: 'ignore' });
	const base = `http://127.0.0.1:${port}/`;
	for (let i = 0; ; i++) {
		if (await fetch(base).then((r) => r.ok, () => false)) break;
		if (i > 50) await die('server did not answer on ' + base);
		await new Promise((r) => setTimeout(r, 100));
	}

	// 3+4. every viewport x theme
	browser = await chromium.launch();
	for (const [w, h] of [[1400, 900], [390, 844]]) for (const theme of ['light', 'dark']) {
		const tag = `${w}px/${theme}`;
		const ctx = await browser.newContext({ viewport: { width: w, height: h }, colorScheme: theme });
		await ctx.addInitScript((t) => { try { localStorage.setItem('theme', JSON.stringify(t)); } catch {} }, theme);
		const page = await ctx.newPage();
		page.setDefaultTimeout(5000);
		const errs = [];
		const ok = (c, msg) => { if (!c) throw new Error(msg + (errs.length ? '\n  browser errors so far:\n  ' + errs.join('\n  ') : '')); };
		page.on('pageerror', (e) => errs.push('pageerror: ' + e.message));
		page.on('console', (m) => m.type() === 'error' && errs.push('console.error: ' + m.text()));
		const visible = async (sel, what) => ok(await page.locator(sel).first().waitFor({ timeout: 3000 }).then(() => true, () => false), `[${tag}] ${what} (${sel}) not visible`);
		const noOverflow = async (view) => { // panels animate in, so give it a moment to settle
			await page.waitForFunction(() => document.documentElement.scrollWidth <= innerWidth, null, { timeout: 1500 }).catch(() => {});
			const sw = await page.evaluate(() => [document.documentElement.scrollWidth, innerWidth]);
			ok(sw[0] <= sw[1], `[${tag}] ${view}: horizontal overflow, scrollWidth ${sw[0]} > innerWidth ${sw[1]}`);
		};
		const toggle = async (sel, what) => { // open a <details> panel, check it, close it again
			await page.click(`${sel} > summary`);
			await visible(`${sel} .fbody`, what);
			await noOverflow(what);
			await page.click(`${sel} > summary`);
		};

		await page.goto(base);
		await page.waitForSelector('.card', { state: 'attached' });
		ok((await page.locator('section.col').count()) >= 4, `[${tag}] expected 4 board columns`);
		ok(await page.evaluate((t) => document.documentElement.dataset.theme === t, theme), `[${tag}] theme not applied`);
		ok(await page.locator('.col.doing .agc').count(), `[${tag}] claimed card chip missing (claim made via CLI)`);
		await noOverflow('board');

		await page.click('[data-view="graph"]');
		await visible('#graph', 'graph canvas');
		await noOverflow('graph');
		await page.click('[data-view="docs"]');
		await visible('#dStage .dn', 'docs strip node'); // #dSvg (edges) is display:none on mobile by design
		if (w > 600) await visible('#dSvg', 'docs strip svg');
		await noOverflow('docs');
		await page.click('[data-view="board"]');

		await toggle('#fpanel', 'Filters panel');
		await toggle('#spanel', 'Settings panel');
		await toggle('#apanel', 'Agents panel');

		await page.click('.card >> visible=true');
		await visible('#drawer', 'ticket drawer');
		await noOverflow('drawer');
		await page.click('[data-setstatus="done"]'); // status change through the UI -> toast
		await visible('#toasts .toast', 'status-change toast');
		await page.click('#toasts .toast button'); // Undo, so the next run sees the same board
		await visible('#toasts .toast:has-text("back in")', 'undo confirmation toast');
		await page.keyboard.press('Escape');

		ok(!errs.length, `[${tag}] browser errors:\n  ` + errs.join('\n  '));
		await ctx.close();
	}
	await cleanup();
	console.log(`UI smoke OK: board/graph/docs/drawer/filters/settings/agents/toast x 1400+390px x light+dark, no errors, no overflow (${((Date.now() - t0) / 1000).toFixed(1)}s)`);
} catch (e) { await die(e.message); }
