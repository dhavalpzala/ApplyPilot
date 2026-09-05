"""Local-model apply driver: an agent loop over Playwright, no Claude CLI.

The Claude driver (launcher.run_job) shells out to `claude -p`, which reaches
Chrome through npx + @playwright/mcp over CDP. This module does the same job
in-process: it connects to the very same CDP-exposed Chrome with Playwright
Python, exposes the tools in apply/tools.py to an OpenAI-compatible endpoint
(LM Studio, typically), and runs the tool-call loop itself.

run_job_local() mirrors run_job()'s signature and return contract exactly, so
worker_loop() can dispatch to either.

No personal data lives here -- the profile is injected via apply/prompt.py.
"""

from __future__ import annotations

import logging
import time
from datetime import datetime
from pathlib import Path

from playwright.sync_api import sync_playwright

from applypilot import config
from applypilot.apply import prompt as prompt_mod
from applypilot.apply import tools as tools_mod
from applypilot.apply.dashboard import add_event, get_state, update_state
from applypilot.apply.tools import (
    TOOL_SCHEMAS,
    BrowserTools,
    ResultReported,
    strip_template_artifacts,
)
from applypilot.llm import get_client

logger = logging.getLogger(__name__)

# A straightforward application is 10-20 turns. The cap is a runaway guard, not
# a target; hitting it is reported as failed:max_turns.
MAX_TURNS = 40

# Tool output the model never needs twice. Superseding old snapshots is what
# keeps context flat across a long application: measured local latency is ~9s
# at 3.5k prompt tokens but ~117s at 41k, and un-pruned snapshots reach that
# within roughly ten turns.
_SUPERSEDED = "[snapshot superseded - call browser_snapshot for the current page]"

# Output budget per turn. Tool calls are short; this is not the place to be
# generous, because unused budget still costs nothing but overruns cost a retry.
_MAX_TOKENS = 1024


def _is_snapshot_result(message: dict) -> bool:
    """True for a tool message holding a (now stale) page snapshot."""
    return (
        message.get("role") == "tool"
        and message.get("name") in tools_mod.SNAPSHOT_TOOLS
        and message.get("content") != _SUPERSEDED
    )


def _prune_history(messages: list[dict]) -> None:
    """Replace all but the most recent snapshot with a short placeholder.

    Mutates in place. Without this the transcript grows without bound and every
    subsequent turn re-processes every snapshot the agent has ever taken.
    """
    snapshot_indexes = [i for i, m in enumerate(messages) if _is_snapshot_result(m)]
    for i in snapshot_indexes[:-1]:
        messages[i]["content"] = _SUPERSEDED


def _describe_call(name: str, args: dict) -> str:
    """One-line label for the dashboard/log, mirroring the Claude driver's."""
    short = name.replace("browser_", "")
    if "url" in args:
        return f"{short} {str(args['url'])[:60]}"
    if "ref" in args:
        return f"{short} {args.get('element', args.get('text', args['ref']))}"[:50]
    if "fields" in args:
        return f"{short} ({len(args['fields'] or [])} fields)"
    if "paths" in args:
        return f"{short} upload"
    if "status" in args:
        return f"{short} {args['status']}"
    return short


def _parse_args(raw: str) -> dict:
    """Parse a tool call's JSON arguments, tolerating an empty string."""
    import json

    if not raw or not raw.strip():
        return {}
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError:
        return {}
    return parsed if isinstance(parsed, dict) else {}


def run_job_local(job: dict, port: int, worker_id: int = 0,
                  model: str | None = None, dry_run: bool = False) -> tuple[str, int]:
    """Apply to one job using the local model + Playwright.

    Drop-in replacement for launcher.run_job(). Chrome must already be running
    with --remote-debugging-port=<port> (chrome.launch_chrome does this).

    Args:
        job: Job row from the database.
        port: CDP port of this worker's Chrome.
        worker_id: Worker slot, for logs and the dashboard.
        model: Ignored -- the model comes from LLM_MODEL via the LLM singleton.
            Present so the signature matches run_job().
        dry_run: Never click Submit. Enforced in tools.py, not the prompt.

    Returns:
        (status_string, duration_ms), same vocabulary as run_job().
    """
    from applypilot.apply.launcher import _stop_event, parse_result

    resume_path = job.get("tailored_resume_path")
    txt_path = Path(resume_path).with_suffix(".txt") if resume_path else None
    resume_text = txt_path.read_text(encoding="utf-8") if txt_path and txt_path.exists() else ""

    system_prompt = (
        prompt_mod.build_prompt(job=job, tailored_resume=resume_text,
                                dry_run=dry_run, native_captcha=True)
        + "\n\n"
        + prompt_mod.build_local_addendum(dry_run=dry_run)
    )

    update_state(worker_id, status="applying", job_title=job["title"],
                 company=job.get("site", ""), score=job.get("fit_score", 0),
                 start_time=time.time(), actions=0, last_action="connecting")
    add_event(f"[W{worker_id}] Starting: {job['title'][:40]} @ {job.get('site', '')}")

    worker_log = config.LOG_DIR / f"worker-{worker_id}.log"
    ts_header = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    log_header = (
        f"\n{'=' * 60}\n"
        f"[{ts_header}] {job['title']} @ {job.get('site', '')}  (local driver)\n"
        f"URL: {job.get('application_url') or job['url']}\n"
        f"Score: {job.get('fit_score', 'N/A')}/10\n"
        f"{'=' * 60}\n"
    )

    start = time.time()
    deadline = start + config.DEFAULTS["local_apply_timeout"]
    transcript: list[str] = []
    client = get_client()

    messages: list[dict] = [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content":
            "Begin the application now. Start by navigating to the job URL, "
            "then call browser_snapshot to see the page."},
    ]

    def finish(status: str) -> tuple[str, int]:
        elapsed = int(time.time() - start)
        add_event(f"[W{worker_id}] {status.upper()} ({elapsed}s): {job['title'][:30]}")
        update_state(worker_id, status=status.split(":")[0],
                     last_action=f"{status[:25]} ({elapsed}s)")
        return status, int((time.time() - start) * 1000)

    playwright = None
    browser = None
    try:
        playwright = sync_playwright().start()
        try:
            browser = playwright.chromium.connect_over_cdp(f"http://localhost:{port}")
        except Exception as exc:  # noqa: BLE001 - reported as a clean status
            logger.error("CDP connect to port %d failed: %s", port, exc)
            return finish("failed:chrome_cdp_unreachable")
        if not browser.contexts:
            return finish("failed:no_browser_context")
        context = browser.contexts[0]
        browser_tools = BrowserTools(context, dry_run=dry_run, screenshot_dir=config.LOG_DIR)

        with open(worker_log, "a", encoding="utf-8") as lf:
            lf.write(log_header)

            for turn in range(MAX_TURNS):
                if _stop_event.is_set():
                    return "skipped", int((time.time() - start) * 1000)
                if time.time() > deadline:
                    lf.write(">> deadline exceeded\n")
                    return finish("failed:timeout")

                _prune_history(messages)
                reply = client.chat_tools(messages, TOOL_SCHEMAS, max_tokens=_MAX_TOKENS)
                content = strip_template_artifacts(reply.get("content", ""))
                tool_calls = reply.get("tool_calls") or []

                if content:
                    transcript.append(content)
                    lf.write(content + "\n")

                if not tool_calls:
                    # The prompt asks for a RESULT: line; honour it if present.
                    if "RESULT:" in content:
                        elapsed = int(time.time() - start)
                        status = parse_result(content, worker_id, job, elapsed)
                        return status, int((time.time() - start) * 1000)
                    # Otherwise nudge once, then give up rather than loop silently.
                    messages.append({"role": "assistant", "content": content})
                    messages.append({"role": "user", "content":
                        "You must either call a tool or finish by calling "
                        "report_result. Do not reply with prose alone."})
                    continue

                messages.append({
                    "role": "assistant",
                    "content": content,
                    "tool_calls": tool_calls,
                })

                for call in tool_calls:
                    fn = call.get("function", {})
                    name = fn.get("name", "")
                    args = _parse_args(fn.get("arguments", ""))
                    label = _describe_call(name, args)
                    lf.write(f"  >> {label}\n")
                    lf.flush()

                    state = get_state(worker_id)
                    update_state(worker_id,
                                 actions=(state.actions if state else 0) + 1,
                                 last_action=f"t{turn + 1} {label}"[:35])

                    try:
                        result = browser_tools.dispatch(name, args)
                    except ResultReported as reported:
                        lf.write(f"  << RESULT:{reported.status}"
                                 f"{':' + reported.reason if reported.reason else ''}\n")
                        elapsed = int(time.time() - start)
                        line = f"RESULT:{reported.status}"
                        if reported.status == "FAILED":
                            line += f":{reported.reason}"
                        status = parse_result(line, worker_id, job, elapsed)
                        return status, int((time.time() - start) * 1000)

                    messages.append({
                        "role": "tool",
                        "tool_call_id": call.get("id", ""),
                        "name": name,
                        "content": result,
                    })

            lf.write(">> hit MAX_TURNS without a result\n")
            return finish("failed:max_turns")

    except Exception as exc:  # noqa: BLE001 - never let one job abort the pipeline
        logger.exception("Local apply driver failed for %s", job.get("url"))
        return finish(f"failed:{str(exc).splitlines()[0][:100]}")

    finally:
        if transcript:
            ts = datetime.now().strftime("%Y%m%d_%H%M%S")
            site = job.get("site", "unknown")[:20]
            job_log = config.LOG_DIR / f"local_{ts}_w{worker_id}_{site}.txt"
            job_log.write_text("\n".join(transcript), encoding="utf-8")
        # Only detach from Chrome -- the worker loop owns its lifecycle.
        try:
            if browser is not None:
                browser.close()
        except Exception:  # noqa: BLE001 - teardown is best effort
            pass
        try:
            if playwright is not None:
                playwright.stop()
        except Exception:  # noqa: BLE001 - teardown is best effort
            pass
