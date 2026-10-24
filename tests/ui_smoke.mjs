// UI smoke test: python server on a copy of example/, headless Chromium, 2 viewports x 2 themes.
// Local: NODE_PATH=$(npm root -g) node tests/ui_smoke.mjs   (CI: npm i --no-save playwright)
import { createRequire } from 'node:module';
import { spawn, execFileSync } from 'node:child_process';
import { mkdtemp, cp, readFile, writeFile, readdir, rm, mkdir } from 'node:fs/promises';
import { createServer } from 'node:net';
import { tmpdir } from 'node:os';
import { join, dirname } from 'node:path';
import { fileURLToPath } from 'node:url';

const { chromium } = createRequire(import.meta.url)('playwright'); // require, not import: ESM ignores NODE_PATH
const repo = join(dirname(fileURLToPath(import.meta.url)), '..');
// the board refetches when its tab becomes visible again: fire that instead of waiting for the 4 s poll, wait for the reply and a frame to render it
const backOnTab = async (page) => {
	await Promise.all([page.waitForResponse((r) => /\/tickets(\?|$)/.test(r.url())), page.evaluate(() => document.dispatchEvent(new Event('visibilitychange')))]);
	await page.evaluate(() => new Promise((r) => requestAnimationFrame(() => setTimeout(r))));
};
const t0 = Date.now();
let server, browser, lastPage;
const die = async (msg) => { console.error('UI SMOKE FAIL: ' + msg); if (process.env.DEBUG && lastPage) { console.error(JSON.stringify(lastPage.viewportSize())); await lastPage.screenshot({ path: process.env.DEBUG + '/fail.png' }).catch(() => {}); } if (process.env.DEBUG && lastPage) console.error(await lastPage.evaluate(() => 'TOASTS: ' + document.getElementById('toasts').innerText + '\nPANEL: ' + document.getElementById('npanel').innerText.slice(0, 600)).catch(() => '')); await cleanup(); process.exit(1); };
const cleanup = async () => { server?.kill(); await browser?.close().catch(() => {}); };
const ok = (c, msg) => { if (!c) throw new Error(msg); };

try {
	// 1. temp copy of example/ + a claim and a split, so claimed cards and activity exist
	const root = await mkdtemp(join(tmpdir(), 'unblockd-ui-'));
	await cp(join(repo, 'example'), join(root, 'example'), { recursive: true });
	const cli = (...a) => execFileSync('python3', ['-m', 'unblockd.cli', '--dir', join(root, 'example/tickets'), ...a], { cwd: repo, stdio: 'pipe' });
	const cliB = (...a) => execFileSync('python3', ['-m', 'unblockd.cli', '--dir', join(root, 'zsecond/tickets'), ...a], { cwd: repo, stdio: 'pipe' }).toString();
	await cp(join(repo, 'example'), join(root, 'zsecond'), { recursive: true }); // second board: the title becomes a switcher
	// the committed example dates age (a doing ticket goes "stuck" after stale_days): restamp both copies to today so the run never depends on the date
	const today = new Date().toISOString().slice(0, 10);
	for (const b of ['example', 'zsecond']) for (const f of (await readdir(join(root, b, 'tickets'))).filter(n => /^[A-Z]+-\d+\.json$/.test(n))) {
		const p = join(root, b, 'tickets', f);
		await writeFile(p, JSON.stringify({ ...JSON.parse(await readFile(p, 'utf8')), created: today, updated: today }, null, 2));
	}
	const cfgP = join(root, 'zsecond/tickets/unblockd.json'), cfg = JSON.parse(await readFile(cfgP, 'utf8'));
	await writeFile(cfgP, JSON.stringify({ ...cfg, id: 'zsecond', title: 'Second board', agent_tickets: 'propose', schema: { prefixes: { ZQ: 'questions', ZP: 'proposed work' }, required: ['done_when', 'priority'], tags: ['bug', 'chore'], body_template: '## Why\n' } }));
	cli('create', '--prefix', 'SM', '--title', 'smoke ticket', '--json', '{"owner":"Claude","pillar":"API"}');
	cli('claim', '--agent', 'backend-1', '--note', 'smoke');
	cli('create', '--prefix', 'SM', '--title', 'smoke ticket with a quiet worker', '--json', '{"owner":"Claude","pillar":"API"}');
	cli('claim', '--agent', 'backend-2', '--note', 'quiet one');
	{ const cp = join(root, 'example/tickets/.unblockd/claims.json'), cl = JSON.parse(await readFile(cp, 'utf8')); // age ONE heartbeat (8 of 15 min, just over half) so the quiet state shows
		for (const c of Object.values(cl)) if (c.agent === 'backend-2') c.heartbeat_at = new Date(Date.now() - 8 * 60000).toISOString().replace(/\.\d+Z$/, '+00:00');
		await writeFile(cp, JSON.stringify(cl)); }
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
		page.setDefaultTimeout(5000); lastPage = page;
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
		const settle = () => page.waitForFunction(() => document.getAnimations().every((a) => a.playState !== 'running' || a.effect?.getTiming().iterations === Infinity)); // view cross-fade, map zoom: done (the endless live-dot pulse aside), instead of a fixed pause
		const refresh = () => backOnTab(page); // what the 4 s poll does, without waiting for it

		if (theme === 'dark') { // dark changes colours, not behaviour: the full flow runs in light; here every view and the drawer render with no errors and no overflow
			await page.goto(base);
			await page.waitForSelector('.card', { state: 'attached' });
			ok(await page.evaluate((t) => document.documentElement.dataset.theme === t, theme), `[${tag}] theme not applied`);
			await noOverflow('board');
			for (const v of ['graph', 'docs', 'map', 'roadmap', 'board']) {
				await page.click(`#viewSeg [data-view="${v}"]`);
				await page.waitForFunction((x) => document.querySelector(`#viewSeg [data-view="${x}"]`)?.getAttribute('aria-pressed') === 'true', v);
				await settle();
				await noOverflow(v);
			}
			await page.evaluate(() => document.querySelector('#board .card[data-open]').click());
			await visible('#drawer #dTitle', 'drawer');
			await noOverflow('drawer');
			await page.keyboard.press('Escape');
			ok(!errs.length, `[${tag}] browser errors:\n  ` + errs.join('\n  '));
			await ctx.close();
			continue;
		}

		const oid = JSON.parse(cliB('create', '--prefix', 'ZQ', '--title', 'other board question', '--json', '{"owner":"Claude"}')).id; // before the page loads: the other board is only re-read on the 15 s needs poll
		cliB('claim', '--agent', 'backend-1', '--id', oid); cliB('ask', oid, '--agent', 'backend-1', '--question', 'Which colour?');
		await page.goto(base);
		await page.waitForSelector('.card', { state: 'attached' });
		ok((await page.locator('section.col').count()) >= 4, `[${tag}] expected 4 board columns`);
		ok(await page.evaluate((t) => document.documentElement.dataset.theme === t, theme), `[${tag}] theme not applied`);
		ok(await page.locator('.col.doing .liv').count(), `[${tag}] claimed card liveness line missing (claim made via CLI)`);
		ok(/^live · /.test(await page.locator('.col.doing .liv:not(.quiet) [data-hb]').first().innerText()), `[${tag}] a fresh claim should read "live ·"`);
		ok(/ of \d+ min lease used/.test(await page.locator('.col.doing .lbar').first().getAttribute('aria-label')), `[${tag}] lease bar needs a label`);
		ok(await page.locator('.col.doing .own svg').count() > 0, `[${tag}] owner shows a glyph`);
		ok(!(await page.locator('#strip, #working, #sub, #kbHint').count()), `[${tag}] strip, Working now, subtitle and the shortcuts link are gone`);
		ok(/^Agents · \d+ live$/.test(await page.locator('#agBtn').innerText()), `[${tag}] Agents button counts live agents`);
		ok(/^\d+$/.test(await page.locator('.col.todo .colhead .n').innerText()), `[${tag}] To do head shows a plain count`);
		{ const wh = await page.locator('.col.pending .colhead h2').innerText(), wl = await page.locator('[data-col=pending] .lane').allInnerTexts(); ok(wh === 'Waiting' && wl.some((x) => /^on other work/i.test(x)), `[${tag}] Waiting column with "On other work" section: ${wh} / ${wl}`); }
		{ // the second claim's heartbeat was aged 8 min in the temp copy (lease 15): it reads "quiet ·", the fresh one "live ·"
			const q = await page.locator('.col.doing .liv.quiet [data-hb]').allInnerTexts();
			ok(q.length === 1 && /^quiet · \d+ min$/.test(q[0]), `[${tag}] one stale claim should read "quiet · N min", got ${q}`);
			ok(/^\d+ of 15 min lease used$/.test(await page.locator('.col.doing .liv.quiet + .lbar').getAttribute('aria-label')), `[${tag}] lease bar reads N of 15 min`);
		}
		ok(await page.evaluate(() => /unblockd/.test(document.title) && !!document.querySelector('link[rel=icon]')), `[${tag}] title / favicon missing`);
		await noOverflow('board');
		await page.evaluate(() => { const k = Object.keys(localStorage).find((x) => x.startsWith('unblockd.lastSeen.')); localStorage.setItem(k, JSON.stringify('2020-01-01T00:00:00Z')); }); // digest: expand/collapse runs the grid-rows transition
		await page.reload(); await page.waitForSelector('.card', { state: 'attached' });
		await visible('#digest [data-dgtoggle]', 'digest bar');
		await page.click('#digest [data-dgtoggle]');
		await page.waitForFunction(() => document.querySelector('#digest .dg-body').getBoundingClientRect().height > 0 && document.querySelector('[data-dgtoggle]')?.getAttribute('aria-expanded') === 'true', null, { timeout: 2000 }).catch(() => ok(false, `[${tag}] digest did not expand`));
		await page.click('#digest [data-dgtoggle]');
		await page.waitForFunction(() => document.querySelector('#digest .dg-body').getBoundingClientRect().height === 0, null, { timeout: 2000 }).catch(() => ok(false, `[${tag}] digest did not collapse`));
		await page.click('#digest [data-dgdismiss]'); await page.waitForSelector('#digest', { state: 'hidden' });

		const title0 = await page.locator('#appTitle').innerText(); // board switcher: the title is the menu
		ok(!(await page.locator('#bsum').evaluate((e) => e.inert)), `[${tag}] two boards: title should be a button`);
		await page.click('#bsum');
		await visible('#bbody .bi', 'board list');
		ok((await page.locator('#bbody .bi').count()) === 3, `[${tag}] board list: 2 boards + New board`);
		ok((await page.locator('#bbody [aria-current=true]').innerText()).includes(title0), `[${tag}] current board not marked`);
		await noOverflow('board list');
		await page.keyboard.press('Escape');
		ok(!(await page.locator('#bpanel').evaluate((e) => e.open)), `[${tag}] Escape should close the board list`);
		if (w > 600) { await page.keyboard.press('Shift+B'); ok(await page.locator('#bpanel').evaluate((e) => e.open), `[${tag}] Shift+B should open the board list`); await page.keyboard.press('Escape'); }
		await page.click('#bsum');
		await page.waitForFunction(() => document.activeElement.classList.contains('bi')); // the list focuses the current board
		await page.keyboard.press('ArrowDown'); await page.keyboard.press('Enter'); // focus starts on the current board, so Down = the other one
		await page.waitForURL(/board=zsecond/);
		await page.waitForFunction(() => document.getElementById('appTitle').textContent === 'Second board');
		await page.click('#bsum'); await page.click('#bbody [data-bd="example"]'); // and back
		await page.waitForFunction((t) => document.getElementById('appTitle').textContent === t, title0);
		await page.waitForSelector('.card', { state: 'attached' });
		await page.evaluate(() => document.getElementById('kbBtn').click()); // hidden at <=600px (no keyboard there), so click it directly
		await visible('#kb', 'shortcuts sheet from the hint');
		ok((await page.locator('#kb').innerText()).includes('Shift'), `[${tag}] ? sheet should mention Shift-click select`);
		const sp = await browser.newPage({ viewport: { width: w, height: h } }); // fresh page: an unknown board names itself
		await sp.goto(base + '?board=nope');
		ok(await sp.waitForSelector('#toasts .toast:has-text("nope")', { timeout: 3000 }).then(() => true, () => false), `[${tag}] an unknown board's toast should name it`);
		await sp.close();
		await page.keyboard.press('Escape');

		if (w >= 1100) { // one header row: the first column starts near the top of the page
			const top = await page.evaluate(() => document.querySelector('.col').getBoundingClientRect().top + scrollY);
			ok(top < 200, `[${tag}] header should be one row, the first column starts at ${top}px`);
		}
		await page.click('[data-view="graph"]');
		await visible('#graph', 'graph canvas');
		await noOverflow('graph');
		await page.click('[data-view="docs"]');
		await visible('#dStage .dn', 'docs strip node'); // #dSvg (edges) is display:none on mobile by design
		if (w > 600) await visible('#dSvg', 'docs strip svg');
		await noOverflow('docs');
		{ // Roadmap view: example has 2 milestones (v0.1 is next) and one unscheduled ticket; a milestone opens the board filtered to it
			await page.click('[data-view="roadmap"]');
			await page.waitForFunction(() => document.querySelectorAll('#roadWrap .rmi:not(.run)').length === 2);
			ok(await page.getAttribute('#viewSeg [data-view="roadmap"]', 'title') === 'Roadmap (r)', `[${tag}] Roadmap tooltip should carry its key`);
			const first = page.locator('#roadWrap .rmi').first();
			ok((await first.getAttribute('class')).includes('rnext') && (await first.getAttribute('class')).includes('open'), `[${tag}] roadmap: v0.1 is Next and starts expanded`);
			ok((await first.locator('.rbar').getAttribute('aria-label')).includes('done'), `[${tag}] roadmap: the bar says its states in words`);
			const chips = await first.locator('.rch').evaluateAll((c) => c.map((x) => x.dataset.open));
			const v01ids = await page.evaluate(async () => (await (await fetch('/api/tickets?milestone=v0.1')).json()).map((t) => t.id));
			ok(chips.length === v01ids.length && chips.every((id) => v01ids.includes(id)), `[${tag}] roadmap: v0.1's tickets as chips: ${chips} vs ${v01ids}`);
			ok((await page.locator('#roadWrap .rmi.run').innerText()).includes('Unscheduled') && !(await page.locator('#roadWrap .rmi.run').getAttribute('class')).includes('open'), `[${tag}] roadmap: the unscheduled row, collapsed`);
			await page.click('#roadWrap .rmi.run [data-rmx]'); // expand: its open tickets
			await page.waitForFunction(() => document.querySelector('#roadWrap .rmi.run.open .rch'));
			await page.click('#roadWrap .rmi.run [data-rmx]'); // and collapse
			await page.waitForFunction(() => !document.querySelector('#roadWrap .rmi.run.open'));
			await noOverflow('roadmap');
			await first.locator('.rch').first().click(); // a chip opens its ticket
			await visible('#drawer #dTitle', 'ticket from a roadmap chip');
			await page.keyboard.press('Escape');
			if (process.env.SHOTS) await page.screenshot({ path: `${process.env.SHOTS}/roadmap-${w}-${theme}.png` });
			await settle(); // the view cross-fade swallows pointer events
			await page.click('#roadWrap [data-msgo="v0.1"]');
			await page.waitForFunction(() => document.querySelector('#viewSeg [data-view="board"]')?.getAttribute('aria-pressed') === 'true');
			const v01 = await page.evaluate(async () => (await (await fetch('/api/tickets?milestone=v0.1')).json()).map((t) => t.id));
			const shown = await page.evaluate(() => [...new Set([...document.querySelectorAll('#board .card')].map((c) => c.dataset.open))]);
			ok(shown.length && shown.every((id) => v01.includes(id)), `[${tag}] milestone filter: ${shown} should all be in v0.1 (${v01})`);
			ok(new URL(page.url()).searchParams.get('milestone') === 'v0.1', `[${tag}] the milestone filter should be in the URL`);
			await page.click('#pills [data-ms]');
			await page.waitForFunction(() => !new URL(location.href).searchParams.has('milestone'));
		}
		await page.click('[data-view="board"]');

		{ // Map view: example board has 4 component notes (docs/arch); UI-2 spans api + web-ui, DOC-1 maps to nothing
			const view = (v) => page.waitForFunction((x) => document.querySelector(`#viewSeg [data-view="${x}"]`)?.getAttribute('aria-pressed') === 'true', v);
			await page.waitForFunction(() => document.querySelector('#viewSeg [data-view="board"]')?.getAttribute('aria-pressed') === 'true');
			await page.keyboard.press('m'); await view('map'); // the shortcut
			ok(await page.getAttribute('#viewSeg [data-view="map"]', 'title') === 'Map (m)', `[${tag}] Map tooltip should carry its key`);
			await page.waitForFunction(() => document.querySelectorAll('#mapList .mc').length === 4);
			if (w > 700) {
				await visible('#mapIn .mc', 'map card');
				await settle(); // the view cross-fade swallows pointer events
				ok((await page.locator('#mapIn .mc').count()) === 4 && (await page.locator('#mapSvg .eg').count()) >= 4, `[${tag}] map: 4 cards and its edges`);
				ok((await page.locator('#mProb [data-mprob]').count()) === 1, `[${tag}] map: only problems with a count are listed (example has 1)`);
				ok((await page.locator('#mapSvg .eg.both').count()) === 1, `[${tag}] map: api <-> web-ui is ONE two-headed link`);
				const mid = await page.evaluate(() => { const p = document.querySelector('#mapSvg .eg.both .eh'), q = p.getPointAtLength(p.getTotalLength() / 2), m = p.getScreenCTM(); return [q.x * m.a + m.e, q.y * m.d + m.f]; }); // the curve's own midpoint: its bounding-box centre may be empty
				await page.mouse.move(mid[0], mid[1]);
				await page.waitForFunction(() => { const t = document.getElementById('mapTip').textContent; return t.includes('REST API → Web UI:') && t.includes('Web UI → REST API:'); });
				await page.mouse.move(5, 5);
				await page.click('#mapIn [data-mc="store"]');
				await visible('#drawer #dTitle', 'map panel');
				ok((await page.locator('#drawer #dTitle').innerText()) === 'Ticket store' && (await page.locator('#drawer .mcl li').count()) >= 2, `[${tag}] map panel: title and connections`);
				ok((await page.locator('#mapIn .mc.fade').count()) === 1 && (await page.locator('#mapIn .mc.fade').getAttribute('data-mc')) === 'web-ui', `[${tag}] focus should fade only the non-neighbour`);
				await refresh(); // a poll + refetch must keep the focus
				ok((await page.locator('#mapIn .mc.on').count()) === 1, `[${tag}] focus lost over a poll`);
				await page.keyboard.press('Escape');
				ok((await page.locator('#mapIn .mc.fade').count()) === 0 && await page.locator('#drawer').isHidden(), `[${tag}] Esc should leave the focus and close the panel`);
				await settle(); // the map re-centres when the panel closes
				const m1 = await page.evaluate(() => { const p = document.querySelector('#mapSvg .eg:not(.both) .eh'), q = p.getPointAtLength(p.getTotalLength() / 2), m = p.getScreenCTM(); return [q.x * m.a + m.e, q.y * m.d + m.f]; });
				await page.mouse.move(m1[0], m1[1]); // contract tooltip of a one-way edge
				await page.waitForFunction(() => !document.getElementById('mapTip').hidden && document.getElementById('mapTip').textContent.length > 8);
				await page.click('[data-mprob="cross"]');
				ok((await page.locator('#mapIn .mc.on').count()) === 2, `[${tag}] "tickets span 2+ components" should light api + web-ui`);
				await page.keyboard.press('Escape');
				{ // zoom / pan / drag nodes
					const k = () => page.evaluate(() => +document.getElementById('mapIn').style.transform.match(/scale\(([\d.]+)/)[1]), box = (key) => page.locator(`#mapIn [data-mc="${key}"]`).boundingBox();
					await page.keyboard.press('0'); await settle();
					const k0 = await k(), sb = await page.locator('#mapStage').boundingBox();
					await page.mouse.move(sb.x + sb.width / 2, sb.y + 40); await page.mouse.wheel(0, -200);
					await page.waitForFunction((x) => +document.getElementById('mapIn').style.transform.match(/scale\(([\d.]+)/)[1] > x, k0); ok((await k()) > k0, `[${tag}] map: wheel zooms in`);
					await page.click('[data-mz="fit"]'); await settle();
					ok(Math.abs((await k()) - k0) < 0.01, `[${tag}] map: Fit restores the fitted zoom`);
					let b0 = await box('store'), x0 = b0.x + b0.width / 2, y0 = b0.y + b0.height / 2; // eslint-disable-line
					await page.mouse.move(x0, y0); await page.mouse.down(); await page.mouse.move(x0 + 3, y0); await page.mouse.up();
					await visible('#drawer #dTitle', 'map panel after a 3px wiggle'); // under the threshold it is a click
					await page.keyboard.press('Escape'); await settle();
					b0 = await box('store'); x0 = b0.x + b0.width / 2; y0 = b0.y + b0.height / 2;
					await page.mouse.move(x0, y0); await page.mouse.down(); await page.mouse.move(x0 + 60, y0 + 30, { steps: 5 }); await page.mouse.up();
					const rel = async () => { const a = await box('store'), c = await box('api'), z = await k(); return (a.x - c.x) / z; };
					const r1 = await rel(), b1 = await box('store'); ok(Math.abs(b1.x - b0.x - 60) < 3 && await page.locator('#drawer').isHidden(), `[${tag}] map: dragging a card moves it without opening it`);
					await page.reload(); await page.waitForFunction(() => document.querySelector('#viewSeg [data-view="map"]')?.getAttribute('aria-pressed') === 'true');
					await settle();
					ok(Math.abs((await rel()) - r1) < 2, `[${tag}] map: dragged position survives a reload`);
					ok(await page.evaluate(() => Object.keys(JSON.parse(localStorage.getItem(Object.keys(localStorage).find((x) => x.startsWith('mapPos.'))) || '{}')).includes('store')), `[${tag}] map: dragged position is saved`);
					await page.click('#mReset'); await settle();
					ok(await page.evaluate(() => localStorage.getItem(Object.keys(localStorage).find((x) => x.startsWith('mapPos.'))) === '{}') && await page.locator('#mReset').isDisabled(), `[${tag}] map: Reset layout clears positions`);
				}
				await page.keyboard.press('Escape');
			} else {
				ok((await page.locator('#mapList .mcon li').count()) >= 4, `[${tag}] mobile map: connections listed under each component`);
				ok(await page.locator('#mapStage').isHidden(), `[${tag}] mobile map has no canvas`);
			}
			await noOverflow('map');
			await page.evaluate(() => document.querySelector('[data-open=\"UI-2\"]').click()); await visible('#drawer .ovf', 'ticket drawer');
			await page.click('#drawer .ovf > summary'); await page.click('#drawer [data-onmap]'); await view('map');
			await page.waitForFunction(() => !document.getElementById('mNote').hidden);
			if (w > 700) ok((await page.locator('#mapIn .mc.on').count()) === 2, `[${tag}] Show on map: UI-2 should light both its components`);
			await page.click('#mNote [data-mclear]');
			ok(await page.locator('#mNote').isHidden(), `[${tag}] Clear should leave the blast radius`);
			await page.evaluate(() => document.querySelector('[data-open=\"API-2\"]').click()); await page.click('#drawer .ovf > summary'); await page.click('#drawer [data-onmap]'); await view('map');
			await visible('#drawer #dTitle', 'map panel from a ticket');
			ok((await page.locator('#drawer #dTitle').innerText()) === 'REST API', `[${tag}] Show on map: API-2 should open REST API`);
			await page.evaluate(() => document.querySelector('[data-open=\"DOC-1\"]').click());
			ok((await page.locator('#drawer [data-onmap]').count()) === 0, `[${tag}] DOC-1 maps to no component: no Show on map`);
			await page.click('#drawer [data-close]'); await page.click('[data-view="board"]'); await view('board');
		}

		await toggle('#fpanel', 'Filters panel');
		ok(await page.locator('.card .prog').count(), `[${tag}] parent progress bar missing (DOC-1 was split)`);
		await page.click('#fpanel > summary'); // Group by parent -> swimlane headings; Parent filter -> only its parts
		await page.click('[data-group="parent"]');
		ok(await page.locator('.col .lane').count(), `[${tag}] Group by parent drew no swimlanes`);
		await page.click('[data-group="none"]');
		await page.click('#parentSeg [data-parent]:not([data-parent="ALL"])');
		ok((await page.locator('.card').count()) === 2, `[${tag}] Parent filter should leave the 2 parts`);
		await page.click('#pills [data-parent]');
		await page.click('#fpanel > summary');
		{ // the parent's drawer lists its parts with the progress bar; a part's "Part of" link opens the parent
			await page.evaluate(() => document.querySelector('[data-open="DOC-1"]').click()); await visible('#drawer #dTitle', 'parent drawer');
			const parts = '#drawer h3:has(.prog) + ul.links';
			ok((await page.locator(`${parts} li`).count()) === 2 && await page.locator('#drawer h3 .prog').count(), `[${tag}] parent drawer should list 2 parts and a progress bar`);
			const ptitle = await page.locator('#drawer #dTitle').innerText();
			await page.click(`${parts} li button`); await page.waitForFunction((t) => document.querySelector('#drawer #dTitle')?.textContent !== t, ptitle);
			await page.click('#drawer .dmeta [data-open="DOC-1"]'); await page.waitForFunction((t) => document.querySelector('#drawer #dTitle')?.textContent === t, ptitle);
			await noOverflow('drawer with parts');
			if (process.env.SHOTS) await page.screenshot({ path: `${process.env.SHOTS}/parts-drawer-${w}-${theme}.png` });
			await page.click('#drawer [data-close]');
		}

		{ // Components: filter (explicit OR derived), Group by (two lanes), picker, bulk add, drawer chip -> Map, Map -> board
			await page.waitForFunction(() => !document.getElementById('compSeg').hidden); // /architecture has arrived
			ok((await page.getAttribute('#pillarSeg', 'title')).includes('kind of work') && (await page.getAttribute('#compSeg', 'title')).includes('parts of the system'), `[${tag}] Pillar and Component rows should explain themselves`);
			const ids = () => page.evaluate(() => [...new Set([...document.querySelectorAll('#board .card')].map((c) => c.dataset.open))].sort());
			const all = await ids();
			const fp = async () => { if (!(await page.evaluate(() => document.getElementById('fpanel').open))) await page.click('#fpanel > summary'); }; // a click outside (a pill) closes it
			await fp(); await page.click('#compSeg [data-comp="api"]');
			const api = await ids();
			ok(api.includes('API-2') && api.includes('UI-2') && !api.includes('UI-1') && api.length < all.length, `[${tag}] Component api should match derived tickets (API-2, UI-2) and not UI-1; got ${api}`);
			await page.click('#compSeg [data-comp="web-ui"]');
			ok((await ids()).includes('UI-1') && (await ids()).includes('UI-2'), `[${tag}] Component web-ui: UI-1 (explicit) and UI-2 (derived)`);
			ok((await page.locator('#pills .pill').innerText()).includes('Component: Web UI'), `[${tag}] active Component filter should be a removable pill`);
			await page.click('#pills [data-comp]');
			ok((await ids()).length === all.length, `[${tag}] removing the pill should show everything again`);
			await fp(); await page.click('[data-group="component"]');
			ok((await page.locator('#board .card[data-open="UI-2"]').count()) === 2, `[${tag}] a ticket on 2 components should be in both lanes`);
			ok(/no component/i.test(await page.locator('#board .lane').last().textContent()), `[${tag}] the no-component lane should come last`);
			await noOverflow('group by component');
			await fp(); await page.click('[data-group="none"]');
			await page.keyboard.press('Escape');

			await page.evaluate(() => document.querySelector('[data-open="UI-2"]').click()); // picker in the edit form
			await visible('#drawer [data-mapcomp="api"]', 'derived component chip in the drawer');
			ok(await page.locator('#drawer [data-mapcomp="api"].der').count(), `[${tag}] chips derived from touches should be marked`);
			await page.click('[data-edit]');
			ok((await page.locator('#f-comp .pc').count()) === 0, `[${tag}] UI-2 has no explicit components`);
			await page.fill('#f-comp .pq', 'zzz-nothing'); await visible('#f-comp .pl .none', 'component picker no-match row');
			await page.keyboard.press('Enter');
			ok((await page.locator('#f-comp .pc').count()) === 0, `[${tag}] no free text can become a component`);
			await page.fill('#f-comp .pq', 'REST'); await page.keyboard.press('Enter');
			ok((await page.locator('#f-comp .pc[data-id="api"]').innerText()).startsWith('REST API'), `[${tag}] chip shows the title, value is the key`);
			await noOverflow('component picker');
			await page.fill('#f-skills .pq', 'zzz-nothing'); await visible('#f-skills .pl .none', 'skill picker no-match row'); // closed list: registry names only
			await page.keyboard.press('Enter');
			ok((await page.locator('#f-skills .pc').count()) === 0, `[${tag}] no free text can become a skill`);
			await page.fill('#f-skills .pq', 'policy'); await page.keyboard.press('Enter');
			await page.fill('#f-mcp .pq', 'brow'); await page.keyboard.press('Enter');
			ok((await page.locator('#f-skills .pc').evaluateAll(c => c.map(x => x.dataset.id))).join() === 'agent-policy', `[${tag}] skill chip is the registry name`);
			await page.click('#editForm button[type="submit"]');
			await visible('#toasts .toast:has-text("Saved UI-2")', 'save with components');
			ok(JSON.parse(cli('get', 'UI-2')).components.join() === 'api', `[${tag}] the picker should set components`);
			const u2 = JSON.parse(cli('get', 'UI-2'));
			ok(u2.skills.join() === 'agent-policy' && u2.mcp.join() === 'browser', `[${tag}] the pickers should set skills and mcp`);
			await visible('#drawer .tag:has-text("skill: agent-policy")', 'skill chip in the drawer');
			cli('update', 'UI-2', '--json', '{"skills":[],"mcp":[]}');
			await visible('#drawer [data-mapcomp="api"]:not(.der)', 'explicit chip after save');
			cli('update', 'UI-2', '--json', '{"components":[]}');
			await page.keyboard.press('Escape');

			await page.locator('.card >> visible=true').nth(0).click({ modifiers: ['Shift'] }); // bulk: ADD a component to 2 tickets, one Undo
			await page.locator('.card >> visible=true').nth(1).click({ modifiers: ['Shift'] });
			const sel = await page.evaluate(() => [...document.querySelectorAll('.card.sel')].map((c) => c.dataset.open));
			await noOverflow('bulk bar with Component');
			await page.selectOption('#bulk [data-bulk="components"]', 'store');
			await visible('#toasts .toast:has-text("2 updated")', 'bulk component toast');
			ok(sel.every((id) => JSON.parse(cli('get', id)).components.includes('store')), `[${tag}] bulk should add store to ${sel}`);
			await page.click('#toasts .toast button');
			await visible('#toasts .toast:has-text("Reverted")', 'bulk component undo');
			ok(sel.every((id) => !JSON.parse(cli('get', id)).components.includes('store')), `[${tag}] Undo should take it off again`);
			await page.keyboard.press('Escape');

			await page.evaluate(() => document.querySelector('[data-open="UI-2"]').click()); // chip -> Map focused -> Show N on the board
			await page.click('#drawer [data-mapcomp="api"]');
			await page.waitForFunction(() => document.querySelector('#viewSeg [data-view="map"]')?.getAttribute('aria-pressed') === 'true');
			await page.waitForFunction(() => document.querySelector('#drawer #dTitle')?.textContent === 'REST API');
			const n = await page.evaluate(() => +document.querySelector('#drawer [data-showboard]').textContent.match(/\d+/)[0]);
			await page.click('#drawer [data-showboard="api"]');
			await page.waitForFunction(() => document.querySelector('#viewSeg [data-view="board"]')?.getAttribute('aria-pressed') === 'true');
			await page.waitForFunction(() => document.querySelector('#compSeg [data-comp="api"]')?.getAttribute('aria-pressed') === 'true');
			ok((await ids()).length === n && n >= 2, `[${tag}] Show N tickets on the board: expected ${n} cards, got ${(await ids()).length}`);
			await page.click('#pills [data-comp]');
		}
		await toggle('#spanel', 'Settings panel');
		await toggle('#apanel', 'Agents panel');
		await page.click('#apanel > summary'); await visible('#apanel .fbody', 'Agents panel');
		ok(!/\bother\b/i.test(await page.locator('#apanel .fbody').innerText()), `[${tag}] Agents panel should not have an "other" row`);
		{ // Profiles and tools: off for every worker, back on for one profile; Undo changes drops the draft (nothing saved: the board is shared by later steps)
			await page.click('#apanel [data-aptab="tools"]'); await visible('#apanel .tlbox', 'tools tab');
			const pr = await page.locator('#apanel [data-tp]').first().getAttribute('data-tp');
			await page.click('#apanel [data-tb][data-t="read_doc"]');
			ok((await page.locator(`#apanel [data-tp="${pr}"][data-t="read_doc"]`).getAttribute('aria-pressed')) === 'false', `[${tag}] tools: off for every worker is off for ${pr} too`);
			await page.click(`#apanel [data-tp="${pr}"][data-t="read_doc"]`);
			ok((await page.locator(`#apanel [data-tp="${pr}"][data-t="read_doc"]`).getAttribute('aria-pressed')) === 'true', `[${tag}] tools: ${pr} switches it back on`);
			await noOverflow('tools tab');
			await page.click('#apanel [data-treset]');
			ok((await page.locator('#apanel [data-tb][data-t="read_doc"]').getAttribute('aria-pressed')) === 'true', `[${tag}] tools: Undo changes`);
			await page.click('#apanel [data-aptab="stats"]');
		}
		await page.click('#apanel > summary');

		await page.locator('.card >> visible=true').nth(0).click({ modifiers: ['Shift'] }); // bulk edit: select 2, set priority, Undo
		await page.locator('.card >> visible=true').nth(1).click({ modifiers: ['Shift'] });
		await visible('#bulk', 'bulk bar');
		ok((await page.locator('#bulk b').innerText()).startsWith('2 selected'), `[${tag}] bulk bar should say 2 selected`);
		await noOverflow('bulk bar');
		await page.selectOption('#bulk [data-bulk="priority"]', 'P3');
		await visible('#toasts .toast:has-text("2 updated")', 'bulk update toast');
		await page.click('#toasts .toast button');
		await visible('#toasts .toast:has-text("Reverted")', 'bulk undo toast');
		await page.click('#bulkParent .pq'); // Parent picker: lists tickets, no free text
		await visible('#bulkParent .pl [data-pk]', 'bulk parent picker list');
		await noOverflow('bulk parent picker');
		await page.keyboard.press('Escape'); // closes the list only
		ok(await page.locator('#bulk').isVisible(), `[${tag}] Escape in the picker should only close its list`);
		await page.keyboard.press('Escape');
		ok(await page.locator('#bulk').isHidden(), `[${tag}] Escape should clear the selection`);
		await page.click('.card >> visible=true');
		await visible('#drawer', 'ticket drawer');
		await noOverflow('drawer');
		const pcur = await page.locator('#drawer .dhead .seal').innerText().catch(() => ''), pkey = pcur === 'P0' ? '1' : '0'; // p, then a digit: priority of the open ticket, with Undo
		await page.keyboard.press('p'); await visible('#toasts .toast:has-text("press 0 1 2 3")', 'priority hint');
		await page.keyboard.press(pkey);
		await page.waitForFunction((v) => document.querySelector('#drawer .dhead .seal')?.textContent === v, 'P' + pkey);
		await page.click('#toasts .toast:has(button) button');
		await page.waitForFunction((v) => (document.querySelector('#drawer .dhead .seal')?.textContent || '') === v, pcur);
		await page.click('[data-edit]'); // Blocked by picker: search, pick, chip appears, remove it
		const nChips = await page.locator('#f-blocked .pc').count();
		await page.fill('#f-blocked .pq', 'smoke');
		await page.keyboard.press('Enter');
		ok((await page.locator('#f-blocked .pc').count()) === nChips + 1, `[${tag}] picking a blocker should add a chip`);
		ok((await page.locator('#f-blocked .pc[data-id="SM-1"]').innerText()).startsWith('SM-1'), `[${tag}] picked chip should be SM-1, labelled with its id`);
		await page.fill('#f-blocked .pq', 'zzz-nothing');
		await visible('#f-blocked .pl .none', 'picker no-match row');
		await noOverflow('edit form');
		await page.click('#f-blocked .pc[data-id="SM-1"] [data-pkrm]');
		ok((await page.locator('#f-blocked .pc').count()) === nChips, `[${tag}] removing the chip should drop it`);
		await page.keyboard.press('Escape'); // list already closed by the click: back to the ticket
		await visible('[data-setstatus="done"]', 'drawer back in view mode');
		await page.click('[data-setstatus="done"]'); // status change through the UI -> toast
		await visible('#toasts .toast', 'status-change toast');
		await page.click('#toasts .toast button'); // Undo, so the next run sees the same board
		await visible('#toasts .toast:has-text("back in")', 'undo confirmation toast');
		await page.keyboard.press('Escape');

		// Needs you (header badge + side panel, this board only): a review + an agent question here; the question on the other board shows only there
		const rid = JSON.parse(cli('create', '--prefix', 'NY', '--title', 'review me', '--json', '{"owner":"Claude","done_when":["it works"]}')).id;
		cli('claim', '--agent', 'backend-1', '--id', rid); // result is only written by complete(); the example board has no review gate, so move it to review by hand
		cli('complete', rid, '--agent', 'backend-1', '--summary', 'Did the **thing**', '--file', 'a.py', '--ref', 'https://example.com/pull/1', '--handoff', 'nothing');
		cli('update', rid, '--json', '{"status":"review"}');
		const qid = JSON.parse(cli('create', '--prefix', 'NY', '--title', 'question me', '--json', '{"owner":"Claude","priority":"P1"}')).id;
		cli('create', '--prefix', 'NY', '--title', 'after the question', '--json', `{"owner":"Claude","blocked_by":["${qid}"]}`);
		cli('claim', '--agent', 'backend-1', '--id', qid);
		cli('ask', qid, '--agent', 'backend-1', '--question', 'Which one?'); // an agent question: in the panel
		cli('create', '--prefix', 'NY', '--title', 'parked by hand', '--json', '{"owner":"Claude","waiting_on":"Omri: later"}'); // hand-parked: not in the panel
		const badge = () => page.locator('#needsBadge .n').innerText();
		// the CLI wrote the files: refresh as the 4 s poll would, then wait for the state
		await refresh();
		await page.waitForFunction(() => document.querySelector('#needsBadge .n')?.textContent === '2'); // this board only: the second board's question is not counted here
		ok(/needs? you/.test(await page.locator('#needsBadge').textContent()), `[${tag}] Needs badge is in words`);
		{ const wl = await page.locator('[data-col=pending] .lane').allInnerTexts(); // the agent question and the hand-parked ticket land in their own Waiting sections
			ok(['on you', 'on other work', 'parked'].every((x) => wl.some((y) => y.toLowerCase().startsWith(x))), `[${tag}] Waiting sections On you / On other work / Parked: ${wl}`); }
		ok((await page.getAttribute('#needsBadge', 'title')) === 'Needs you (i)', `[${tag}] badge tooltip`);
		for (const v of ['graph', 'docs', 'board']) { // visible in every view; Free/Blocked only in the Graph
			await page.click(`[data-view="${v}"]`);
			await page.waitForFunction((x) => document.querySelector(`#viewSeg [data-view="${x}"]`)?.getAttribute('aria-pressed') === 'true' && !document.getElementById('needsBadge').hidden && (x === 'graph') === !document.getElementById('depSeg').hidden, v);
		}
		{ // Graph's Free/Blocked must not leak into the Board
			await page.waitForFunction((x) => x.every((id) => document.querySelector(`#board .card[data-open="${id}"]`)), [rid, qid]); // the 4 s poll has brought this board's new tickets (the badge can come from the 15 s timer)
			const n0 = await page.locator('#board .card').count();
			await page.click('[data-view="graph"]'); await page.waitForFunction(() => document.querySelector('#viewSeg [data-view="graph"]')?.getAttribute('aria-pressed') === 'true');
			await page.click('#depSeg [data-dep="BLOCKED"]');
			await page.click('[data-view="board"]'); await page.waitForFunction(() => document.querySelector('#viewSeg [data-view="board"]')?.getAttribute('aria-pressed') === 'true');
			await settle();
			{ const n1 = await page.locator("#board .card").count(); ok(n1 === n0, `[${tag}] Blocked chosen in the Graph should not filter the Board (${n0} -> ${n1})`); }
			await page.click('[data-view="graph"]'); await page.click('#depSeg [data-dep="ALL"]'); await page.click('[data-view="board"]');
		}
		await page.click('#needsBadge'); // open via the badge, close with Esc, open again with the key
		await visible('#npanel .nr', 'Needs you panel');
		await page.keyboard.press('Escape');
		ok(await page.locator('#npanel').isHidden(), `[${tag}] Escape should close the panel`);
		await page.keyboard.press('i');
		await visible('#npanel .nr', 'Needs you panel via the i key');
		ok((await page.locator('#npList .nr').count()) === 2, `[${tag}] panel should list this board's 2 items only`);
		ok((await page.locator('#npList .ng').count()) === 0 && (await page.locator(`#npList .nr[data-key="zsecond/${oid}"]`).count()) === 0, `[${tag}] only this board's items, no board groups`);
		ok((await page.locator('#npList .nr').first().locator('.nb').innerText()) === 'Question', `[${tag}] higher score (P1, blocks a ticket) should sort first`);
		ok(!(await page.locator('#npanel').innerText()).includes('parked by hand'), `[${tag}] hand-parked ticket must not be in the panel`);
		ok((await page.locator(".card:has-text(\"parked by hand\") .why.park").first().innerText()) === 'Parked: Omri: later', `[${tag}] hand-parked card should say Parked`);
		ok((await page.locator(`.card:has-text("question me") .why`).first().innerText()) === 'Question from backend-1', `[${tag}] agent question card should say Question from <agent>, not the first word`);
		await noOverflow('needs panel');
		{ // a long collapsed question shows up to 3 lines
			const q = page.locator('#npList .nr:not(.open) .nd-q').first();
			const [h1, h] = await q.evaluate((e) => { const lh = parseFloat(getComputedStyle(e).lineHeight); e.textContent = 'word '.repeat(400); return [lh, e.getBoundingClientRect().height]; });
			ok(h > h1 * 1.5 && h <= h1 * 3.2, `[${tag}] collapsed question should clamp to 3 lines (line ${h1}, height ${h})`);
		}
		// expand the question: full text, a multi-line answer box; Enter = newline, Ctrl+Enter sends
		const boardId = 'example', qrow = `#npList .nr[data-key="${boardId}/${qid}"]`;
		await page.click(`${qrow} .nttl`);
		await visible(`${qrow}.open textarea`, 'expanded answer box');
		await noOverflow('needs panel expanded');
		const h1 = await page.locator(`${qrow} textarea`).evaluate((e) => e.offsetHeight);
		await page.fill(`${qrow} textarea`, 'line one'); await page.keyboard.press('Enter'); await page.keyboard.type('line two'); await page.keyboard.press('Enter'); await page.keyboard.type('line three'); await page.keyboard.press('Enter'); await page.keyboard.type('line four');
		ok((await page.inputValue(`${qrow} textarea`)) === 'line one\nline two\nline three\nline four', `[${tag}] Enter should add a newline`);
		ok((await page.locator(`${qrow} textarea`).evaluate((e) => e.offsetHeight)) > h1, `[${tag}] the answer box should grow with its text`);
		if (process.env.SHOTS) await page.screenshot({ path: `${process.env.SHOTS}/needs-${w}-${theme}.png` });
		// collapse and expand again: the draft (with its newlines) is kept
		await page.click(`${qrow} .nttl`); await page.click(`${qrow} .nttl`);
		ok((await page.inputValue(`${qrow} textarea`)) === 'line one\nline two\nline three\nline four', `[${tag}] draft should survive collapse and expand`);
		await page.keyboard.press('Control+Enter'); // not the Enter key alone: that was a newline
		await page.waitForFunction((k) => !document.querySelector(`#npList .nr[data-key="${k}"]`), `${boardId}/${qid}`);
		ok((await badge()) === '1', `[${tag}] badge should drop to 1 after answering`);
		const answered = await page.evaluate((i) => fetch('/api/tickets/' + i).then((r) => r.json()), qid);
		ok(answered.notes.some((n) => n.kind === 'answer' && n.q === 'Which one?' && n.text === 'line one\nline two\nline three\nline four') && !answered.waiting_on, `[${tag}] the multi-line answer should be on the ticket: ${answered.body}`);
		// the review: open the ticket from the panel (the panel gives way to the drawer)
		await page.click(`#npList .nr:has(.nb.r) .nid`);
		await visible('#drawer .rbox a[href^="https://example.com"]', 'review result with link');
		ok((await page.locator('#drawer .rbox h5').allInnerTexts()).join('|') === 'Files changed|References|Handoff', `[${tag}] result box labels`);
		ok((await page.locator('#drawer .rbox a').first().evaluate((e) => getComputedStyle(e).color)) !== 'rgb(0, 0, 238)', `[${tag}] result link should use the app link colour`);
		ok(await page.locator('#npanel').isHidden(), `[${tag}] opening a ticket should close the panel`);
		ok(await page.locator('#drawer details.kn[open]').count(), `[${tag}] review drawer should open Knowledge and activity`);
		await noOverflow('review drawer');
		if (process.env.SHOTS) await page.screenshot({ path: `${process.env.SHOTS}/ui6-drawer-${w}-${theme}.png` });
		await page.keyboard.press('Escape');
		await page.keyboard.press('i');
		// expanded review: result + done_when + notes; a half-typed reject reason survives a data change (a note arrives from outside)
		const rrow = `#npList .nr[data-key="${boardId}/${rid}"]`;
		await page.click(`${rrow} .nttl`);
		await visible(`${rrow} .rbox a[href^="https://example.com"]`, 'review result inside the expanded row');
		ok((await page.locator(`${rrow} .dw li`).first().innerText()) === 'it works', `[${tag}] done_when in the expanded row`);
		ok(/^Request changes/.test(await page.locator(`${rrow} .rqd > summary`).innerText()) && !(await page.locator(`${rrow} [data-approve]`).evaluate((e) => e.classList.contains('ghost'))), `[${tag}] review row: Approve is primary, sending back is a quiet "Request changes…"`);
		await page.click(`${rrow} .rqd > summary`);
		await page.fill(`${rrow} textarea`, 'half typed');
		cli('note', rid, 'checked the diff', '--by', 'omri');
		await refresh(); // as the 4 s poll would
		await page.waitForFunction((r) => document.querySelector(`${r} .nn`)?.textContent.includes('checked the diff'), rrow); // the 4 s poll brought it
		ok((await page.inputValue(`${rrow} textarea`)) === 'half typed', `[${tag}] draft lost when the data changed`);
		await page.fill(`${rrow} textarea`, '');
		await page.click(`${rrow} [data-approve]`);
		await page.waitForFunction((k) => !document.querySelector(`#npList .nr[data-key="${k}"]`), `${boardId}/${rid}`);
		ok(await page.locator('.col.done .card:has-text("review me")').count(), `[${tag}] approved ticket should be in Done`);
		{ // a done card is one compact row: id + title + age, no seal / owner chip, still opens the drawer, still has its Move-to menu
			const d = page.locator('.col.done .card.dn').first(), td = page.locator('.col.todo .card').first();
			ok(await d.getAttribute('data-open') && await d.getAttribute('title'), `[${tag}] done card keeps data-open and the full title`);
			ok((await d.locator('.seal, .own').count()) === 0, `[${tag}] done card should show no priority seal or owner chip`);
			ok((await d.locator('xpath=..').locator('[data-moveto]').count()) > 0, `[${tag}] done card keeps its Move-to menu`);
			const hd = await d.boundingBox(), ht = await td.boundingBox();
			if (hd && ht) ok(hd.height < ht.height / 2, `[${tag}] done card (${hd.height}px) should be under half a To do card (${ht.height}px)`);
		}
		ok((await page.evaluate((i) => fetch('/api/tickets/' + i).then((r) => r.json()), rid)).result.approved_by === 'human', `[${tag}] Approve should record the approver (human when I am is not set)`);
		{ // Approve / Reject from the drawer (same handlers, same draft key as the panel); own review tickets, so the panel checks around it stay as they are
			const mk = (title) => { const id = JSON.parse(cli('create', '--prefix', 'NY', '--title', title, '--json', '{"owner":"Claude"}')).id; cli('claim', '--agent', 'backend-1', '--id', id); cli('complete', id, '--agent', 'backend-1', '--summary', 'done it'); cli('update', id, '--json', '{"status":"review"}'); return id; };
			const st = async (id) => (await page.evaluate((i) => fetch('/api/tickets/' + i).then((r) => r.json()), id));
			const open = async (id) => { await page.keyboard.press('Escape'); await page.waitForFunction((i) => document.querySelector(`.col.review [data-open="${i}"]`), id); await page.evaluate((i) => document.querySelector(`[data-open="${i}"]`).click(), id); await visible('#drawer [data-nform=reject]', 'drawer reject form'); };
			const aid = mk('approve me'), xid = mk('reject me');
			await open(aid); await noOverflow('review drawer with decide');
			await page.click('#drawer [data-approve]'); await page.waitForFunction(() => /Approved/.test(document.querySelector('#toasts')?.textContent || ''));
			ok(await page.waitForSelector('#toasts button:has-text("Undo")', { timeout: 3000 }).then(() => true, () => false) && (await st(aid)).status === 'done', `[${tag}] Approve from the drawer: done, with Undo`);
			await open(xid);
			await page.click('#drawer [data-nform=reject] .rqd > summary'); await page.fill('#drawer [data-nform=reject] textarea', 'needs tests'); await page.click('#drawer [data-nform=reject] button[type=submit]');
			for (let n = 0; n < 50 && (await st(xid)).status !== 'todo'; n++) await page.waitForTimeout(100);
			ok((await st(xid)).notes.some((n) => n.kind === 'rejected' && n.text === 'needs tests'), `[${tag}] Reject from the drawer: back to To do with the reason in the body`);
			cli('delete', aid); cli('delete', xid);
			await page.keyboard.press('Escape'); await page.keyboard.press('i'); await visible('#npanel .none:not([hidden])', 'panel back, empty (this board is done)');
		}
		await page.waitForFunction(() => document.getElementById('needsBadge').hidden); // this board is clear
		await page.waitForSelector('#npanel .none:not([hidden])'); // the panel stays open and says so
		// the other board's question shows on that board
		await page.keyboard.press('Escape');
		await page.goto(base + '?board=zsecond'); await page.waitForSelector('.card', { state: 'attached' });
		await page.waitForFunction(() => document.querySelector('#needsBadge .n')?.textContent === '1');
		await page.keyboard.press('i');
		const orow = `#npList .nr[data-key="zsecond/${oid}"]`;
		await page.fill(`${orow} input[data-draft]`, 'Teal'); await page.keyboard.press('Enter');
		await page.waitForFunction(() => document.getElementById('needsBadge').hidden);
		const oans = await page.evaluate((i) => fetch('/api/b/zsecond/tickets/' + i).then((r) => r.json()), oid);
		ok(oans.notes.some((n) => n.kind === 'answer' && n.q === 'Which colour?' && n.text === 'Teal'), `[${tag}] the other board's ticket should carry the answer`);
		await page.keyboard.press('Escape');
		await page.goto(base + '?board=' + boardId); await page.waitForSelector('.card', { state: 'attached' }); // explicit: the page reopens the last board (zsecond) otherwise
		await page.keyboard.press('i'); await page.waitForSelector('#npanel .none:not([hidden])');
		ok((await page.locator('#attnSeg').count()) === 0, `[${tag}] the Needs attention chip is gone (Stuck lives in the panel)`);
		// Stuck: a doing ticket nobody touched for stale_days, and a todo whose blocker file is gone (both crafted in the JSON); the open panel picks them up
		const age = async (id, change) => { const f = join(root, 'example/tickets', `${id}.json`); await writeFile(f, JSON.stringify({ ...JSON.parse(await readFile(f, 'utf8')), ...change })); };
		const sid = JSON.parse(cli('create', '--prefix', 'ST', '--title', 'gone quiet', '--json', '{"owner":"Claude"}')).id;
		cli('claim', '--agent', 'backend-1', '--id', sid); await age(sid, { updated: '2020-01-01' });
		const mid = JSON.parse(cli('create', '--prefix', 'ST', '--title', 'lost its blocker', '--json', '{"owner":"Claude"}')).id;
		await age(mid, { blocked_by: ['GONE-1'] });
		const bk = JSON.parse(cli('create', '--prefix', 'ST', '--title', 'the blocker', '--json', '{"owner":"Claude"}')).id;
		const did = JSON.parse(cli('create', '--prefix', 'ST', '--title', 'doing while blocked', '--json', '{"owner":"Claude"}')).id;
		cli('claim', '--agent', 'backend-1', '--id', did); await age(did, { blocked_by: [bk] }); // claimed first (claim refuses a blocked ticket), blocker added in the JSON
		await refresh(); // as the 4 s poll would
		await page.waitForFunction(() => document.querySelectorAll('#npList .nb.s').length === 3);
		ok((await page.locator('#npSum').innerText()) === '3 stuck', `[${tag}] header counts: ${await page.locator('#npSum').innerText()}`);
		const srow = `#npList .nr[data-key="${boardId}/${sid}"]`, mrow = `#npList .nr[data-key="${boardId}/${mid}"]`;
		const drow = `#npList .nr[data-key="${boardId}/${did}"]`;
		ok((await page.locator(`${drow} .nd-q`).innerText()).startsWith(`In progress but blocked by ${bk} (To do)`) && (await page.locator(`${drow} [data-release]`).count()) === 1, `[${tag}] doing+blocked row names the blocker: ${await page.locator(`${drow} .nd-q`).innerText()}`);
		if (process.env.SHOTS) await page.screenshot({ path: `${process.env.SHOTS}/stuck-${w}-${theme}.png` });
		ok((await page.locator(`${mrow} .nd-q`).innerText()) === 'Points at GONE-1, which was deleted. Open it to remove or replace the blocker.', `[${tag}] missing-blocker hint`);
		ok((await page.locator(`${mrow} [data-release]`).count()) === 0 && (await page.locator(`${mrow} [data-open]`).count()) === 2, `[${tag}] a missing blocker only gets Open (id + button)`);
		ok((await page.locator(`${srow} .nd-q`).innerText()).startsWith('In progress for '), `[${tag}] stale hint`);
		await noOverflow('stuck rows');
		// failed-N-times cue on the card: 3 claim+release rounds, gone after a human note
		const fid = JSON.parse(cli('create', '--prefix', 'ST', '--title', 'keeps failing', '--json', '{"owner":"Claude"}')).id;
		for (let i = 0; i < 3; i++) { cli('claim', '--agent', 'backend-1', '--id', fid); cli('release', fid, '--agent', 'backend-1', '--reason', 'tried ' + i); }
		await refresh();
		await page.waitForFunction((i) => /Failed 3 times/.test(document.querySelector(`#board [data-open="${i}"] .wl.fail`)?.textContent || ''), fid);
		cli('note', fid, 'try the other approach', '--by', 'human');
		await refresh();
		await page.waitForFunction((i) => !document.querySelector(`#board [data-open="${i}"] .wl.fail`), fid);
		ok(true, `[${tag}] failed-3-times cue shows on the card and clears after a human note`);
		await page.click(`${srow} [data-release]`);
		await page.waitForFunction((k) => !document.querySelector(`#npList .nr[data-key="${k}"]`), `${boardId}/${sid}`);
		const rel = await page.evaluate((i) => fetch('/api/tickets/' + i).then((r) => r.json()), sid);
		ok(rel.status === 'todo' && !rel.claim, `[${tag}] Release to To do should drop the claim: ${rel.status}`);
		cli('delete', did); cli('delete', bk);
		cli('delete', mid); // the other one goes when its ticket does
		await refresh();
		await page.waitForFunction(() => document.getElementById('needsBadge').hidden);
		await page.keyboard.press('Escape');
		await page.evaluate(() => scrollTo(0, 0));
		await page.screenshot({ path: process.env.SHOTS ? `${process.env.SHOTS}/ui6-${w}-${theme}.png` : tmpdir() + '/ui6.png' });
		await noOverflow('board after needs');

		// proposal gate + schema (second board): an agent's ticket parks in Needs you, Accept clears it; the New ticket form lists prefixes with meanings
		const pid = JSON.parse(cliB('create', '--prefix', 'ZP', '--title', 'agent idea', '--agent', 'backend-1', '--json', '{"priority":"P2","done_when":["it works"],"body":"## Why\\nbecause"}')).id;
		await page.goto(base + '?board=zsecond'); await page.waitForSelector('.card', { state: 'attached' }); // a proposal shows on its own board
		await page.waitForFunction(() => document.querySelector('#needsBadge .n')?.textContent === '1');
		await page.keyboard.press('i');
		const prow = `#npList .nr[data-key="zsecond/${pid}"]`;
		await visible(prow, 'proposal row');
		const ptxt = await page.locator(prow).innerText();
		ok(ptxt.includes('Proposed by backend-1') && ptxt.includes('it works'), `[${tag}] proposal row should name the agent and show done_when: ${ptxt}`);
		await page.click(`${prow} [data-accept]`);
		await page.waitForFunction(() => document.getElementById('needsBadge').hidden);
		ok((await page.evaluate((i) => fetch('/api/b/zsecond/tickets/' + i).then((r) => r.json()), pid)).proposed_by === '', `[${tag}] Accept should clear proposed_by`);
		await page.keyboard.press('Escape');
		await page.goto(base + '?board=zsecond'); await page.waitForSelector('.card', { state: 'attached' });
		await page.keyboard.press('n');
		await visible('select#f-prefix', 'prefix select on a schema board');
		ok((await page.locator('#f-prefix option').allInnerTexts()).includes('ZP — proposed work'), `[${tag}] prefix options should show their meaning`);
		ok((await page.locator('#editForm .rq-l.empty').count()) >= 1, `[${tag}] empty required fields should be highlighted`);
		// form pickers: suggest existing docs/tags, but a typed value (a glob, a path that does not exist yet) is kept too
		await page.fill('#f-title', 'picker ticket');
		await page.fill('#f-docs .pq', 'guide'); await visible('#f-docs .pl li[data-pk="docs/guide.md"]', 'docs suggestion');
		await page.keyboard.press('ArrowDown'); await page.keyboard.press('Enter'); // picks the suggestion
		await page.fill('#f-docs .pq', 'docs/new/**/*.md'); await page.keyboard.press('Enter'); // typed, not in the list
		await page.fill('#f-touches .pq', 'src/not-yet.py,'); // a comma commits
		const longP = 'src/' + 'a'.repeat(120) + '.py'; // a long path chip wraps inside the picker, no overflow
		await page.fill('#f-touches .pq', longP); await page.keyboard.press('Enter');
		await noOverflow('long path chip');
		ok(await page.evaluate(() => { const c = document.querySelector('#f-touches .pc:last-child'), p = document.getElementById('f-touches'); return c.getBoundingClientRect().right <= p.getBoundingClientRect().right + 1; }), `[${tag}] long chip should stay inside the picker`);
		await page.click('#f-touches .pc:last-child [data-pkrm]');
		await page.fill('#f-tags .pq', 'chore'); await page.keyboard.press('Enter');
		await page.fill('#f-tags .pq', 'Fresh Tag'); await page.keyboard.press('Enter');
		await page.fill('#f-docs .pq', 'AGENT_POLICY.md'); // left typed: blur must keep it
		await page.fill('#f-done', 'works');
		ok((await page.locator('#f-docs .pc').evaluateAll(c => c.map(x => x.dataset.id))).join() === 'docs/guide.md,docs/new/**/*.md,AGENT_POLICY.md', `[${tag}] docs chips (blur keeps typed text)`);
		ok((await page.locator('#f-tags .pc').evaluateAll(c => c.map(x => x.dataset.id))).join() === 'chore,fresh,tag', `[${tag}] tags chips`);
		await noOverflow('new ticket form on a schema board');
		await page.selectOption('#f-priority', 'P2'); await page.selectOption('#f-prefix', 'ZQ');
		await page.click('#editForm button[type=submit]');
		await page.waitForFunction(() => document.querySelector('#drawer h2')?.textContent !== 'New ticket', null, { timeout: 5000 });
		const made = await page.evaluate(() => fetch('/api/b/zsecond/tickets').then((r) => r.json()).then((a) => a.find((t) => t.title === 'picker ticket')));
		ok(made && made.docs.join() === 'docs/guide.md,docs/new/**/*.md,AGENT_POLICY.md' && made.touches.join() === 'src/not-yet.py' && made.tags.join() === 'chore,fresh,tag', `[${tag}] saved lists: ${JSON.stringify(made)}`);
		await page.keyboard.press('Escape');

		ok(!errs.length, `[${tag}] browser errors:\n  ` + errs.join('\n  '));
		await ctx.close();
	}
	// 4b. the view lives in the URL: a link restores it, a change rewrites it, junk is ignored
	{
		const ok0 = (c, msg) => { if (!c) throw new Error(msg); };
		const W = async (page, f, what) => page.waitForFunction(f).catch(async () => { throw new Error('url: ' + what + ' (search=' + await page.evaluate(() => location.search) + ')'); });
		const ctx = await browser.newContext({ viewport: { width: 1400, height: 900 } });
		const page = await ctx.newPage(); page.setDefaultTimeout(5000);
		const errs = []; page.on('pageerror', (e) => errs.push(e.message)); page.on('console', (m) => m.type() === 'error' && errs.push(m.text()));
		await page.goto(base + '?q=a&priority=P0,P9&view=graph&group=nope&pillar=zzz');
		await W(page, () => !document.getElementById('graphWrap').hidden, 'wait');
		ok0(await page.inputValue('#q') === 'a', 'url: q not restored');
		ok0(await page.locator('#prioSeg [aria-pressed=true]').allTextContents().then((a) => a.join() === 'P0'), 'url: priority not restored / junk P9 kept');
		ok0(!(await page.evaluate(() => location.search)).includes('nope'), 'url: junk value survived the rewrite');
		await page.click('#depSeg [data-dep=FREE]');
		await W(page, () => location.search.includes('dep=FREE'), 'wait');
		await page.click('#viewSeg [data-view=board]');
		await W(page, () => !location.search.includes('view=') && !location.search.includes('dep='), 'wait');
		await page.fill('#q', '');
		await page.click('#fpanel > summary');
		await page.click('#prioSeg [data-prio=P0]');
		await W(page, () => location.search === '', 'wait');
		ok0(!errs.length, 'url: browser errors ' + errs.join('; '));
		await ctx.close();
	}
	// 4c. phone chrome: Next up starts collapsed at every width (a stored choice wins), touch targets >= 44px, desktop sizes unchanged
	{
		const ok0 = (c, msg) => { if (!c) throw new Error('mobile: ' + msg); };
		const heights = (page) => page.evaluate(() => ['.btn', '.chip', '#bsum', '.vico', '.tab'].flatMap((s) => [...document.querySelectorAll(s)].filter((e) => e.offsetParent).map((e) => { const r = e.getBoundingClientRect(); return [s, Math.round(r.width), Math.round(r.height)]; })));
		const D = await browser.newContext({ viewport: { width: 1400, height: 900 } }), dp = await D.newPage(); dp.setDefaultTimeout(5000);
		await dp.goto(base); await dp.waitForSelector('#nextUp:not([hidden])');
		ok0(await dp.evaluate(() => document.getElementById('nextUp').classList.contains('m-c')), 'desktop Next up should also start collapsed with empty storage');
		const dh = await heights(dp); ok0(dh.some(([s, , h]) => s === '.btn' && h < 44), 'desktop .btn grew to 44px');
		await D.close();
		const ctx = await browser.newContext({ viewport: { width: 390, height: 844 }, hasTouch: true, isMobile: true }), page = await ctx.newPage(); page.setDefaultTimeout(5000);
		const errs = []; page.on('pageerror', (e) => errs.push(e.message)); page.on('console', (m) => m.type() === 'error' && errs.push(m.text()));
		await page.goto(base); await page.waitForSelector('#nextUp:not([hidden])');
		ok0(await page.evaluate(() => document.getElementById('nextUp').classList.contains('m-c')), 'Next up should start collapsed at 390 with empty storage');
		await page.click('#nextUp .nexttog'); // a stored choice wins on reload
		await page.reload(); await page.waitForSelector('#nextUp:not([hidden])');
		ok0(await page.evaluate(() => !document.getElementById('nextUp').classList.contains('m-c')), 'stored choice (top) should beat the collapsed default');
		const small = (await heights(page)).filter(([, w, h]) => h < 44 || w < 44);
		ok0(!small.length, 'targets under 44px on touch: ' + JSON.stringify(small.slice(0, 5)));
		ok0(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth), 'horizontal overflow on touch');
		{ const top = (pg) => pg.evaluate(() => Math.round(document.querySelector('#board .card').getBoundingClientRect().top + scrollY)), M = await browser.newContext({ viewport: { width: 390, height: 844 } }), mp = await M.newPage(); // first card: <= 483px with a mouse, <= 523px on touch (AGENTS.md "Phone chrome")
			await mp.goto(base); await mp.waitForSelector('#board .card'); const mt = await top(mp); await M.close();
			ok0(mt <= 483, `first card at 390 with a mouse starts ${mt}px down (max 483)`);
			const T2 = await browser.newContext({ viewport: { width: 390, height: 844 }, hasTouch: true, isMobile: true }), tp = await T2.newPage(); await tp.goto(base); await tp.waitForSelector('#board .card'); const ct = await top(tp); await T2.close(); // fresh storage: this page above stored Next up open
			ok0(ct <= 523, `first card at 390 on touch starts ${ct}px down (max 523)`); }
		ok0(!errs.length, errs.join('; '));
		await ctx.close();
	}
	// 5. motion: a ticket moved behind the page's back slides (WAAPI) and ends in its new column with no leftover transform; under reduced motion nothing animates and everything still works
	for (const reducedMotion of ['no-preference', 'reduce']) {
		const ctx = await browser.newContext({ viewport: { width: 1400, height: 900 }, reducedMotion });
		const page = await ctx.newPage(); page.setDefaultTimeout(8000);
		const errs = []; page.on('pageerror', (e) => errs.push(e.message));
		await page.addInitScript(() => { window.__anim = 0; const o = Element.prototype.animate; Element.prototype.animate = function (...a) { window.__anim++; return o.apply(this, a); }; });
		await page.goto(base); await page.waitForSelector('.card', { state: 'attached' });
		const created = JSON.parse(cli('create', '--prefix', 'MV', '--title', 'mover', '--json', '{"owner":"Claude","pillar":"API"}').toString());
		const id = created.id; await page.waitForSelector(`.col.todo .card[data-open="${id}"]`);
		await page.evaluate(() => { window.__anim = 0; });
		cli('update', id, '--json', '{"status":"doing"}');
		await page.waitForSelector(`.col.doing .card[data-open="${id}"]`);
		await page.waitForTimeout(500);
		const end = await page.evaluate((i) => { const w = document.querySelector(`.card[data-open="${i}"]`).closest('.cw'); return { tf: getComputedStyle(w).transform, running: w.getAnimations().length, anim: window.__anim }; }, id);
		ok(end.tf === 'none' && end.running === 0, `[motion/${reducedMotion}] moved card should rest in its column without a transform: ${JSON.stringify(end)}`);
		ok(reducedMotion === 'reduce' ? end.anim === 0 : end.anim > 0, `[motion/${reducedMotion}] expected ${reducedMotion === 'reduce' ? 'no' : 'some'} animation, saw ${end.anim}`);
		await page.click('[data-view="graph"]'); await page.waitForSelector('#graph', { state: 'visible' });
		const ind = await page.evaluate(() => { const b = document.querySelector('#viewSeg [aria-pressed=true]'), i = document.querySelector('#viewSeg .ind'), r = b.getBoundingClientRect(), q = i.getBoundingClientRect(); return b.dataset.view === 'graph' && Math.abs(r.left - q.left) < 2 && Math.abs(r.width - q.width) < 2; });
		ok(ind, `[motion/${reducedMotion}] view indicator should sit behind the pressed view`);
		await page.keyboard.press('g'); await page.waitForSelector('.card', { state: 'visible' });
		ok(!errs.length, `[motion/${reducedMotion}] page errors: ${errs.join('; ')}`);
		await ctx.close();
	}
	// 6. chat proposals that reference each other: "#1" waits until card 1 is created, then resolves to its id
	{
		const ctx = await browser.newContext({ viewport: { width: 1400, height: 900 } });
		const page = await ctx.newPage(); page.setDefaultTimeout(8000);
		const errs = []; page.on('pageerror', (e) => errs.push(e.message));
		await page.route('**/api/chat', (r) => r.request().method() === 'POST'
			? r.fulfill({ json: { reply: 'plan', tools: [], refs: [], proposals: [{ title: 'Alpha' }, { title: 'Beta', blocked_by: ['#1'] }] } })
			: r.fulfill({ json: { enabled: true, model: 'x' } }));
		await page.goto(base); await page.waitForSelector('.card', { state: 'attached' });
		await page.click('#chatBtn'); await page.fill('#cin', 'plan it'); await page.keyboard.press('Enter');
		await page.waitForSelector('[data-prop="1:1"][data-act="create"]');
		ok(await page.locator('[data-prop="1:1"][data-act="create"]').isDisabled(), '[chat] card 2 should wait for card 1');
		await page.click('[data-prop="1:0"][data-act="create"]'); await page.waitForSelector('[data-prop="1:1"][data-act="create"]:not([disabled])');
		await page.click('[data-prop="1:1"][data-act="create"]'); await page.waitForFunction(() => document.querySelectorAll('.prop .pm').length && [...document.querySelectorAll('.prop')].every((e) => /Created/.test(e.textContent)));
		const ids = await page.evaluate(() => [...document.querySelectorAll('.prop')].map((e) => e.textContent.match(/Created (\S+)/)[1])), b = await (await fetch(base + 'api/tickets/' + ids[1])).json();
		ok(JSON.stringify(b.blocked_by) === JSON.stringify([ids[0]]), `[chat] Beta should be blocked_by ${ids[0]}: ${JSON.stringify(b.blocked_by)}`);
		ok(!errs.length, `[chat] page errors: ${errs.join('; ')}`);
		await ctx.close();
	}
	// 6b. "I am" + Mine, on a board with 2 human owners (the example board has one: the setting is hidden there)
	await mkdir(join(root, 'zteam/tickets'), { recursive: true });
	await writeFile(join(root, 'zteam/tickets/unblockd.json'), JSON.stringify({ id: 'zteam', title: 'Team board', owners: [{ name: 'Dana', kind: 'human' }, { name: 'Omri', kind: 'human' }, { name: 'Claude', kind: 'agent' }], default_owner: 'Dana', review: true }));
	const cliT = (...a) => execFileSync('python3', ['-m', 'unblockd.cli', '--dir', join(root, 'zteam/tickets'), ...a], { cwd: repo, stdio: 'pipe' }).toString();
	cliT('create', '--prefix', 'ZT', '--title', 'dana owns', '--json', '{"owner":"Dana"}');
	const zt2 = JSON.parse(cliT('create', '--prefix', 'ZT', '--title', 'agent for dana', '--json', '{"owner":"Claude"}')).id;
	cliT('claim', '--agent', 'backend-1', '--id', zt2, '--principal', 'Dana');
	cliT('create', '--prefix', 'ZT', '--title', 'omri owns', '--json', '{"owner":"Omri"}');
	for (const [w, h] of [[1400, 900], [390, 844]]) for (const theme of ['light']) { // a setting, not a look: dark is covered by the quick dark passes
		const tag = `i-am/${w}px/${theme}`;
		const ctx = await browser.newContext({ viewport: { width: w, height: h }, colorScheme: theme });
		await ctx.addInitScript((t) => { try { localStorage.setItem('theme', JSON.stringify(t)); } catch {} }, theme);
		const page = await ctx.newPage(); page.setDefaultTimeout(5000); lastPage = page;
		const errs = [];
		page.on('pageerror', (e) => errs.push('pageerror: ' + e.message));
		page.on('console', (m) => m.type() === 'error' && errs.push('console.error: ' + m.text()));
		const ok = (c, msg) => { if (!c) throw new Error(`[${tag}] ${msg}` + (errs.length ? '\n  browser errors so far:\n  ' + errs.join('\n  ') : '')); };
		const noOver = async (what) => { const sw = await page.evaluate(() => [document.documentElement.scrollWidth, innerWidth]); ok(sw[0] <= sw[1], `${what}: horizontal overflow ${sw[0]} > ${sw[1]}`); };
		const cards = () => page.evaluate(() => [...document.querySelectorAll('#board .card')].map((e) => e.dataset.open).sort());
		await page.goto(base); await page.waitForSelector('.card', { state: 'attached' }); // the example board: one human, nothing to choose
		await page.click('#spanel > summary');
		ok(await page.locator('#setMe').isHidden(), 'the I am setting should be hidden with one human owner');
		await page.click('#spanel > summary');
		const qid = JSON.parse(cliT('create', '--prefix', 'ZT', '--title', 'question for the team', '--json', '{"owner":"Claude"}')).id;
		cliT('claim', '--agent', 'backend-1', '--id', qid); cliT('ask', qid, '--agent', 'backend-1', '--question', 'Which way?');
		const rvid = JSON.parse(cliT('create', '--prefix', 'ZT', '--title', 'agent work to review', '--json', '{"owner":"Claude"}')).id; // finished by an agent for Omri: Dana will approve it
		cliT('claim', '--agent', 'backend-1', '--id', rvid, '--principal', 'Omri'); cliT('complete', rvid, '--agent', 'backend-1', '--summary', 'did it', '--principal', 'Omri');
		await page.goto(base + '?board=zteam'); await page.waitForSelector('.card', { state: 'attached' });
		ok(await page.locator('#mineSeg').isHidden(), 'Mine should be hidden until I am is set');
		await page.click('#spanel > summary');
		await page.waitForSelector('#setMe:not([hidden])');
		await noOver('settings with I am');
		await page.click('#setMe [data-pref="me:Dana"]');
		await page.click('#spanel > summary');
		const all = await cards(); ok(all.length >= 4, `expected all tickets, got ${all}`);
		await page.click('#fpanel > summary'); await page.click('#mineSeg [data-mine="1"]');
		await page.waitForSelector('#pills [data-mine="0"]');
		const mine = await cards();
		ok(mine.length === 2 && mine.includes(zt2) && mine.includes('ZT-1'), `Mine should be what Dana owns or an agent works on for Dana: ${mine}`);
		ok(new URL(page.url()).searchParams.get('me') === '1', `URL should carry me=1: ${page.url()}`);
		await page.click('#fpanel > summary');
		await page.goto(page.url()); await page.waitForSelector('.card', { state: 'attached' }); // round trip: the link reproduces the view
		await page.waitForSelector('#pills [data-mine="0"]');
		ok(JSON.stringify(await cards()) === JSON.stringify(mine), 'reloading the link should show the same Mine view');
		await noOver('Mine view');
		if (process.env.SHOTS) await page.screenshot({ path: `${process.env.SHOTS}/me-${w}-${theme}.png` });
		await page.click('#pills [data-mine="0"]'); // the pill removes it
		await page.waitForFunction(() => !new URL(location.href).searchParams.has('me') && !document.querySelector('#pills [data-mine]'));
		ok((await cards()).length === all.length, 'removing the pill should show everything again');
		await page.click('#needsBadge'); // answer from the Needs panel: the note is signed with the I am name
		const qrow = `#npList .nr[data-key="zteam/${qid}"]`;
		await page.fill(`${qrow} input[data-draft]`, 'This way'); await page.keyboard.press('Enter');
		await page.waitForFunction((k) => !document.querySelector(`#npList .nr[data-key="${k}"]`), `zteam/${qid}`);
		const ans = await page.evaluate((i) => fetch('/api/b/zteam/tickets/' + i).then((r) => r.json()), qid);
		ok(ans.notes.some((n) => n.by === 'Dana' && n.kind === 'answer' && n.q === 'Which way?' && n.text === 'This way'), `the answer should be signed Dana: ${JSON.stringify(ans.notes)}`);
		// approve in the Needs panel: the approver is recorded and the drawer says so
		const vrow = `#npList .nr[data-key="zteam/${rvid}"]`;
		await page.click(`${vrow} .nttl`); await page.click(`${vrow} [data-approve]`);
		await page.waitForFunction((k) => !document.querySelector(`#npList .nr[data-key="${k}"]`), `zteam/${rvid}`);
		const appr = await page.evaluate((i) => fetch('/api/b/zteam/tickets/' + i).then((r) => r.json()), rvid);
		ok(appr.status === 'done' && appr.result.approved_by === 'Dana' && appr.result.principal === 'Omri', `approve should record Dana: ${JSON.stringify(appr.result)}`);
		await page.keyboard.press('Escape');
		await page.evaluate((i) => document.querySelector(`#board .card[data-open="${i}"]`).click(), rvid); await page.waitForSelector('#drawer .rbox'); // the Done tab may be hidden at 390px
		ok((await page.innerText('#drawer .rbox')).includes('Done by backend-1 for Omri · approved by Dana'), `drawer Result line: ${await page.innerText('#drawer .rbox')}`);
		ok((await page.getAttribute(`#board .card[data-open="${rvid}"]`, 'title')).endsWith(' · Done by backend-1 for Omri · approved by Dana'), 'done card tooltip: title, then who did it');
		await noOver('drawer with approver');
		if (process.env.SHOTS) await page.screenshot({ path: `${process.env.SHOTS}/approved-${w}-${theme}.png` });
		await page.keyboard.press('Escape');
		ok(!errs.length, 'browser errors:\n  ' + errs.join('\n  '));
		await ctx.close();
	}
	// 7. editing the architecture from the Map, on a board with no architecture folder (the empty state): add components, connect, edit, remove + Undo, delete
	await mkdir(join(root, 'zempty/tickets'), { recursive: true });
	await writeFile(join(root, 'zempty/tickets/unblockd.json'), JSON.stringify({ id: 'zempty', title: 'Empty board' }));
	const cliE = (...a) => execFileSync('python3', ['-m', 'unblockd.cli', '--dir', join(root, 'zempty/tickets'), ...a], { cwd: repo, stdio: 'pipe' }).toString();
	const docsE = join(root, 'zempty/docs/arch'), note = (k) => readFile(join(docsE, k + '.md'), 'utf8').catch(() => null);
	cliE('create', '--prefix', 'T', '--title', 'uses web');
	const tp = join(root, 'zempty/tickets/T-1.json'), tj = JSON.parse(await readFile(tp, 'utf8'));
	await writeFile(tp, JSON.stringify({ ...tj, components: ['web'] })); // an open ticket that lists the component Web (written by hand: the note does not exist yet)
	for (const [w, h] of [[1400, 900], [390, 844]]) {
		const tag = `arch-edit/${w}px`, wide = w > 700;
		await rm(join(root, 'zempty/docs'), { recursive: true, force: true });
		const ctx = await browser.newContext({ viewport: { width: w, height: h }, colorScheme: 'dark' });
		await ctx.addInitScript(() => { try { localStorage.setItem('theme', JSON.stringify('dark')); } catch {} });
		const page = await ctx.newPage(); page.setDefaultTimeout(5000); lastPage = page;
		const errs = [];
		page.on('pageerror', (e) => errs.push('pageerror: ' + e.message));
		page.on('console', (m) => m.type() === 'error' && !/status of 400/.test(m.text()) && errs.push('console.error: ' + m.text())); // the refusals this part provokes (duplicate key, delete while a ticket lists it) are 400s on purpose
		const ok = (c, msg) => { if (!c) throw new Error(`[${tag}] ${msg}` + (errs.length ? '\n  browser errors so far:\n  ' + errs.join('\n  ') : '')); };
		const noOver = async (what) => { const sw = await page.evaluate(() => [document.documentElement.scrollWidth, innerWidth]); ok(sw[0] <= sw[1], `${what}: horizontal overflow ${sw[0]} > ${sw[1]}`); };
		const vis = (sel) => page.locator(sel).first().waitFor({ state: 'visible' });
		const submit = (form) => page.click(`#drawer [data-aform=${form}] button[type=submit]`);
		const addComp = async (title, key, summary = '', paths = '') => {
			await page.fill('#drawer [data-draft="an:title"]', title);
			if (key) await page.fill('#drawer [data-draft="an:key"]', key);
			await page.fill('#drawer [data-draft="an:summary"]', summary); await page.fill('#drawer [data-draft="an:paths"]', paths);
			await submit('new');
		};
		await page.goto(base + '?board=zempty');
		await page.click('#viewSeg [data-view="map"]');
		await vis('#mapMsg [data-anew]');
		ok(/no component notes yet/.test(await page.innerText('#mapMsg')) && (await page.locator('#mapIn .mc, #mapList .mc').count()) === 0, 'empty state: the button above the how-to, no cards');
		await noOver('empty state');
		// first component: the key follows the title until it is edited by hand
		await page.click('#mapMsg [data-anew]'); await vis('#drawer [data-aform=new]');
		await page.fill('#drawer [data-draft="an:title"]', 'Photo Store');
		ok(await page.inputValue('#drawer [data-draft="an:key"]') === 'photo-store', 'key derived from the title');
		await page.fill('#drawer [data-draft="an:key"]', 'photos'); await page.fill('#drawer [data-draft="an:title"]', 'Photo Storage');
		ok(await page.inputValue('#drawer [data-draft="an:key"]') === 'photos', 'a key edited by hand stays');
		await noOver('new component form');
		await addComp('Photo Storage', '', 'Stores uploaded photos.', 'src/photos/**\ninfra/s3.tf');
		await page.waitForFunction(() => document.getElementById('dTitle')?.innerText === 'Photo Storage' && document.querySelector('#mapWrap .mc'));
		ok(await note('photos') === '---\npaths: src/photos/**, infra/s3.tf\n---\n# Photo Storage\n\nStores uploaded photos.\n', 'the note is written as a plain markdown file');
		ok(await page.evaluate(() => document.activeElement?.dataset.mc) === 'photos', 'focus stays on the edited component');
		// a second one: a refused key shows inline; "a" (wide) or the toolbar opens the form
		await page.click('#drawer [data-close]');
		await page.click('#mTools [data-anew]'); await vis('#drawer [data-aform=new]');
		await addComp('Dup', 'photos');
		await page.waitForSelector('#drawer .msg.err');
		ok(/already exists/.test(await page.innerText('#drawer .msg.err')) && (await page.inputValue('#drawer [data-draft="an:title"]')) === 'Dup', 'duplicate key: refused inline, the draft stays');
		await page.click('#drawer [data-close]');
		if (wide) { await page.keyboard.press('a'); await vis('#drawer [data-aform=new]'); ok(await page.inputValue('#drawer [data-draft="an:title"]') === 'Dup', 'a draft survives closing the form'); }
		else { await page.click('#mTools [data-anew]'); await vis('#drawer [data-aform=new]'); }
		await addComp('Web', 'web', 'The page.', 'web/**');
		await page.waitForFunction(() => document.getElementById('dTitle')?.innerText === 'Web');
		ok(await page.locator('#mapWrap .mc').count() >= 2, 'two components drawn');
		// a connection, with the "save without a contract?" question once
		await page.click('#drawer [data-aadd]');
		await page.fill('#c-to .pq', 'photo'); await page.click('#c-to .pl li[data-pk]');
		await noOver('add connection form');
		await submit('conn'); await vis('#drawer .cwarn');
		ok((await note('web')).indexOf('Connections') < 0, 'nothing written while the question is open');
		await page.fill('#drawer [data-draft="ac:web:text"]', 'direct presigned PUT; never proxy bytes');
		ok(await page.locator('#drawer .cwarn').count() === 0, 'typing a contract clears the question');
		await submit('conn'); await vis('#drawer [data-cedit]');
		ok((await note('web')).endsWith('## Connections\n- [Photo Storage](photos.md) — direct presigned PUT; never proxy bytes\n'), 'the edge is one canonical line in the note');
		if (wide) ok(await page.locator('#mapSvg .eg').count() === 1, 'the edge is drawn');
		// edit the contract: Esc cancels (and does not close the panel), Enter saves
		await page.click('#drawer [data-cedit]'); await vis('#drawer .cedit input');
		await page.fill('#drawer .cedit input', 'thrown away'); await page.keyboard.press('Escape');
		ok(await page.locator('#drawer .cedit').count() === 0 && await page.locator('#drawer').isVisible() && /direct presigned/.test(await page.innerText('#drawer .mcl')), 'Esc cancels the line edit and keeps the panel');
		await page.click('#drawer [data-cedit]'); await page.fill('#drawer .cedit input', 'presigned PUT only'); await page.keyboard.press('Enter');
		await page.waitForFunction(() => /presigned PUT only/.test(document.querySelector('#drawer .mcl')?.innerText || ''));
		ok((await note('web')).includes('— presigned PUT only\n'), 'Enter saves the contract');
		// remove + Undo
		await page.click('#drawer [data-crm]'); await vis('.toast button');
		ok(!(await note('web')).includes('photos.md') && await page.locator('#drawer .mcl').count() === 0, 'remove: the line is gone');
		await page.click('.toast button');
		await page.waitForFunction(() => /presigned PUT only/.test(document.querySelector('#drawer .mcl')?.innerText || ''));
		ok((await note('web')).includes('- [Photo Storage](photos.md) — presigned PUT only\n'), 'Undo puts the same line back');
		// title + summary: hand-written prose around the managed parts stays
		await writeFile(join(docsE, 'web.md'), (await note('web')) + '\n## Notes\nWritten by hand, keep me.\n');
		await page.click('#drawer [data-aedit]'); await vis('#drawer [data-aform=edit]');
		await page.fill('#drawer [data-draft="ae:web:title"]', 'Web UI'); await page.fill('#drawer [data-draft="ae:web:summary"]', 'The single page.');
		await noOver('edit form');
		if (wide) { // the poll re-renders the panel: half-typed text and the caret stay
			await page.fill('#drawer [data-draft="ae:web:title"]', 'Web UI draft'); cliE('create', '--prefix', 'T', '--title', 'poll trigger');
			await backOnTab(page); // what the 4 s poll does, now
			ok(await page.inputValue('#drawer [data-draft="ae:web:title"]') === 'Web UI draft' && await page.evaluate(() => document.activeElement?.dataset.draft) === 'ae:web:title', 'a draft survives the 4 s poll');
			await page.fill('#drawer [data-draft="ae:web:title"]', 'Web UI');
		}
		await submit('edit');
		await page.waitForFunction(() => document.getElementById('dTitle')?.innerText === 'Web UI');
		const web = await note('web');
		ok(web.startsWith('---\npaths: web/**\n---\n# Web UI\n\nThe single page.\n\n## Connections\n- [Photo Storage]') && web.endsWith('\n## Notes\nWritten by hand, keep me.\n'), 'only title and summary changed in the note');
		// delete: refused while an open ticket lists it, then allowed
		await page.click('#drawer .ovf > summary'); await page.click('#drawer [data-adel]');
		ok(/removes 1 connection/.test(await page.innerText('#drawer .adel')), 'delete asks, and says how many connections go');
		await page.click('#drawer [data-adelgo]'); await page.waitForSelector('#drawer .adel .msg.err .idlink[data-open="T-1"]');
		ok(await note('web') !== null, 'refused: the note stays');
		await page.click('#drawer [data-acancel=del]');
		await page.evaluate(() => fetch('/api/b/zempty/tickets/T-1', { method: 'PATCH', headers: { 'Content-Type': 'application/json' }, body: '{"status":"done"}' }));
		await page.click('#drawer .ovf > summary'); await page.click('#drawer [data-adel]'); await page.click('#drawer [data-adelgo]');
		await page.waitForFunction(() => !document.querySelector('#mapWrap [data-mc="web"]'));
		ok(await note('web') === null && await note('photos') !== null, 'delete removes the note');
		await noOver('after delete');
		await page.evaluate(() => fetch('/api/b/zempty/tickets/T-1', { method: 'PATCH', headers: { 'Content-Type': 'application/json' }, body: '{"status":"todo"}' }));
		ok(!errs.length, 'browser errors:\n  ' + errs.join('\n  '));
		await ctx.close();
	}
	await cleanup();
	console.log(`UI smoke OK: board/graph/docs/map/drawer/filters/settings/agents/toast x 1400+390px (full flow light, quick pass dark), no errors, no overflow (${((Date.now() - t0) / 1000).toFixed(1)}s)`);
} catch (e) { await die(process.env.DEBUG ? e.stack : e.message + '\n  at ' + ((e.stack || '').match(/ui_smoke\.mjs:\d+/g) || ['?']).join(' <- ')); } // the test line(s), so a CI failure says where
