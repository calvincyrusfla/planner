# Juggle Planner

A standalone planner for juggling school, work, music, the label, the website, travel and social plans. Each area has its own to-do list in a tab, and a dashboard ties everything together on one color-coded calendar.

**Live app:** https://claude.ai/artifact/AvcQtPEN9Y3594XRjA9qrx

---

## Features

### Dashboard (landing page)
- **Summary counts:** overdue, due today, due in the next 7 days, and total open tasks.
- **Month calendar:** every dated task appears on its day, in its color. Use ‹ › to change months and **Today** to jump back.
- **Selected day panel:** click a day to see everything on it, then use **+ Add** to schedule something there.
- **Coming up:** overdue items first, then everything due in the next 14 days.
- **Calendar filters:** the tab names under the calendar show or hide each area.
- **Your lists:** one card per tab with its open-task count and the next due item.

### Tabbed to-do lists
- Each tab is its own list, grouped into **Overdue**, **Today**, **Upcoming**, **No date** and **Done** (Done is collapsed by default).
- Quick-add bar: title, optional start date, optional end date, time and color.
- **Multi-day tasks:** give a task an end date and it stretches across every day from start to end on the calendar. It counts as "Today" while it's in progress and only becomes overdue after its last day.
- Click any task to edit its title, tab, date, time, color, notes or done status, or to delete it.
- Tick the checkbox to mark a task done.

### Cycling between tabs
- Use the ‹ › arrows on either end of the tab bar.
- Press `[` (previous) and `]` (next) on the keyboard.

### Managing tabs
- **+ New tab** creates a list with a name and color.
- **Rename:** double-click a tab, or use **Rename & color** inside it.
- **Move left / Move right** reorders tabs.
- **Delete tab** removes the tab and its tasks, after a confirmation.

### Colors
- Ten colors: Coral, Amber, Grass, Teal, Sky, Cobalt, Violet, Berry, Clay, Slate.
- Each tab has a color, and its tasks use it by default.
- Any task can have its own color, which overrides the tab color on the calendar.

### Sharing (Claude version only)
1. Open the **Share** menu in the bar above the app.
2. Invite the person by email.
3. Give **view** access to see the calendar and lists (the app shows a read-only "View only" mode), or **edit** access to add, change and check off tasks.

Changes appear live for everyone. People you share with need a Claude account.

---

## Starting tabs

| Order | Tab | Color |
|---|---|---|
| 1 | NLP | Sky |
| 2 | Computer Vision | Cobalt |
| 3 | Pre-Cal & Trig | Teal |
| 4 | ESC | Grass |
| 5 | Sales | Amber |
| 6 | DJ Gigs | Violet |
| 7 | Record Label | Berry |
| 8 | Website Moderation | Slate |
| 9 | Travel | Coral |
| 10 | Social | Clay |

---

## Data model

In the Claude artifact, data lives in the artifact's shared database and syncs across devices and everyone the app is shared with. On the standalone site, the same two collections are stored in the browser's `localStorage` under the key `juggle.data`.

**`tabs`** collection

| Field | Type | Notes |
|---|---|---|
| `name` | string | Tab label |
| `color` | string | Hex color |
| `order` | number | Position in the tab bar |

**`tasks`** collection

| Field | Type | Notes |
|---|---|---|
| `title` | string | Task text |
| `tab` | string | ID of the tab it belongs to |
| `due` | string | Start date, `YYYY-MM-DD`, empty if undated |
| `end` | string | Last day for a multi-day task, `YYYY-MM-DD`; empty or missing means a one-day task |
| `time` | string | `HH:MM` (24h), optional |
| `color` | string | Hex color; empty means "use the tab color" |
| `notes` | string | Optional |
| `done` | boolean | Completion state |
| `created` | number | Timestamp (ms) |
| `doneAt` | number | Timestamp (ms) when completed |

Small per-device preferences (last open tab, hidden calendar filters) are kept in the browser's local storage.

---

## Netlify site: shared calendar

On Netlify the planner saves to a shared store, so the link shows the same tabs and tasks on every device and to everyone you send it to.

- **Viewing:** anyone with the link sees the calendar and lists (read-only), refreshed every 20 seconds.
- **Editing:** tap **Unlock editing** and enter the passcode. The browser remembers it. Give the passcode only to people who should be able to change things.
- **Share:** the Share button gives you the link to copy.
- **Backup:** downloads every tab and task as a JSON file, and can import one. Importing only adds what's missing; it never deletes.

### One-time setup in Netlify

1. **Site configuration → Environment variables → Add a variable**: key `EDIT_KEY`, value = the passcode you want. (Without it, the site is view-only for everyone.)
2. **Deploys → Trigger deploy → Deploy site**, so the function picks up the passcode.

### How earlier data is kept

- The shared store starts with the tabs and tasks from the Claude artifact (`netlify/functions/seed.json`).
- Tasks you typed into the earlier Netlify version are still in that browser. The first time you unlock editing in that browser, they're added to the shared store automatically (skipping duplicates). The browser's own copy is never deleted, and **Backup → Add this browser's saved tasks** can add it again at any time.
- Do this on every device where you entered tasks before.

### How it works

- `index.html`: the whole app. Inside Claude it uses the artifact's database; on Netlify it uses `/api/planner`; opened as a local file it falls back to browser storage.
- `netlify/functions/planner.mjs`: reads and writes one JSON document in Netlify Blobs. Edits are sent as individual add, update, delete or merge operations with conditional writes, so two people saving at once don't overwrite each other.

### Running locally

Opening `index.html` directly works with browser storage only. To run the shared mode locally, use the Netlify CLI:

```bash
npm install
```

```bash
EDIT_KEY=yourpasscode npx netlify dev
```

---

## Agent Department

`department/` holds a local dashboard for monitoring the 3 agent teams (5 builders and a reviewer each), their task assignments and their progress. Run `npm run dashboard`, then see [department/README.md](department/README.md). To get a desktop shortcut with a network icon that starts the dashboard and opens it in your browser, run `npm run shortcut` once (Windows, macOS and Linux).

The dashboard has a Claude usage meter at the top (5-hour and weekly limits). To connect it, run `npm run usage-meter` once on your PC and restart Claude Code; see "Usage meter" in [department/README.md](department/README.md).
