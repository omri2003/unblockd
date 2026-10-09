// UI smoke test: python server on a copy of example/, headless Chromium, 2 viewports x 2 themes.
// Local: NODE_PATH=$(npm root -g) node tests/ui_smoke.mjs   (CI: npm i --no-save playwright)
import { createRequire } from 'node:module';
import { spawn, execFileSync } from 'node:child_process';
import { mkdtemp, cp, readFile, writeFile } from 'node:fs/promises';
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
	await cp(join(repo, 'example'), join(root, 'zsecond'), { recursive: true }); // second board: the title becomes a switcher
	const cfgP = join(root, 'zsecond/tickets/unblockd.json'), cfg = JSON.parse(await readFile(cfgP, 'utf8'));
	await writeFile(cfgP, JSON.stringify({ ...cfg, id: 'zsecond', title: 'Second board' }));
	cli('create', '--prefix', 'SM', '--title', 'smoke ticket', '--json', '{"owner":"Claude","pillar":"API"}');
	cli('claim', '--agent', 'backend-1', '--note', 'smoke');
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
		await page.evaluate(() => document.getElementById('kbHint').click()); // hidden at <=600px (no keyboard there), so click it directly
		await visible('#kb', 'shortcuts sheet from the hint');
		await page.keyboard.press('Escape');

		await page.click('[data-view="graph"]');
		await visible('#graph', 'graph canvas');
		await noOverflow('graph');
		await page.click('[data-view="docs"]');
		await visible('#dStage .dn', 'docs strip node'); // #dSvg (edges) is display:none on mobile by design
		if (w > 600) await visible('#dSvg', 'docs strip svg');
		await noOverflow('docs');
		await page.click('[data-view="board"]');

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
		await toggle('#spanel', 'Settings panel');
		await toggle('#apanel', 'Agents panel');

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
		await page.click('[data-edit]'); // Blocked by picker: search, pick, chip appears, remove it
		const nChips = await page.locator('#f-blocked .pc').count();
		await page.fill('#f-blocked .pq', 'smoke');
		await page.keyboard.press('Enter');
		ok((await page.locator('#f-blocked .pc').count()) === nChips + 1, `[${tag}] picking a blocker should add a chip`);
		ok(await page.locator('#f-blocked .pc[data-id="SM-1"]').count(), `[${tag}] picked chip should be SM-1`);
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

		// Needs you: a review ticket + a question ticket -> strip lists both; answer one, approve the other, strip empties
		const rid = JSON.parse(cli('create', '--prefix', 'NY', '--title', 'review me', '--json', '{"owner":"Claude","done_when":["it works"]}')).id;
		cli('claim', '--agent', 'backend-1', '--id', rid); // result is only written by complete(); the example board has no review gate, so move it to review by hand
		cli('complete', rid, '--agent', 'backend-1', '--summary', 'Did the **thing**', '--file', 'a.py', '--ref', 'https://example.com/pull/1', '--handoff', 'nothing');
		cli('update', rid, '--json', '{"status":"review"}');
		const qid = JSON.parse(cli('create', '--prefix', 'NY', '--title', 'question me', '--json', '{"owner":"Claude","priority":"P1"}')).id;
		cli('create', '--prefix', 'NY', '--title', 'after the question', '--json', `{"owner":"Claude","blocked_by":["${qid}"]}`);
		cli('claim', '--agent', 'backend-1', '--id', qid);
		cli('ask', qid, '--agent', 'backend-1', '--question', 'Which one?'); // an agent question: in the strip
		cli('create', '--prefix', 'NY', '--title', 'parked by hand', '--json', '{"owner":"Claude","waiting_on":"Omri: later"}'); // hand-parked: not in the strip
		// the CLI wrote the files; the page only sees them on its next 4 s poll, so wait for the condition (default 30 s), not a fixed delay
		await page.waitForFunction(() => document.querySelectorAll('#needs li').length === 2);
		await visible('#needs li', 'Needs you strip');
		ok((await page.locator('#needs li').count()) === 2, `[${tag}] Needs you should list the review and the question`);
		ok((await page.locator('#needs .n').innerText()) === '2', `[${tag}] Needs you count`);
		await noOverflow('needs strip');
		ok((await page.locator('#needs li').first().locator('.nb').innerText()) === 'Question', `[${tag}] higher score (P1, blocks a ticket) should sort first`);
		ok(!(await page.locator('#needs').innerText()).includes('parked by hand'), `[${tag}] hand-parked ticket must not be in the strip`);
		ok((await page.locator(".card:has-text(\"parked by hand\") .why.park").first().innerText()) === 'Parked: Omri: later', `[${tag}] hand-parked card should say Parked`);
		// collapse -> one summary line; survives reload; a NEW question re-opens it
		const sum = () => page.locator('#ndT').innerText();
		await page.click('#ndT');
		ok((await page.getAttribute('#ndT', 'aria-expanded')) === 'false', `[${tag}] collapsed`);
		ok(/^!\s*2 need you · oldest .+ · blocking 1 ticket$/s.test((await sum())), `[${tag}] summary text: ${await sum()}`);
		await page.waitForFunction(() => getComputedStyle(document.getElementById('ndBody')).visibility === 'hidden');
		await page.reload(); await visible('#ndT', 'summary after reload');
		ok((await page.getAttribute('#ndT', 'aria-expanded')) === 'false', `[${tag}] collapsed state should survive reload`);
		const xid = JSON.parse(cli('create', '--prefix', 'NY', '--title', 'late question', '--json', '{"owner":"Claude"}')).id;
		cli('claim', '--agent', 'backend-1', '--id', xid); cli('ask', xid, '--agent', 'backend-1', '--question', 'Again?');
		await page.waitForFunction(() => document.getElementById('ndT').getAttribute('aria-expanded') === 'true');
		await page.waitForFunction(() => document.querySelectorAll('#needs li').length === 3);
		cli('delete', xid); // back to 2; it stays open
		await page.waitForFunction(() => document.querySelectorAll('#needs li').length === 2);
		ok((await page.getAttribute('#ndT', 'aria-expanded')) === 'true', `[${tag}] strip should stay open when an item leaves`);
		await page.click('[data-view="graph"]'); // the strip is a board thing: hidden in Graph, back on return
		await page.waitForFunction(() => document.getElementById('needs').hidden).catch(() => {}); // the view switch cross-fades (View Transition), so it lands a frame later
		ok(await page.locator('#needs').isHidden(), `[${tag}] Needs you strip should be hidden in graph view`);
		await page.click('[data-view="board"]');
		await visible('#needs li', 'Needs you strip back on board');
		await page.click('#needs li:has(.nb.r) [data-open]'); // review drawer: result + done_when on top, activity open
		await visible('#drawer .rbox a[href^="https://example.com"]', 'review result with link');
		ok(await page.locator('#drawer details.kn[open]').count(), `[${tag}] review drawer should open Knowledge and activity`);
		await noOverflow('review drawer');
		if (process.env.SHOTS) await page.screenshot({ path: `${process.env.SHOTS}/ui6-drawer-${w}-${theme}.png` });
		await page.keyboard.press('Escape');
		await page.fill('#needs [data-nform="answer"] input', 'half typed'); // survives the 4 s poll
		await page.waitForTimeout(4500);
		ok((await page.inputValue('#needs [data-nform="answer"] input')) === 'half typed', `[${tag}] draft lost on poll`);
		await page.evaluate(() => scrollTo(0, 0));
		await page.screenshot({ path: process.env.SHOTS ? `${process.env.SHOTS}/ui6-${w}-${theme}.png` : tmpdir() + '/ui6.png' });
		await page.fill('#needs [data-nform="answer"] input', 'The first one');
		await page.click('#needs [data-nform="answer"] button[type=submit]');
		await page.waitForFunction(() => document.querySelectorAll('#needs li').length === 1);
		await page.click('#needs [data-approve]');
		await page.waitForFunction(() => document.getElementById('needs').hidden);
		ok(await page.locator('.col.done .card:has-text("review me")').count(), `[${tag}] approved ticket should be in Done`);
		await noOverflow('board after needs');

		ok(!errs.length, `[${tag}] browser errors:\n  ` + errs.join('\n  '));
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
	await cleanup();
	console.log(`UI smoke OK: board/graph/docs/drawer/filters/settings/agents/toast x 1400+390px x light+dark, no errors, no overflow (${((Date.now() - t0) / 1000).toFixed(1)}s)`);
} catch (e) { await die(e.message); }
