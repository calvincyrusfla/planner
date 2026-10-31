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
- Quick-add bar: title, optional date and time, optional color.
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
| `due` | string | `YYYY-MM-DD`, empty if undated |
| `time` | string | `HH:MM` (24h), optional |
| `color` | string | Hex color; empty means "use the tab color" |
| `notes` | string | Optional |
| `done` | boolean | Completion state |
| `created` | number | Timestamp (ms) |
| `doneAt` | number | Timestamp (ms) when completed |

Small per-device preferences (last open tab, hidden calendar filters) are kept in the browser's local storage.

---

## Standalone site (Netlify)

`index.html` is the whole app: one self-contained page with no build step.

- **Inside Claude** it saves through the artifact's shared database (`window.claude.use("db")`).
- **Anywhere else** (Netlify, a local file) it saves to the browser's `localStorage`. The starting tabs are created on first visit. Data stays on that one device and browser, so clearing site data erases it, and it does not sync between devices or people. The Share button is hidden in this mode.

Syncing across devices on the hosted site would mean swapping the storage functions (`add`, `upd`, `del`, `loadLocal`) for a backend such as Firebase or Supabase.

### Deploying to Netlify

1. In Netlify, choose **Add new site → Import an existing project → GitHub**, and pick this repository.
2. Choose the branch to deploy. `netlify.toml` already sets the publish directory to the repo root with no build command.
3. Deploy. Every push to that branch redeploys the site.

### Running locally

Open `index.html` in a browser, or serve the folder:

```bash
python3 -m http.server 8000
```
