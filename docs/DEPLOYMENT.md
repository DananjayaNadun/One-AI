# Deployment

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

