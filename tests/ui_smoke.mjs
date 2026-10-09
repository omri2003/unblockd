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
let server, browser, lastPage;
const die = async (msg) => { console.error('UI SMOKE FAIL: ' + msg); if (process.env.DEBUG && lastPage) console.error(await lastPage.evaluate(() => 'TOASTS: ' + document.getElementById('toasts').innerText + '\nPANEL: ' + document.getElementById('npanel').innerText.slice(0, 600)).catch(() => '')); await cleanup(); process.exit(1); };
const cleanup = async () => { server?.kill(); await browser?.close().catch(() => {}); };
const ok = (c, msg) => { if (!c) throw new Error(msg); };

try {
	// 1. temp copy of example/ + a claim and a split, so claimed cards and activity exist
	const root = await mkdtemp(join(tmpdir(), 'unblockd-ui-'));
	await cp(join(repo, 'example'), join(root, 'example'), { recursive: true });
	const cli = (...a) => execFileSync('python3', ['-m', 'unblockd.cli', '--dir', join(root, 'example/tickets'), ...a], { cwd: repo, stdio: 'pipe' });
	const cliB = (...a) => execFileSync('python3', ['-m', 'unblockd.cli', '--dir', join(root, 'zsecond/tickets'), ...a], { cwd: repo, stdio: 'pipe' }).toString();
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

		const oid = JSON.parse(cliB('create', '--prefix', 'ZQ', '--title', 'other board question', '--json', '{"owner":"Claude"}')).id; // before the page loads: the other board is only re-read on the 15 s needs poll
		cliB('claim', '--agent', 'backend-1', '--id', oid); cliB('ask', oid, '--agent', 'backend-1', '--question', 'Which colour?');
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

		// Needs you (header badge + side panel, every board): a review + an agent question here, a question on the other board
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
		// the CLI wrote the files; the page sees this board on its next 4 s poll, so wait for the state
		await page.waitForFunction(() => document.querySelector('#needsBadge .n')?.textContent === '3');
		ok((await page.getAttribute('#needsBadge', 'title')).includes('This board: 2') && (await page.getAttribute('#needsBadge', 'title')).includes('Second board: 1'), `[${tag}] badge tooltip should split the count per board`);
		for (const v of ['graph', 'docs', 'board']) { // visible in every view; Free/Blocked only in the Graph
			await page.click(`[data-view="${v}"]`);
			await page.waitForFunction((x) => document.querySelector(`#viewSeg [data-view="${x}"]`).getAttribute('aria-pressed') === 'true' && !document.getElementById('needsBadge').hidden && (x === 'graph') === !document.getElementById('depSeg').hidden, v);
		}
		await page.click('#needsBadge'); // open via the badge, close with Esc, open again with the key
		await visible('#npanel .nr', 'Needs you panel');
		await page.keyboard.press('Escape');
		ok(await page.locator('#npanel').isHidden(), `[${tag}] Escape should close the panel`);
		await page.keyboard.press('i');
		await visible('#npanel .nr', 'Needs you panel via the i key');
		ok((await page.locator('#npList .nr').count()) === 3, `[${tag}] panel should list 2 here + 1 on the other board`);
		ok((await page.locator('#npList .ng').allInnerTexts()).join('|').replace(/\s+/g, ' ') === 'This board (2)|Second board (1)', `[${tag}] board groups: this board first`);
		ok((await page.locator('#npList .nr').first().locator('.nb').innerText()) === 'Question', `[${tag}] higher score (P1, blocks a ticket) should sort first`);
		ok(!(await page.locator('#npanel').innerText()).includes('parked by hand'), `[${tag}] hand-parked ticket must not be in the panel`);
		ok((await page.locator(".card:has-text(\"parked by hand\") .why.park").first().innerText()) === 'Parked: Omri: later', `[${tag}] hand-parked card should say Parked`);
		ok((await page.locator(`.card:has-text("question me") .why`).first().innerText()) === 'Question from backend-1', `[${tag}] agent question card should say Question from <agent>, not the first word`);
		await noOverflow('needs panel');
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
		ok((await badge()) === '2', `[${tag}] badge should drop to 2 after answering`);
		const answered = await page.evaluate((i) => fetch('/api/tickets/' + i).then((r) => r.json()), qid);
		ok(answered.body.includes('Answer to: Which one? — line one\nline two\nline three\nline four') && !answered.waiting_on, `[${tag}] the multi-line answer should be on the ticket: ${answered.body}`);
		// the review: open the ticket from the panel (the panel gives way to the drawer)
		await page.click(`#npList .nr:has(.nb.r) .nid`);
		await visible('#drawer .rbox a[href^="https://example.com"]', 'review result with link');
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
		await page.fill(`${rrow} textarea`, 'half typed');
		cli('note', rid, 'checked the diff', '--by', 'omri');
		await page.waitForFunction((r) => document.querySelector(`${r} .nn`)?.textContent.includes('checked the diff'), rrow); // the 4 s poll brought it
		ok((await page.inputValue(`${rrow} textarea`)) === 'half typed', `[${tag}] draft lost when the data changed`);
		await page.fill(`${rrow} textarea`, '');
		await page.click(`${rrow} [data-approve]`);
		await page.waitForFunction((k) => !document.querySelector(`#npList .nr[data-key="${k}"]`), `${boardId}/${rid}`);
		ok(await page.locator('.col.done .card:has-text("review me")').count(), `[${tag}] approved ticket should be in Done`);
		// answer the other board's question from here: no board switch
		const orow = `#npList .nr[data-key="zsecond/${oid}"]`;
		await page.fill(`${orow} input[data-draft]`, 'Teal'); await page.keyboard.press('Enter');
		await page.waitForFunction(() => document.getElementById('needsBadge').hidden);
		ok(page.url().includes('board=zsecond') === false, `[${tag}] answering must not switch boards`);
		const oans = await page.evaluate((i) => fetch('/api/b/zsecond/tickets/' + i).then((r) => r.json()), oid);
		ok(oans.body.includes('Answer to: Which colour? — Teal'), `[${tag}] the other board's ticket should carry the answer`);
		await page.waitForSelector('#npanel .none:not([hidden])'); // the panel stays open and says so
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
		await page.click(`${srow} [data-release]`);
		await page.waitForFunction((k) => !document.querySelector(`#npList .nr[data-key="${k}"]`), `${boardId}/${sid}`);
		const rel = await page.evaluate((i) => fetch('/api/tickets/' + i).then((r) => r.json()), sid);
		ok(rel.status === 'todo' && !rel.claim, `[${tag}] Release to To do should drop the claim: ${rel.status}`);
		cli('delete', did); cli('delete', bk);
		cli('delete', mid); // the other one goes when its ticket does
		await page.waitForFunction(() => document.getElementById('needsBadge').hidden);
		await page.keyboard.press('Escape');
		await page.evaluate(() => scrollTo(0, 0));
		await page.screenshot({ path: process.env.SHOTS ? `${process.env.SHOTS}/ui6-${w}-${theme}.png` : tmpdir() + '/ui6.png' });
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
} catch (e) { await die(process.env.DEBUG ? e.stack : e.message); }
