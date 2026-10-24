---
name: unblockd-design
description: Use before designing or reviewing any change to static/index.html (board, drawer, Needs you panel, Graph, Docs, Map, forms, toasts) to apply unblockd's UI conventions, owner decisions and the review checklist.
---

# unblockd design skill (one-file vanilla web UI)

Source of truth: `AGENTS.md`. This skill points at its bullets by name. If this skill and AGENTS.md disagree, AGENTS.md wins, and you fix this skill in the same commit.
Tags: **[AGENTS]** a rule written in AGENTS.md (bullet name in quotes). **[GENERAL]** a basic accessibility rule that AGENTS.md does not spell out. Use it, but do not cite AGENTS.md for it.

## 1. How to use this skill

- Reviewers: read sections 2 and 3, then score the change against section 6. Report each failed check as `check number, screen/state, evidence, smallest fix`. Do not restyle what passes.
- Designers: follow sections 2 to 5 while you work. Self-score with section 6 before you hand off.
- Read AGENTS.md "Conventions" and "Traps that already bit" first. They are long. Search them for the feature you touch.
- Always check these states:
  - **Empty**: a new board, an empty column, the Needs you panel with nothing in it.
  - **Loading**: first paint with no data yet.
  - **Partial**: filters hide everything, a search with no hits, one board versus several, a board with no architecture notes.
  - **Normal**: the usual case.
  - **Error**: 401, an unknown board (404), a 409 conflict, a refused write, the server gone.
  - **Large**: 50+ tickets, a 130-character title, 20+ done tickets, a long path as a chip.
- Always check these modes too: light, dark, System, comfortable and compact density, 390px wide, and reduced motion.

## 2. Precedence

When rules clash, the first one wins:

1. **Owner decisions** in AGENTS.md. Do not re-argue them. They are listed in 2.1.
2. **Basic accessibility** (section 4.5).
3. **AGENTS.md "Conventions" and "Traps that already bit"**.
4. **Existing patterns** in `static/index.html`. Reuse the helper before you write another.
5. **Taste**.

Ties go to the simplest thing that works.

### 2.1 Owner decisions (do not reverse)

- One JSON file per ticket in the project's git repo. [AGENTS: "Why files, not a database"]
- Stdlib Python, one-file UI, no build step, no CDN libraries. [AGENTS: "Conventions"]
- Needs you is a header badge plus a side panel. There is no strip on the board, no status strip and no Working now band. [AGENTS: "Needs you: header badge + side panel"]
- The Done column shows only recent tickets, capped. Search and "view all" show the rest. [AGENTS: "Filter chrome"]
- Schema is strict for agents and guided for humans (warnings, never blocked). [AGENTS: "Schema and proposal gate"]
- The proposal gate (`agent_tickets: "propose"`) is opt-in. [AGENTS: "Schema and proposal gate"]
- `pending` is derived. The UI never lets you set it. [AGENTS: "Traps that already bit"]
- Auth is one shared token. There is no per-user identity yet. [AGENTS: "Auth", "Not built yet"]
- Motion is owner-approved, small, and off under reduced motion. [AGENTS: "Motion"]

### 2.2 Do not re-propose what exists

Check AGENTS.md and the `?` sheet first. Already built: Board, Graph, Docs, Map and Roadmap views; the `Ctrl/Cmd+K` command palette; the Board's List layout (`l`); `j`/`k` card focus; `p` then `0`-`3` priority; bulk bar; Needs you panel (`i`); digest; split; Undo toasts; settings (theme, density, digest); free pickers; Component and Milestone filters and Group by; agent stats panel. Improve them. Do not add a second one.

## 3. Hard rules (from AGENTS.md "Conventions")

- **One file.** The UI is `static/index.html`: vanilla JS, no build, no CDN, no library. Do not add a UI file.
- **Colour tokens.** Colours are CSS tokens on `:root`, each one `light-dark(light, dark)` with `color-scheme: light dark`. There is no separate dark block. Add a token once with both values. Pillar colours come from config in `applyConfig`.
- **Drawing code.** A custom property holding `light-dark()` reads back unresolved. JS that draws (graph canvas, grain) reads colours through `readColors()`. Never use `getPropertyValue` on a token for drawing. Never use `closest('[data-theme]')`: `<html>` carries it.
- **Settings are per browser.** Keys `theme`, `density`, `digest` in `store`, set through `setPref`. A tiny inline script in `<head>` applies theme and density before first paint.
- **Poll every 4 s.** `load()` sends the last `ETag`. The UI re-renders only when the tickets JSON text changes. So the JSON must stay stable while nothing happens. Store timestamps, never "x seconds ago". Ages are filled in the browser by `tickAges`.
- **Menus.** Every popover `<details>` goes in `MENUS`, and nowhere else. `MENUS` and `closeMenus` give you outside-click, Esc, and render deferral while a menu is open.
- **Shared helpers.** Use `jf` (every fetch), `chips`/`chipsHtml` (every segmented control), `store`, `pickOf`, `hm`/`ago`, `toast`. Do not write a second one.
- **Segmented controls.** `slidePill` is the one indicator. A new pressed colour sets `--ind`, not `background`. Keep exactly one button pressed.
- **Motion.** At most 200 ms, card slides at most 280 ms, ease-out, only where it explains a change. Off under `prefers-reduced-motion: reduce`: CSS in the one `@media` block at the end of `<style>`, JS checks `reduce.matches`. JS motion is Web Animations (`el.animate`).
- **Init order.** Anything `chips()` or `slidePill` reaches runs during init. Every `const` they read must be declared above the first call (`reduce` once broke this).
- **The view lives in the URL.** A new filter needs a param in `urlWrite()`/`urlRead()`, validated with `pickOf`, plus a removable pill. [AGENTS: "View in the URL", "Filter chrome"]
- **Shortcuts.** A new shortcut updates the `?` sheet AND the tooltip. Do not reuse `b`, `B`, `i`, `p`. Keys already in use include `n`, `g`, `d`, `m`, `r`, `l`, `c`, `a` (in the Map), `t`, `j`, `k`, and `Ctrl/Cmd+K`. [AGENTS: "Boards", "Priority", "Needs you"]

## 4. Interaction rules

### 4.1 Undo, not confirm
- Reversible actions use a toast with Undo (`toast(msg,{err,undo})`). Undo is one PATCH back. Do not add `window.confirm`. The Map delete uses an inline confirm sentence instead.
- A refusal from the store shows in the place of the action (inline text or a toast with the server's message). Never drop it silently.

### 4.2 Drafts survive the poll
- Half-typed text lives in `DRAFTS`, keyed by `data-draft`, saved on `input`, refilled by `draftField`. The 4 s refresh and a collapse/expand must not lose it.
- While a menu is open, `load()` keeps the data and defers the render to `paint`.
- Do not rebuild the bulk bar or an open panel from the poll if its inputs did not change (`keepFocus` restores the caret).
- A 409 conflict reloads the ticket and keeps the draft. The edit form sends the `rev` it was opened with.
- Store an ETag WITH the data it describes. Race-test anything with overlapping fetches. [AGENTS: "Traps that already bit"]

### 4.3 One handler, not two
- The panel and the drawer share one submit handler for every `form[data-nform]` (`submitNeeds`, `nPatch`). Do not fork it.
- Panel forms and buttons carry `data-board` and call `/api/b/<board>/...`, so they work on any board.
- Ctrl/Cmd+Enter sends a `textarea[data-draft]`. Enter is a newline there.
- `<input value>` drops newlines. Send `DRAFTS[key]`, not the field value.

### 4.4 Layers and Esc
- The Needs you panel and the drawer share `#scrim`. Opening one closes the other. Whoever closes second must not leave the scrim up or hide it from the other.
- Esc closes the top-most layer in this order: hint (`p`), command palette, `?` sheet, menus, panel, drawer.
- Closing the drawer rewrites the URL. Keep `location.search` (`?board=`).

### 4.5 Basic accessibility [GENERAL unless noted]
- Every control has an accessible name, a role and a state. Segmented buttons use `aria-pressed`. [AGENTS: "View switch" in "Boards"]
- Toasts live in one `aria-live` region (`#toasts`).
- Focus is visible. The CSS has a `:focus-visible` ring. Keep it.
- Meaning is never colour alone. Pair colour with a word, icon or shape.
- Contrast: text 4.5:1, UI parts 3:1, in both themes.
- Touch targets are about 44px on touch screens, with space between neighbours.
- A drag or gesture has a keyboard or tap alternative: the card's "Move to" menu, Shift/Ctrl-click select, the Map edit form.
- Hover is never the only way to learn something. Map contracts are in the side panel too.

## 5. Patterns that matter

### 5.1 Needs you (badge + panel)
- Source is `GET /api/needs`. Kinds in this precedence: review, proposal, question, stuck.
- Parked (`waiting_on` with no `asked_by`) is never listed and never counted. Cards and drawer say `Parked: <waiting_on>`.
- A question is free text. Never split `waiting_on` into a person. Say `Question from <asked_by>`. [AGENTS: "Traps that already bit"]
- Collapsed row: ID, title, kind badge, `blocks N`, age, one line, a one-line input. Click expands ONE row at a time.
- Rows fade in and slide out through `needsMotion`. The badge pulses once for a new item, never while the panel is open.
- The stuck row says in plain words what is wrong and what Release does.

### 5.2 Drawer
- Order: title, status/owner, details, blockers, then a collapsed Knowledge and activity `<details>`. Ask / Show in graph / Delete live in `.ovf`.
- In review, the drawer leads with the result and Done when, and opens Knowledge and activity without touching the remembered `knOpen`.
- A ticket with `waiting_on` has an Answer box. Every ticket has an Add note box.
- The 4 s refresh must not re-animate or reset the drawer.

### 5.3 Board, cards, columns
- Cards are buttons wrapped in `.cw`. Moves go through `moveTo()` (drag-drop, Move to, drawer status). It refuses `doing` with open blockers.
- Do not use class `board` on anything but the board grid. Use `.bref` for board links in chat.
- At 600px and below, `#tabs` shows one column at a time. `body[data-tab]` drives CSS. Never use a bare `[data-tab]` selector in the click handler.
- Card ages, heartbeats and the lease bar are filled by `tickAges`, never in the JSON. The Pending column is labelled Waiting (value stays `pending`); owners carry a person/bot glyph; claimed cards show `live ·` / `quiet ·` with a dot shape, not colour alone.
- Empty and "filters hide everything" use `emptyState()` and a way out (`clearFilters`).

### 5.4 Pickers
- `pickHtml` is the only way to enter ticket ids. No free-text id can be submitted.
- Docs, Touches and Tags pickers suggest, they do not restrict. Enter adds what was typed.
- A schema board's prefix stays a `<select>`. Otherwise it is a native `<datalist>`.

### 5.5 Views
- Graph: Show mode and Isolate are separate. Do not reuse `state.q` or `showGraph` for isolate.
- Docs and Map are DOM + SVG on purpose. Tokens resolve in CSS there.
- Map: positions are per browser (`mapPos.<board>`), not committed. Contracts show in a hover tooltip and in the panel, never on the canvas. At 700px and below it is a list.
- `[hidden]{display:none!important}` is needed. A component that sets `display` would otherwise ignore the `hidden` attribute.

### 5.6 Copy
- Plain words. Say what happens next ("Release it so someone can pick it up").
- Do not show derived field names like `effective_status` in the UI.
- Every empty, loading and error state gets a sentence and a next step.

### 5.7 Front doors
- A UI change that needs a new Store operation also needs the REST route AND a `Remote` method, or remote workers lose it. Add the MCP tool and CLI command when agents need it. [AGENTS: "Remote CLI"]

## 6. Reviewer checklist (yes/no; score = yes count / applicable)

Mark a check "n/a" when the change cannot touch it.

**Scope and decisions**
1. No owner decision from 2.1 is reversed?
2. The feature is not already built (AGENTS.md and the `?` sheet checked)?
3. One file, vanilla JS, no library, no CDN, no build step, no new UI file?
4. No control or chip is added to the header or a card unless it replaces one?

**Look**
5. Colours are `:root` tokens with `light-dark()` once?
6. JS that draws reads resolved colours through `readColors()`, not `getPropertyValue`?
7. It looks right in light, dark and System, comfortable and compact?
8. No horizontal overflow at 390px (`scrollWidth === 390`) on each view it touches?
9. Copy is plain, says what happens next, shows no internal field names?
10. Empty, loading and error states each have a sentence and a next step?
11. New chrome is hidden or reduced at 600px and below, and `body[data-tab]` still works?

**Accessibility**
12. Every new control has a name, role and state?
13. Meaning never rests on colour alone?
14. Contrast is 4.5:1 for text and 3:1 for UI parts in both themes?
15. Touch targets are about 44px with space between them?
16. Every drag or gesture has a keyboard or tap alternative?
17. Focus is visible, tab order is logical, and Esc closes the top-most layer in the AGENTS.md order?

**Behaviour**
18. A new shortcut is in the `?` sheet and the tooltip, and is not `b`, `B`, `i` or `p`?
19. A new popover is in `MENUS` and nowhere else?
20. A poll-triggered render leaves open menus, drafts, caret, scroll and focus alone?
21. The tickets JSON stays stable while idle (timestamps, not "x ago")?
22. Reversible actions use Undo, not `window.confirm`?
23. Every refusal shows in the place of the action with the server's text?
24. A 409 reloads and keeps the draft?
25. A new filter, Group or Show value has a URL param, a `pickOf` check and a removable pill?
26. Ticket ids come from a picker only. Docs, Touches and Tags pickers still suggest without restricting?
27. Motion is at most 200 ms (cards 280), ease-out, explains a change, and is off under reduced motion?
28. Every `const` that `chips()`/`slidePill` reads is declared above the first call?

**Needs you, drawer, board**
29. Needs you precedence (review, proposal, question, stuck), the badge count and "parked is never counted" are unchanged?
30. A `waiting_on` is never parsed as a person?
31. The drawer opens with what the human must decide, and `knOpen` is untouched?
32. Panel and drawer share handlers and drafts (`submitNeeds`, `nPatch`, `data-nform`), not copies?
33. The Done column still obeys its window and cap, and search or "view all" still shows everything?

**Safety and process**
34. Front doors agree: a new Store operation has its REST route and `Remote` method (and MCP/CLI if agents need it)?
35. No known trap is hit: `.board` class, `[hidden]` override, `pkill -f` self-kill, bare `[data-tab]`, ETag stored with its data, `<input value>` newline loss?
36. Docs moved with code: the AGENTS.md bullet, `docs/reference.md` if user-visible, the `?` sheet? (CHANGELOG is the maintainer's.)
37. Verified with section 7, and the report says what was NOT verified?

## 7. Verify

1. `python -m unittest` is green.
2. `NODE_PATH=$(npm root -g) node tests/ui_smoke.mjs` passes. Put new checks next to the checks of the same feature, not at the end of the file.
3. Run `UNBLOCKD_ROOT=<temp copy of example/> PORT=8822 python -m unblockd.web`. Screenshot with Playwright at 1400px light and dark, and at 390px. Never run `playwright install`.
4. Check zero page errors, and `scrollWidth === 390` on mobile.
5. Stop the server with `pgrep -f "[u]nblockd.web" | xargs -r kill`, in a command that does not start a server.
6. Say what you did NOT verify: a real touch device, a screen reader, 200% font size, real Docker, a real API key, a real MCP client.

## 8. Report format

One line per finding:

`UI-<n> (High|Med|Low, S|M|L) <screen/state>: <evidence: file:line or screenshot>. <why it hurts>. Fix: <smallest fix>.`

End with:
- `Scorecard: <yes>/<applicable>`, and the numbers of the failed checks.
- `Not verified: ...`
