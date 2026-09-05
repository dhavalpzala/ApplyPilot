"""
Playwright-backed implementations of the browser tools the apply agent uses.

This is the local-driver replacement for the `@playwright/mcp` server. The
Claude driver reaches Chrome through node/npx/MCP over CDP; this module reaches
the same Chrome directly from Python, exposing the same tool names so
apply/prompt.py can be reused verbatim.

Tool names deliberately match the Playwright MCP surface referenced in
apply/prompt.py (browser_navigate, browser_snapshot, browser_click, ...). Two
tools are additions: solve_captcha (native port of the CapSolver JS block) and
report_result (a structurally reliable terminator).

No personal data lives here -- the profile is injected via apply/prompt.py.
"""

from __future__ import annotations

import json
import logging
import os
import re
import time

import httpx

log = logging.getLogger(__name__)

# Per-action timeout. Deliberately short: a stale ref should fail fast and let
# the model re-snapshot rather than stall the whole turn budget.
ACTION_TIMEOUT_MS = 10_000
NAV_TIMEOUT_MS = 45_000

# Snapshot budget. At ~4 chars/token this keeps a snapshot near 1.5k tokens.
# Sizing matters: measured local latency is ~9s at 3.5k prompt tokens but ~117s
# at 41k, and an unfiltered aria snapshot of an ATS page lands in that upper band.
MAX_SNAPSHOT_CHARS = 6_000

# Roles the model can act on -- always kept, even when unnamed.
_INTERACTIVE_ROLES = frozenset({
    "textbox", "searchbox", "textarea", "button", "link", "combobox", "listbox",
    "option", "checkbox", "radio", "slider", "spinbutton", "switch",
    "menuitem", "menuitemcheckbox", "menuitemradio", "tab",
})

# Roles that carry meaning the model needs to read (errors, section headers).
_INFO_ROLES = frozenset({
    "heading", "alert", "alertdialog", "status", "dialog", "progressbar", "iframe",
})

_KEEP_ROLES = _INTERACTIVE_ROLES | _INFO_ROLES

# Structural scaffolding. Dropped even when it carries an accessible name --
# otherwise a page with many <p> blocks defeats the filter, since the generic
# "keep anything quoted" rule would let all the body copy through.
_LAYOUT_ROLES = frozenset({
    "generic", "paragraph", "list", "listitem", "region", "main", "navigation",
    "banner", "contentinfo", "article", "section", "group", "separator", "form",
    "img", "image", "figure", "table", "rowgroup", "row", "cell", "columnheader",
    "rowheader", "complementary", "blockquote", "code", "emphasis", "strong",
    "superscript", "subscript", "time", "deletion", "insertion", "caption",
    "definition", "term", "note", "presentation", "none", "document",
})

# Bare text nodes are kept because on many ATS forms an adjacent text node is
# the only label an unnamed input has. Every kept line is capped so that a job
# description sitting on the apply page can't blow the snapshot budget --
# aria_snapshot puts long content inline after the role
# ("- paragraph [ref=e6]: <500 words>"), not only in child nodes.
_MAX_NODE_CHARS = 160

# Matches "  - textbox \"Full name\" [ref=e5]:" -> indent, role.
# The optional quote matters: Playwright YAML-quotes the whole node when the
# accessible name contains characters needing escaping, e.g.
#   - 'heading "Apply: Staff Engineer" [level=1] [ref=e5]'
# Without tolerating it, every such node was silently dropped.
_NODE_RE = re.compile(r"^(?P<indent>\s*)-\s+'?(?P<role>[a-zA-Z]+)")

# Trailing "[ref=eN]" — must survive truncation or the node becomes unusable.
_TRAILING_REF_RE = re.compile(r"(\[ref=e\d+\])'?:?$")


def _truncate_node(line: str, cap: int = _MAX_NODE_CHARS) -> str:
    """Shorten an over-long node line without destroying its ref handle."""
    if len(line) <= cap:
        return line
    match = _TRAILING_REF_RE.search(line)
    if match:
        ref = match.group(1)
        keep = max(cap - len(ref) - 4, 0)
        return f"{line[:keep].rstrip()}... {ref}"
    return line[:cap].rstrip() + "..."


# Elements a dry run must never activate.
_SUBMIT_RE = re.compile(
    r"\b(submit|apply now|apply|send application|finish|confirm and send)\b",
    re.IGNORECASE,
)


class ResultReported(Exception):  # noqa: N818 - control-flow signal, not an error
    """Raised by report_result to unwind the agent loop with a final status."""

    def __init__(self, status: str, reason: str = "") -> None:
        self.status = status
        self.reason = reason
        super().__init__(f"{status}:{reason}" if reason else status)


class StaleRef(Exception):  # noqa: N818 - carries a recovery hint, not a crash
    """A ref no longer resolves, usually because the page moved on."""

    def __init__(self, ref: str) -> None:
        self.ref = ref
        super().__init__(
            f"ref {ref} not found on the current page - it is from an older "
            f"snapshot. Call browser_snapshot to get current refs."
        )


# ---------------------------------------------------------------------------
# Snapshot filtering
# ---------------------------------------------------------------------------

def filter_snapshot(raw: str, max_chars: int = MAX_SNAPSHOT_CHARS) -> str:
    """Strip non-actionable nodes from an aria snapshot and re-indent.

    page.aria_snapshot(mode="ai") emits the full accessibility tree, most of
    which is `generic`/`paragraph` scaffolding with no accessible name. Dropping
    those typically cuts an ATS page by an order of magnitude while preserving
    every ref the model can act on.

    Dropped nodes are transparent: their children are re-parented to the nearest
    surviving ancestor so indentation still reflects real structure.

    Args:
        raw: Output of page.aria_snapshot(mode="ai").
        max_chars: Hard cap on the returned string.

    Returns:
        Filtered snapshot, with an explicit marker if anything was truncated.
    """
    kept: list[str] = []
    ancestor_indents: list[int] = []
    dropped = 0

    for line in raw.splitlines():
        if not line.strip():
            continue

        stripped = line.strip()
        match = _NODE_RE.match(line)
        if match:
            indent = len(match.group("indent"))
            role = match.group("role").lower()
            # Keep actionable/informative roles and bare text nodes (labels);
            # drop layout scaffolding outright; be permissive about roles we
            # don't know, keeping them when they carry an accessible name.
            if role in _KEEP_ROLES or role == "text":
                keep = True
            elif role in _LAYOUT_ROLES:
                keep = False
            else:
                keep = '"' in line
        else:
            # Continuation lines of a multi-line text node.
            indent = len(line) - len(line.lstrip())
            keep = False

        while ancestor_indents and ancestor_indents[-1] >= indent:
            ancestor_indents.pop()

        if keep:
            kept.append("  " * len(ancestor_indents) + _truncate_node(stripped))
            ancestor_indents.append(indent)
        else:
            dropped += 1

    # A kept parent whose children were all dropped leaves a dangling colon.
    for i, line in enumerate(kept):
        if line.endswith(":"):
            this_indent = len(line) - len(line.lstrip())
            nxt = kept[i + 1] if i + 1 < len(kept) else ""
            if not nxt or (len(nxt) - len(nxt.lstrip())) <= this_indent:
                kept[i] = line[:-1]

    out = "\n".join(kept)
    if len(out) > max_chars:
        cut = out[:max_chars].rsplit("\n", 1)[0]
        omitted = out[len(cut):].count("\n") + 1
        out = f"{cut}\n[snapshot truncated - {omitted} more nodes. Scroll or narrow the page.]"
    if dropped:
        out += f"\n[{dropped} layout-only nodes hidden]"
    return out or "[empty page]"


def strip_template_artifacts(text: str) -> str:
    """Remove chat-template tokens some local models leak into content.

    gemma-4-*-mlx in LM Studio emits literal `<|channel>thought\\n<channel|>`
    markers into the content field. They corrupt RESULT: greps and logs.
    """
    if not text:
        return ""
    text = re.sub(r"<\|?channel\|?>", "", text)
    text = re.sub(r"<\|[a-z_]+\|>", "", text)
    return text.replace("thought\n", "").strip()


# ---------------------------------------------------------------------------
# CapSolver (native port of prompt.py::_build_captcha_section)
# ---------------------------------------------------------------------------

_CAPSOLVER_BASE = "https://api.capsolver.com"

_TASK_TYPES = {
    "hcaptcha": "HCaptchaTaskProxyLess",
    "recaptchav2": "ReCaptchaV2TaskProxyLess",
    "recaptchav3": "ReCaptchaV3TaskProxyLess",
    "turnstile": "AntiTurnstileTaskProxyLess",
    "funcaptcha": "FunCaptchaTaskProxyLess",
}

# Detection order matters: hCaptcha elements also carry data-sitekey, so they
# must be checked before reCAPTCHA.
_DETECT_JS = """() => {
  const r = {};
  const hc = document.querySelector('.h-captcha, [data-hcaptcha-sitekey]');
  if (hc) { r.type = 'hcaptcha'; r.sitekey = hc.dataset.sitekey || hc.dataset.hcaptchaSitekey; }
  if (!r.type && document.querySelector('script[src*="hcaptcha.com"], iframe[src*="hcaptcha.com"]')) {
    const el = document.querySelector('[data-sitekey]');
    if (el) { r.type = 'hcaptcha'; r.sitekey = el.dataset.sitekey; }
  }
  if (!r.type) {
    const cf = document.querySelector('.cf-turnstile, [data-turnstile-sitekey]');
    if (cf) {
      r.type = 'turnstile'; r.sitekey = cf.dataset.sitekey || cf.dataset.turnstileSitekey;
      if (cf.dataset.action) r.action = cf.dataset.action;
      if (cf.dataset.cdata) r.cdata = cf.dataset.cdata;
    }
  }
  if (!r.type && document.querySelector('script[src*="challenges.cloudflare.com"]')) {
    r.type = 'turnstile_script_only';
  }
  if (!r.type) {
    const s = document.querySelector('script[src*="recaptcha"][src*="render="]');
    if (s) { const m = s.src.match(/render=([^&]+)/);
      if (m && m[1] !== 'explicit') { r.type = 'recaptchav3'; r.sitekey = m[1]; } }
  }
  if (!r.type) {
    const rc = document.querySelector('.g-recaptcha');
    if (rc) { r.type = 'recaptchav2'; r.sitekey = rc.dataset.sitekey; }
  }
  if (!r.type && document.querySelector('script[src*="recaptcha"]')) {
    const el = document.querySelector('[data-sitekey]');
    if (el) { r.type = 'recaptchav2'; r.sitekey = el.dataset.sitekey; }
  }
  if (!r.type) {
    const fc = document.querySelector('#FunCaptcha, [data-pkey], .funcaptcha');
    if (fc) { r.type = 'funcaptcha'; r.sitekey = fc.dataset.pkey; }
  }
  if (!r.type && document.querySelector('script[src*="arkoselabs"], script[src*="funcaptcha"]')) {
    const el = document.querySelector('[data-pkey]');
    if (el) { r.type = 'funcaptcha'; r.sitekey = el.dataset.pkey; }
  }
  if (r.type) { r.url = window.location.href; return r; }
  return null;
}"""

_INJECT_JS = {
    "recaptchav2": """(token) => {
  document.querySelectorAll('[name="g-recaptcha-response"]').forEach(el => { el.value = token; el.style.display = 'block'; });
  if (window.___grecaptcha_cfg) {
    const clients = window.___grecaptcha_cfg.clients;
    for (const key in clients) {
      const walk = (obj, d) => {
        if (d > 4 || !obj) return;
        for (const k in obj) {
          if (typeof obj[k] === 'function' && k.length < 3) { try { obj[k](token); } catch(e) {} }
          else if (typeof obj[k] === 'object') walk(obj[k], d+1);
        }
      };
      walk(clients[key], 0);
    }
  }
  return 'injected';
}""",
    "hcaptcha": """(token) => {
  const ta = document.querySelector('[name="h-captcha-response"], textarea[name*="hcaptcha"]');
  if (ta) ta.value = token;
  document.querySelectorAll('iframe[data-hcaptcha-response]').forEach(f => f.setAttribute('data-hcaptcha-response', token));
  return 'injected';
}""",
    "turnstile": """(token) => {
  const inp = document.querySelector('[name="cf-turnstile-response"], input[name*="turnstile"]');
  if (inp) inp.value = token;
  return 'injected';
}""",
    "funcaptcha": """(token) => {
  const inp = document.querySelector('#FunCaptcha-Token, input[name="fc-token"]');
  if (inp) inp.value = token;
  if (window.ArkoseEnforcement) { try { window.ArkoseEnforcement.setConfig({data: {blob: token}}); } catch(e) {} }
  return 'injected';
}""",
}
_INJECT_JS["recaptchav3"] = _INJECT_JS["recaptchav2"]


def _capsolver_solve(detected: dict, api_key: str) -> tuple[str, str]:
    """createTask -> poll -> token.

    Returns:
        (token, error). Exactly one is non-empty.
    """
    kind = detected.get("type", "")
    task_type = _TASK_TYPES.get(kind)
    if not task_type:
        return "", f"unsupported captcha type '{kind}'"

    task: dict = {
        "type": task_type,
        "websiteURL": detected.get("url", ""),
        "websiteKey": detected.get("sitekey", ""),
    }
    if kind == "recaptchav3":
        task["pageAction"] = "submit"
    if kind == "turnstile":
        metadata = {}
        if detected.get("action"):
            metadata["action"] = detected["action"]
        if detected.get("cdata"):
            metadata["cdata"] = detected["cdata"]
        if metadata:
            task["metadata"] = metadata

    with httpx.Client(timeout=30) as client:
        resp = client.post(
            f"{_CAPSOLVER_BASE}/createTask",
            json={"clientKey": api_key, "task": task},
        )
        data = resp.json()
        if data.get("errorId"):
            return "", f"createTask failed: {data.get('errorDescription', 'unknown')}"
        task_id = data.get("taskId")
        if not task_id:
            return "", "createTask returned no taskId"

        # 10 polls x 3s = 30s, matching the budget the prompt documents.
        for _ in range(10):
            time.sleep(3)
            resp = client.post(
                f"{_CAPSOLVER_BASE}/getTaskResult",
                json={"clientKey": api_key, "taskId": task_id},
            )
            data = resp.json()
            if data.get("errorId"):
                return "", f"getTaskResult failed: {data.get('errorDescription', 'unknown')}"
            if data.get("status") == "ready":
                solution = data.get("solution", {})
                token = solution.get("gRecaptchaResponse") or solution.get("token", "")
                return (token, "") if token else ("", "solution had no token")

    return "", "timed out after 30s"


# ---------------------------------------------------------------------------
# Tool schemas
# ---------------------------------------------------------------------------

def _fn(name: str, description: str, properties: dict, required: list[str] | None = None) -> dict:
    return {
        "type": "function",
        "function": {
            "name": name,
            "description": description,
            "parameters": {
                "type": "object",
                "properties": properties,
                "required": required or [],
            },
        },
    }


TOOL_SCHEMAS: list[dict] = [
    _fn("browser_snapshot",
        "Accessibility snapshot of the current page. Returns element refs (e.g. e12) "
        "used by every other tool. Refs go stale after navigation - re-snapshot first.",
        {}),
    _fn("browser_navigate", "Navigate to a URL.",
        {"url": {"type": "string"}}, ["url"]),
    _fn("browser_navigate_back", "Go back to the previous page.", {}),
    _fn("browser_click",
        "Click an element by ref. Returns the outcome and flags any new tab.",
        {"ref": {"type": "string", "description": "Element ref from the latest snapshot"},
         "element": {"type": "string", "description": "Human description, for logging"}},
        ["ref"]),
    _fn("browser_type", "Type text into a single element by ref.",
        {"ref": {"type": "string"}, "text": {"type": "string"},
         "submit": {"type": "boolean", "description": "Press Enter afterwards"}},
        ["ref", "text"]),
    _fn("browser_fill_form",
        "Fill several fields in one call. Prefer this over repeated browser_type.",
        {"fields": {"type": "array", "items": {"type": "object", "properties": {
            "ref": {"type": "string"}, "value": {"type": "string"}}}}},
        ["fields"]),
    _fn("browser_select_option", "Choose an option in a select/combobox by ref.",
        {"ref": {"type": "string"}, "values": {"type": "array", "items": {"type": "string"}}},
        ["ref", "values"]),
    _fn("browser_file_upload", "Upload files. Omit ref to target the page's file input.",
        {"paths": {"type": "array", "items": {"type": "string"}},
         "ref": {"type": "string"}},
        ["paths"]),
    _fn("browser_tabs", "List, select, open or close browser tabs.",
        {"action": {"type": "string", "enum": ["list", "select", "new", "close"]},
         "index": {"type": "integer"}},
        ["action"]),
    _fn("browser_evaluate", "Run a JavaScript function in the page and return its result.",
        {"function": {"type": "string", "description": "e.g. () => document.title"},
         "ref": {"type": "string", "description": "Optional element to pass as the argument"}},
        ["function"]),
    _fn("browser_wait_for", "Wait for a number of seconds, or for text to appear/vanish.",
        {"time": {"type": "number"}, "text": {"type": "string"},
         "textGone": {"type": "string"}}),
    _fn("browser_take_screenshot", "Screenshot the page to a file (for your own inspection).", {}),
    _fn("solve_captcha",
        "Detect and solve any CAPTCHA on the page via CapSolver, then inject the token. "
        "Takes no arguments - detection, solving and injection are all handled for you. "
        "Call this whenever a CAPTCHA appears.",
        {}),
    _fn("report_result",
        "Report the final outcome and end the session. Call exactly once, when done.",
        {"status": {"type": "string",
                    "enum": ["APPLIED", "EXPIRED", "CAPTCHA", "LOGIN_ISSUE", "FAILED"]},
         "reason": {"type": "string",
                    "description": "Required for FAILED, e.g. not_eligible_location"}},
        ["status"]),
]

SNAPSHOT_TOOLS = frozenset({"browser_snapshot"})


# ---------------------------------------------------------------------------
# Tool implementations
# ---------------------------------------------------------------------------

class BrowserTools:
    """Executes tool calls against a live Playwright page.

    Args:
        context: A Playwright BrowserContext obtained via connect_over_cdp.
        dry_run: When True, clicks on submit-like elements are refused in
            Python. This is enforced here rather than in the prompt so a model
            that ignores instructions still cannot submit an application.
        screenshot_dir: Where browser_take_screenshot writes.
    """

    def __init__(self, context, dry_run: bool = False, screenshot_dir=None) -> None:
        self.context = context
        self.dry_run = dry_run
        self.screenshot_dir = screenshot_dir
        self._page = context.pages[0] if context.pages else context.new_page()
        self._page.set_default_timeout(ACTION_TIMEOUT_MS)

    # -- helpers ------------------------------------------------------------

    @property
    def page(self):
        """Current page, self-healing if the active tab was closed."""
        if self._page.is_closed():
            self._page = self.context.pages[-1] if self.context.pages else self.context.new_page()
            self._page.set_default_timeout(ACTION_TIMEOUT_MS)
        return self._page

    def _locator(self, ref: str):
        """Resolve a ref, failing fast if it is stale.

        locator.count() answers immediately, whereas letting a bad ref fall
        through to click()/fill() burns the full action timeout. Small models
        guess refs often enough that this is worth a round trip.
        """
        if not ref:
            raise StaleRef("(empty)")
        locator = self.page.locator(f"aria-ref={ref}")
        try:
            if locator.count() == 0:
                raise StaleRef(ref)
        except StaleRef:
            raise
        except Exception as exc:  # selector engine rejects unknown refs outright
            raise StaleRef(ref) from exc
        return locator

    def _adopt_new_tab(self) -> str:
        """Switch to a tab opened by the last action, if any."""
        pages = [p for p in self.context.pages if not p.is_closed()]
        if pages and pages[-1] is not self._page:
            self._page = pages[-1]
            self._page.set_default_timeout(ACTION_TIMEOUT_MS)
            return f" A new tab opened and is now active: {self._page.url}"
        return ""

    def _settle(self) -> None:
        """Best-effort wait for the page to stop moving after an action."""
        try:
            self.page.wait_for_load_state("domcontentloaded", timeout=5_000)
        except Exception:  # noqa: BLE001 - settling is advisory
            pass

    # -- dispatch -----------------------------------------------------------

    def dispatch(self, name: str, args: dict) -> str:
        """Run one tool call and return its result as text.

        Errors are returned as "Error: ..." strings rather than raised, so the
        model can recover (usually by re-snapshotting) instead of the run dying.
        ResultReported is the one exception - it terminates the loop.
        """
        handler = getattr(self, f"_t_{name}", None)
        if handler is None:
            return f"Error: unknown tool '{name}'."
        try:
            return handler(args)
        except ResultReported:
            raise
        except StaleRef as exc:
            return f"Error: {exc}"
        except Exception as exc:  # noqa: BLE001 - surfaced to the model as text
            msg = str(exc).split("\n")[0][:200]
            if "Timeout" in msg and args.get("ref"):
                return (f"Error: {msg} Element {args['ref']} exists but could not be "
                        f"used - it may be hidden, disabled or covered. "
                        f"Re-snapshot and try a different element.")
            return f"Error: {msg}"

    # -- tools --------------------------------------------------------------

    def _t_browser_snapshot(self, args: dict) -> str:
        return filter_snapshot(self.page.aria_snapshot(mode="ai"))

    def _t_browser_navigate(self, args: dict) -> str:
        url = args.get("url", "")
        if not url:
            return "Error: url is required."
        self.page.goto(url, timeout=NAV_TIMEOUT_MS, wait_until="domcontentloaded")
        return f"Navigated to {self.page.url}. Call browser_snapshot to see the page."

    def _t_browser_navigate_back(self, args: dict) -> str:
        self.page.go_back(timeout=NAV_TIMEOUT_MS)
        return f"Went back to {self.page.url}."

    def _t_browser_click(self, args: dict) -> str:
        ref = args.get("ref", "")
        if not ref:
            return "Error: ref is required."
        locator = self._locator(ref)

        if self.dry_run:
            try:
                label = " ".join(filter(None, [
                    locator.get_attribute("aria-label") or "",
                    locator.inner_text(timeout=2_000) or "",
                    locator.get_attribute("value") or "",
                ]))
            except Exception:  # noqa: BLE001 - label is only used for the guard
                label = args.get("element", "")
            if _SUBMIT_RE.search(label):
                return ("DRY RUN: submit blocked. The application was NOT sent. "
                        "Treat this as success and call report_result with status APPLIED.")

        locator.click(timeout=ACTION_TIMEOUT_MS)
        self._settle()
        return f"Clicked {ref}.{self._adopt_new_tab()} Re-snapshot to see the result."

    def _t_browser_type(self, args: dict) -> str:
        ref, text = args.get("ref", ""), args.get("text", "")
        locator = self._locator(ref)
        locator.fill(text, timeout=ACTION_TIMEOUT_MS)
        if args.get("submit"):
            locator.press("Enter")
            self._settle()
        return f"Typed into {ref}."

    def _t_browser_fill_form(self, args: dict) -> str:
        fields = args.get("fields") or []
        if not isinstance(fields, list):
            return "Error: fields must be a list of {ref, value} objects."
        filled, errors = 0, []
        for field in fields:
            if not isinstance(field, dict):
                continue
            ref, value = field.get("ref", ""), str(field.get("value", ""))
            try:
                self._locator(ref).fill(value, timeout=ACTION_TIMEOUT_MS)
                filled += 1
            except Exception as exc:  # noqa: BLE001 - report per-field, keep going
                errors.append(f"{ref}: {str(exc).splitlines()[0][:80]}")
        out = f"Filled {filled}/{len(fields)} fields."
        if errors:
            out += " Failed: " + "; ".join(errors[:5]) + " Re-snapshot for current refs."
        return out

    def _t_browser_select_option(self, args: dict) -> str:
        ref = args.get("ref", "")
        values = args.get("values") or []
        if isinstance(values, str):
            values = [values]
        locator = self._locator(ref)
        try:
            locator.select_option(values, timeout=ACTION_TIMEOUT_MS)
            return f"Selected {values} in {ref}."
        except Exception:  # noqa: BLE001 - fall back to custom widget handling
            # Many ATS use div-based comboboxes that select_option can't drive.
            locator.click(timeout=ACTION_TIMEOUT_MS)
            if values:
                self.page.get_by_text(values[0], exact=False).first.click(timeout=ACTION_TIMEOUT_MS)
            self._settle()
            return f"Selected {values} in {ref} via click (custom dropdown)."

    def _t_browser_file_upload(self, args: dict) -> str:
        paths = args.get("paths") or []
        if isinstance(paths, str):
            paths = [paths]
        missing = [p for p in paths if not os.path.exists(p)]
        if missing:
            return f"Error: file(s) not found: {missing}"

        ref = args.get("ref")
        if ref:
            try:
                self._locator(ref).set_input_files(paths, timeout=ACTION_TIMEOUT_MS)
                self._settle()
                return f"Uploaded {[os.path.basename(p) for p in paths]} to {ref}."
            except Exception as exc:  # noqa: BLE001 - fall through to the page-level input
                log.debug("set_input_files on ref %s failed: %s", ref, exc)

        # set_input_files works on hidden inputs, which is what most ATS use
        # behind a styled "Upload resume" button.
        inputs = self.page.locator('input[type="file"]')
        if inputs.count() == 0:
            return ("Error: no file input found. Click the upload button first, "
                    "then re-snapshot and retry.")
        inputs.last.set_input_files(paths, timeout=ACTION_TIMEOUT_MS)
        self._settle()
        return f"Uploaded {[os.path.basename(p) for p in paths]} to the page file input."

    def _t_browser_tabs(self, args: dict) -> str:
        action = args.get("action", "list")
        pages = [p for p in self.context.pages if not p.is_closed()]

        if action == "list":
            lines = [
                f"{i}{' (active)' if p is self._page else ''}: {p.url}"
                for i, p in enumerate(pages)
            ]
            return "Open tabs:\n" + "\n".join(lines)

        if action == "select":
            idx = int(args.get("index", 0))
            if not 0 <= idx < len(pages):
                return f"Error: no tab at index {idx}. {len(pages)} tabs open."
            self._page = pages[idx]
            self._page.bring_to_front()
            self._page.set_default_timeout(ACTION_TIMEOUT_MS)
            return f"Switched to tab {idx}: {self._page.url}"

        if action == "new":
            self._page = self.context.new_page()
            self._page.set_default_timeout(ACTION_TIMEOUT_MS)
            return "Opened a new tab."

        if action == "close":
            idx = int(args.get("index", pages.index(self._page)))
            if not 0 <= idx < len(pages):
                return f"Error: no tab at index {idx}."
            pages[idx].close()
            return f"Closed tab {idx}."

        return f"Error: unknown action '{action}'."

    def _t_browser_evaluate(self, args: dict) -> str:
        fn = args.get("function", "")
        if not fn:
            return "Error: function is required."
        ref = args.get("ref")
        result = self._locator(ref).evaluate(fn) if ref else self.page.evaluate(fn)
        try:
            text = json.dumps(result, default=str)
        except (TypeError, ValueError):
            text = str(result)
        return text[:2_000] if text else "undefined"

    def _t_browser_wait_for(self, args: dict) -> str:
        if args.get("text"):
            self.page.get_by_text(args["text"], exact=False).first.wait_for(timeout=30_000)
            return f"Text {args['text']!r} appeared."
        if args.get("textGone"):
            self.page.get_by_text(args["textGone"], exact=False).first.wait_for(
                state="hidden", timeout=30_000)
            return f"Text {args['textGone']!r} disappeared."
        seconds = min(float(args.get("time", 3)), 30)
        time.sleep(seconds)
        return f"Waited {seconds}s."

    def _t_browser_take_screenshot(self, args: dict) -> str:
        if not self.screenshot_dir:
            return "Error: screenshots are not configured for this run."
        path = self.screenshot_dir / f"shot-{int(time.time())}.png"
        self.page.screenshot(path=str(path))
        return f"Screenshot saved to {path}."

    def _t_solve_captcha(self, args: dict) -> str:
        api_key = os.environ.get("CAPSOLVER_API_KEY", "")
        detected = self.page.evaluate(_DETECT_JS)
        if not detected:
            return "No CAPTCHA detected on this page. Continue with the application."
        kind = detected.get("type", "")
        if kind == "turnstile_script_only":
            time.sleep(3)
            detected = self.page.evaluate(_DETECT_JS)
            if not detected or detected.get("type") == "turnstile_script_only":
                return "Turnstile script present but no widget rendered yet. Wait and retry."
            kind = detected.get("type", "")

        if not api_key:
            return (f"Detected {kind} but CAPSOLVER_API_KEY is not configured. "
                    f"Call report_result with status CAPTCHA.")

        token, error = _capsolver_solve(detected, api_key)
        if error:
            return (f"CapSolver could not solve the {kind}: {error}. "
                    f"Call report_result with status CAPTCHA.")

        self.page.evaluate(_INJECT_JS.get(kind, _INJECT_JS["recaptchav2"]), token)
        time.sleep(2)
        return (f"Solved and injected a {kind} token. Re-snapshot. If the widget is gone, "
                f"continue; some sites still need a Submit/Verify click.")

    def _t_report_result(self, args: dict) -> str:
        status = str(args.get("status", "")).upper().strip()
        valid = {"APPLIED", "EXPIRED", "CAPTCHA", "LOGIN_ISSUE", "FAILED"}
        if status not in valid:
            return f"Error: status must be one of {sorted(valid)}."
        reason = str(args.get("reason", "")).strip()
        if status == "FAILED" and not reason:
            reason = "unspecified"
        raise ResultReported(status, reason)
