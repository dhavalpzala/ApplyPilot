# Changelog

All notable changes to ApplyPilot will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added
- **Local apply driver — auto-apply without the Claude Code CLI.** `applypilot apply` now
  defaults to `--driver local`, which runs the apply agent in-process against any
  OpenAI-compatible endpoint (LM Studio, Ollama, llama.cpp) and drives Chrome directly with
  Playwright Python. The Claude path is unchanged and still available via `--driver claude`.
  This removes node/npx and the `@playwright/mcp` server from the auto-apply path entirely,
  so no `npm_config_registry` / `PLAYWRIGHT_MCP_CLI` workarounds are needed, and applying
  costs nothing per job.
  - `apply/tools.py` reimplements the Playwright MCP tool surface (`browser_snapshot`,
    `browser_click`, `browser_fill_form`, `browser_file_upload`, …) on top of
    `page.aria_snapshot(mode="ai")` and `aria-ref=` locators, so `apply/prompt.py` is reused
    as-is. No new dependencies — Playwright was already required.
  - `apply/local_agent.py` runs the tool-call loop, with `run_job_local()` mirroring
    `run_job()`'s signature and status vocabulary so `worker_loop()` just picks one.
  - `llm.py` gains `chat_tools()`, which preserves `tool_calls` (`chat()` discarded them).
  - CAPTCHAs are solved by a native Python `solve_captcha` tool instead of the agent
    executing ~190 lines of CapSolver JavaScript. Besides being far more reliable on a small
    model, this cuts the system prompt — re-processed every turn — by 37%.
  - `--dry-run` is now enforced in code: `browser_click` refuses submit-like elements rather
    than relying on the agent to obey the instruction.
  - Snapshots are filtered to interactive and informative nodes (measured 56–92% smaller on
    representative pages, with every actionable ref preserved) and superseded snapshots are
    pruned from history. Both are load-bearing: local latency measured ~9s at 3.5k prompt
    tokens but ~117s at 41k, and unfiltered ATS snapshots land in that upper band.

### Fixed
- **`doctor` reported the wrong LLM provider.** It checked `GEMINI_API_KEY` before `LLM_URL`,
  while `llm.py::_detect_provider` gives `LLM_URL` precedence over both API keys. With a
  Gemini key and a local URL both set — the documented local-mode setup — doctor claimed
  Gemini while every stage actually ran on the local endpoint. It now asks
  `_detect_provider()` directly, and probes local endpoints for reachability and for whether
  `LLM_MODEL` is among the models actually being served.
- **Tier 3 was unreachable without the Claude Code CLI.** `get_tier()`/`check_tier()` are now
  driver-aware, so the local driver needs only an LLM provider plus Chrome. `doctor` reports
  the Claude CLI and `npx` as optional, needed only for `--driver claude`.
- **Reasoning models silently scored every job 0** - models like Qwen3 spend their
  completion budget on hidden reasoning tokens before emitting any visible text. With
  `max_tokens=512` the budget was exhausted mid-thought, returning HTTP 200 with an empty
  `content` and `finish_reason="length"`, which parsed as score 0. `llm.py` now detects
  truncated-empty responses and retries with a 4x larger budget (up to 8192) instead of
  returning nothing. Neither `/no_think` nor `chat_template_kwargs.enable_thinking=False`
  suppresses reasoning on Qwen3.8, so a larger budget is the only reliable fix.
- **LLM failures poisoned the score queue** - `run_scoring()` wrote `fit_score=0` on any
  LLM exception. Since the pending query selects `fit_score IS NULL`, those rows were
  permanently skipped and reported as "scored". Transport failures now leave `fit_score`
  NULL so the next run retries them, and log a warning with the count.
- **Timeouts against local models** - the 120s timeout was tuned for hosted APIs; a local
  27B reasoning model needs several minutes per request, and retrying doesn't help because
  the model is simply still working. Local endpoints now default to 900s, overridable with
  `LLM_TIMEOUT`.
- **Crash on Gemini `MAX_TOKENS`** - a thinking model that exhausted its budget returned a
  candidate with no `parts`, raising `KeyError`. The native Gemini path now indexes
  defensively.

### Changed
- Raised default output budgets for reasoning headroom: score 512 -> 2048, tailor judge
  512 -> 2048, cover letter 1024 -> 3072.

## [0.2.0] - 2026-02-17

### Added
- **Parallel workers for discovery/enrichment** - `applypilot run --workers N` enables
  ThreadPoolExecutor-based parallelism for Workday scraping, smart extract, and detail
  enrichment. Default is sequential (1); power users can scale up.
- **Apply utility modes** - `--gen` (generate prompt for manual debugging), `--mark-applied`,
  `--mark-failed`, `--reset-failed` flags on `applypilot apply`
- **Dry-run mode** - `applypilot apply --dry-run` fills forms without clicking Submit
- **5 new tracking columns** - `agent_id`, `last_attempted_at`, `apply_duration_ms`,
  `apply_task_id`, `verification_confidence` for better apply-stage observability
- **Manual ATS detection** - `manual_ats` list in `config/sites.yaml` skips sites with
  unsolvable CAPTCHAs (e.g. TCS iBegin)
- **Qwen3 `/no_think` optimization** - automatically saves tokens when using Qwen models
- **`config.DEFAULTS`** - centralized dict for magic numbers (`min_score`, `max_apply_attempts`,
  `poll_interval`, `apply_timeout`, `viewport`)

### Fixed
- **Config YAML not found after install** - moved `config/` into the package at
  `src/applypilot/config/` so YAML files (employers, sites, searches) ship with `pip install`
- **Search config format mismatch** - wizard wrote `searches:` key but discovery code
  expected `queries:` with tier support. Aligned wizard output and example config
- **JobSpy install isolation** - removed python-jobspy from package dependencies due to
  broken numpy==1.26.3 exact pin in jobspy metadata. Installed separately with `--no-deps`
- **Scoring batch limit** - default limit of 50 silently left jobs unscored across runs.
  Changed to no limit (scores all pending jobs in one pass)
- **Missing logging output** - added `logging.basicConfig(INFO)` so per-job progress for
  scoring, tailoring, and cover letters is visible during pipeline runs

### Changed
- **Blocked sites externalized** - moved from hardcoded sets in launcher.py to
  `config/sites.yaml` under `blocked:` key
- **Site base URLs externalized** - moved from hardcoded dict in detail.py to
  `config/sites.yaml` under `base_urls:` key
- **SSO domains externalized** - moved from hardcoded list in prompt.py to
  `config/sites.yaml` under `blocked_sso:` key
- **Prompt improvements** - screening context uses `target_role` from profile,
  salary section includes `currency_conversion_note` and dynamic hourly rate examples
- **`acquire_job()` fixed** - writes `agent_id` and `last_attempted_at` to proper columns
  instead of misusing `apply_error`
- **`profile.example.json`** - added `currency_conversion_note` and `target_role` fields

## [0.1.0] - 2026-02-17

### Added
- 6-stage pipeline: discover, enrich, score, tailor, cover letter, apply
- Multi-source job discovery: Indeed, LinkedIn, Glassdoor, ZipRecruiter, Google Jobs
- Workday employer portal support (46 preconfigured employers)
- Direct career site scraping (28 preconfigured sites)
- 3-tier job description extraction cascade (JSON-LD, CSS selectors, AI fallback)
- AI-powered job scoring (1-10 fit scale with rationale)
- Resume tailoring with factual preservation (no fabrication)
- Cover letter generation per job
- Autonomous browser-based application submission via Playwright
- Interactive setup wizard (`applypilot init`)
- Cross-platform Chrome/Chromium detection (Windows, macOS, Linux)
- Multi-provider LLM support (Gemini, OpenAI, local models via OpenAI-compatible endpoints)
- Pipeline stats and HTML results dashboard
- YAML-based configuration for employers, career sites, and search queries
- Job deduplication across sources
- Configurable score threshold filtering
- Safety limits for maximum applications per run
- Detailed application results logging
