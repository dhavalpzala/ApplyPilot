# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Commands

```bash
# Dev install (repo has a .venv; prefix commands with .venv/bin/ or activate it)
pip install -e ".[dev]"
pip install --no-deps python-jobspy && pip install pydantic tls-client requests markdownify regex
playwright install chromium

# Lint (config lives in pyproject.toml: target py311, line-length 120)
ruff check src/
ruff check src/ --fix
ruff format src/

# Smoke-test the install
.venv/bin/applypilot --version
.venv/bin/applypilot doctor      # prints per-requirement status + detected tier

# Exercise the pipeline safely
applypilot run --dry-run             # prints stage plan, touches nothing
applypilot run score --min-score 8   # single stage
applypilot apply --dry-run           # drives the browser but never clicks Submit
applypilot apply --gen --url URL     # writes the agent prompt to a file for inspection
applypilot status                    # per-stage row counts from SQLite
```

**Tests:** there is no `tests/` directory yet. `CONTRIBUTING.md` and `.github/workflows/ci.yml` both reference `pytest tests/`, so CI (manual-trigger only) would fail on the test step. Create `tests/` before relying on those instructions. `pytest` is already in the `dev` extra.

**Undeclared dependency:** `wizard/resume_parser.py` imports `pydantic` unguarded, but `pydantic` is not in `pyproject.toml` dependencies — it arrives only via the second (jobspy) install command. `applypilot init` breaks without it.

## Architecture

Six sequential stages, each reading and writing one wide SQLite table. The database *is* the queue — there is no message broker, and every stage is independently resumable because it selects rows by which columns are still NULL.

```
discover → enrich → score → tailor → cover → pdf → apply
```

| Stage | Module | Writes columns | LLM? |
|---|---|---|---|
| discover | `discovery/{jobspy,workday,smartextract}.py` | `url,title,site,strategy,…` | smartextract only |
| enrich | `enrichment/detail.py` | `full_description, application_url` | tier-3 fallback only |
| score | `scoring/scorer.py` | `fit_score, score_reasoning` | yes |
| tailor | `scoring/tailor.py` | `tailored_resume_path, tailor_attempts` | yes |
| cover | `scoring/cover_letter.py` | `cover_letter_path, cover_attempts` | yes |
| pdf | `scoring/pdf.py` | rewrites `tailored_resume_path` `.txt`→`.pdf` | no |
| apply | `apply/launcher.py` | `apply_status, applied_at, apply_attempts, …` | Claude Code CLI |

Note: directory names are `discovery/`, `enrichment/`, `scoring/`, `apply/`, `wizard/`. The "Project Structure" section of `CONTRIBUTING.md` is stale (it lists `discover/`, `score/`, `tailor/`, `utils/`, `docs/`, none of which exist), as are its `applypilot discover --employer/--site` examples — no `discover` subcommand exists.

### State lives outside the repo

Everything user-specific is under `~/.applypilot/` (override with `APPLYPILOT_DIR`): `applypilot.db`, `profile.json`, `resume.txt`/`.pdf`, `searches.yaml`, `.env`, `tailored_resumes/`, `cover_letters/`, `logs/`, per-worker Chrome profiles. `config.py` is the single source of every path — never hardcode one. Package-shipped registries (`config/employers.yaml`, `config/sites.yaml`, `config/searches.example.yaml`) live inside `src/applypilot/config/` and are declared in `[tool.hatch.build] artifacts`; new YAML there is picked up by that glob.

### Database schema changes

`database.py` holds the whole schema. To add a column, add it to the `_ALL_COLUMNS` dict **and** the `CREATE TABLE` in `init_db()`. `ensure_columns()` diffs `PRAGMA table_info` against `_ALL_COLUMNS` on every startup and `ALTER TABLE ADD COLUMN`s the difference — forward-only, so columns are never removed or renamed. Connections are thread-local (`get_connection()`), WAL mode, `busy_timeout=10000`; parallel workers depend on this, so never share a connection across threads.

### Pipeline orchestration (`pipeline.py`)

Two modes over the same stage runners:
- **Sequential** (default): each stage runs to completion in order.
- **Streaming** (`--stream`): one thread per stage. Downstream stages poll `_PENDING_SQL[stage]` every 10s and exit only when their `_UPSTREAM` stage is marked done *and* pending count is 0. Adding a stage means updating `STAGE_ORDER`, `STAGE_META`, `_UPSTREAM`, `_STAGE_RUNNERS`, and `_PENDING_SQL` together, plus `VALID_STAGES` in `cli.py`.

Stage runners swallow exceptions into `{"status": "error: …"}` rather than raising, so a failing scraper never aborts the pipeline.

### LLM access (`llm.py`)

One `LLMClient` singleton via `get_client()`; never instantiate providers elsewhere. Provider is detected from env **at call time** (so `load_env()` in `_bootstrap()` is visible): `GEMINI_API_KEY` → `OPENAI_API_KEY` → `LLM_URL`, with `LLM_MODEL` overriding the model. Two quirks worth knowing before changing this file:
- Gemini starts on the OpenAI-compat shim; a 403 (typical for preview/experimental models) flips the client to the native `generateContent` API for the rest of the process.
- 429/503/timeout retry up to 5 times honouring `Retry-After`, base 10s doubling to 60s — tuned for the Gemini free tier's 15 RPM.

### Apply stage (`apply/`)

Not Playwright-driven from Python. Each worker:
1. `chrome.py` launches an isolated Chrome with `--remote-debugging-port=BASE_CDP_PORT + worker_id` (9222+) and a cloned user-data dir.
2. `launcher.py` writes `~/.applypilot/.mcp-apply-<worker_id>.json` pointing `@playwright/mcp` at that CDP port.
3. It shells out to `claude -p --mcp-config … --permission-mode bypassPermissions --output-format stream-json`, pipes the prompt on stdin, and parses the JSON event stream for tool calls (dashboard updates), cost, and the final text.
4. The agent's contract is a single `RESULT:` line — `APPLIED`, `EXPIRED`, `CAPTCHA`, `LOGIN_ISSUE`, or `FAILED:<reason>`. `run_job()` greps for it; no line means `failed:no_result_line`. If you change a result code in `apply/prompt.py`, update the parser and `PERMANENT_FAILURES`/`PERMANENT_PREFIXES` in `launcher.py`, which decide whether `apply_attempts` is bumped or slammed to 99 (never retry).

`acquire_job()` claims rows under `BEGIN IMMEDIATE` and sets `apply_status='in_progress'` — that lock is how parallel workers avoid double-applying, and it must be released (`release_lock`) on every early-exit path.

`apply/prompt.py` assembles the entire agent instruction set from `profile.json` + `searches.yaml`. It is long and prescriptive by design (eligibility gates, CAPTCHA/CapSolver flow, SSO refusal, safety refusals). Behaviour changes for the apply agent belong here, not in the launcher.

### Tier gating (`config.py`)

`get_tier()` returns 1 (discovery), 2 (+ LLM key), or 3 (+ `claude` on PATH and Chrome found). Commands call `check_tier(n, feature)`, which exits with a rendered list of what's missing. `doctor` reports the same checks individually. Gate any new LLM-dependent command at tier 2 and any browser/agent command at tier 3.

## Conventions

- **No hardcoded personal data.** Every prompt builder, validator, and scraper takes the user's `profile.json` (via `config.load_profile()`) at runtime. Several module docstrings state this explicitly; it is the invariant that made the project shareable. Same for search terms and site lists — those come from `searches.yaml` / `sites.yaml`.
- **Lazy imports in `cli.py`.** Commands import their implementation inside the function body to keep `applypilot --help` fast and to let tier-1 users run without heavy optional deps (jobspy, playwright) installed.
- **`_bootstrap()` before any DB work** in a CLI command: `load_env()` → `ensure_dirs()` → `init_db()`.
- **LLM output is never trusted directly.** `tailor.py`/`cover_letter.py` ask for structured JSON, then *code* assembles the final document — the resume header (name, contact) is always code-injected. `scoring/validator.py` then checks banned words, LLM leak phrases, fabrication against `profile["resume_facts"]`, and required sections. Strictness is `--validation strict|normal|lenient` (`normal` is default; banned words are warnings there). Failed generations increment `tailor_attempts`/`cover_attempts` and are abandoned at 5.
- Type hints and Google-style docstrings on public functions, `from __future__ import annotations` in newer modules. `CONTRIBUTING.md` says 100-char lines; Ruff is configured for 120 — Ruff wins.
- Update `CHANGELOG.md` under `[Unreleased]`. Releases are tag-driven: pushing `v*` triggers `publish.yml` (PyPI OIDC trusted publishing), so bump `version` in `pyproject.toml` and `__init__.py` together.
