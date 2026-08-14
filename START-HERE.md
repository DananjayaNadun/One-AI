# Start here

Three steps. About five minutes, most of it waiting for packages to install.

---

## Step 1 — Install Python (skip if you already have it)

Download from **https://www.python.org/downloads/**

On Windows, **tick "Add python.exe to PATH"** on the very first installer
screen. It is easy to miss and nothing works without it. If you miss it, run the
installer again and choose Modify.

On macOS the installer has no such option; just install it.

---

## Step 2 — Run setup

**Windows:** double-click **`setup.bat`**

**macOS / Linux:** open Terminal in this folder and run:

```bash
bash setup.sh
```

It creates a private Python environment inside the project folder, installs
everything, generates a `SECRET_KEY`, and checks the result. Nothing is
installed system-wide, so deleting this folder removes all of it.

Safe to run twice — it will not overwrite a `.env` you have already edited.

---

## Step 3 — Add your free API key

1. Go to **https://openrouter.ai/keys** and sign up (free, no card needed)
2. Create a key and copy it — it starts with `sk-or-v1-`
3. Open the **`.env`** file in this folder with any text editor
4. Find this line:

   ```
   OPENROUTER_KEY=sk-or-v1-your-key-here
   ```

   Replace everything after the `=` with your real key. No quotes, no spaces.

5. Save the file.

> **Windows tip:** if you cannot see the `.env` file, open File Explorer's
> **View** menu and tick **File name extensions** and **Hidden items**. If
> Windows saved it as `.env.txt`, rename it back to `.env`.

---

## Run it

**Windows:** double-click **`run.bat`**
**macOS / Linux:** `bash run.sh`

Your browser opens at **http://127.0.0.1:5000**. Leave the black terminal window
open — closing it stops the app. Press `Ctrl+C` there to stop it deliberately.

---

## If something is wrong

Run the built-in check. It tests every part of the install and tells you what to
do about each failure:

```
Windows:      .venv\Scripts\python.exe tools\doctor.py
macOS/Linux:  .venv/bin/python tools/doctor.py
```

Common ones:

| What you see | What it means |
|---|---|
| `Python is not installed` | Step 1, and tick "Add to PATH" |
| `OPENROUTER_KEY is still the placeholder` | Step 3 was not saved |
| `Could not reach OpenRouter` | Check internet, firewall, or VPN |
| `429` / rate limit while using it | Free tier daily cap. Switch to **Code** mode — it uses one request per message instead of four |
| Browser says "can't connect" | The terminal window was closed |

---

## Using it

The mode switch sits under the message box.

- **Code** — for programming. One request per message, large output budget. This
  is the default.
- **Direct** — one model, one request. Fast general answers.
- **Panel** — three models answer and a fourth reconciles them. Better for open
  questions, but it costs **four requests per message**.

The small bar next to the switch counts your free requests for the day. The free
tier allows roughly 200 per day, so panel mode gives you about 50 messages while
code mode gives you about 200.

Attach files with the paperclip, by dragging them onto the window, or by pasting
a screenshot. Images, PDF, Word documents, and code files all work.

**Everything is free.** The app refuses to call a paid model — the check happens
right before each request goes out, so a typo in `.env` cannot bill you.

Full details are in `README.md`.

---

## Moving it to another computer

Copy the whole folder, but **delete the `.venv` folder first** — it contains
paths specific to the old machine. Then run setup again on the new one.
