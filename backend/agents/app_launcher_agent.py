import os
import json
import platform
import re
import subprocess
import urllib.parse

from playwright.sync_api import sync_playwright

from backend.core import llm

# Folder where the persistent Chrome profile (cookies, login sessions,
# history) is stored. Reusing the same real profile across runs is what
# stops Google/YouTube from flagging the session as a bot.
USER_DATA_DIR = os.path.join(os.path.expanduser("~"), ".jarvis_browser_profile")


# ---------------------------------------------------------------
# Real desktop applications (opened via the OS, not the browser)
# ---------------------------------------------------------------
APP_COMMANDS = {
    "chrome": {
        "win": "chrome",
        "mac": "Google Chrome",
        "linux": "google-chrome"
    },
    "notepad": {
        "win": "notepad",
        "mac": "TextEdit",
        "linux": "gedit"
    },
    "calculator": {
        "win": "calc",
        "mac": "Calculator",
        "linux": "gnome-calculator"
    },
    "vs code": {
        "win": "code",
        "mac": "Visual Studio Code",
        "linux": "code"
    },
    "vscode": {
        "win": "code",
        "mac": "Visual Studio Code",
        "linux": "code"
    }
}

# Well-known WEBSITE name -> URL shortcuts (opened inside the Playwright
# session, not as a separate desktop app). Anything not listed here still
# works — the LLM guesses/constructs a URL.
#
# NOTE: include common misspellings/variants here too (e.g. "linkdin"),
# since the LLM classifier passes the target through fairly literally and
# spoken/typo'd names should still resolve correctly.
SITE_ALIASES = {
    "openai": "https://openai.com",
    "chatgpt": "https://chat.openai.com",
    "google": "https://google.com",
    "youtube": "https://youtube.com",
    "gmail": "https://mail.google.com",
    "github": "https://github.com/",
    "linkedin": "https://www.linkedin.com/",
    "linkdin": "https://www.linkedin.com/",
    "linked in": "https://www.linkedin.com/",
}

# Visible browser window (not headless) — so you can watch each command
# execute live while testing.
HEADLESS = False

# Module-level persistent Playwright/context/page — created lazily on the
# first browser-related command, then reused for every command after that.
_playwright = None
_context = None
_page = None


# =================================================================
# COMPOUND COMMAND SPLITTING
# =================================================================
# The LLM classifier only ever returns ONE action. If the user chains
# multiple requests in one sentence (e.g. "open chrome and then open
# linkedin"), we split it into separate sub-commands here and run the
# classify+handle pipeline once per sub-command, in order.

_SPLIT_PATTERN = re.compile(
    r"\s*(?:,?\s*and then\s*|,?\s*then\s*|\s+phir\s+|\s+aur phir\s+|,\s*and\s*)\s*",
    re.IGNORECASE,
)


def _split_compound_command(text: str) -> list:
    """
    Splits a command like "open chrome and then open linkedin on chrome"
    into ["open chrome", "open linkedin on chrome"].

    Falls back to [text] (i.e. no split) if the pattern doesn't match
    anything, so single/simple commands behave exactly as before.
    """
    parts = _SPLIT_PATTERN.split(text)
    parts = [p.strip() for p in parts if p and p.strip()]
    return parts if parts else [text]


def _normalize_site_key(s: str) -> str:
    return re.sub(r"[^a-z0-9]", "", s.lower().strip())


def _resolve_site_url(target: str) -> str:
    """
    Fuzzy-resolves a spoken/typo'd site name against SITE_ALIASES.

    Tries, in order:
    1. Exact (case-insensitive) key match.
    2. Normalized match (ignoring spaces/punctuation), so "linkdin",
       "linked in", "LinkedIn" all resolve the same way.
    3. Falls back to treating `target` itself as a URL/domain.
    """
    raw = target.strip()
    lower = raw.lower()

    if lower in SITE_ALIASES:
        return SITE_ALIASES[lower]

    normalized_target = _normalize_site_key(raw)
    for key, url in SITE_ALIASES.items():
        if _normalize_site_key(key) == normalized_target:
            return url

    url = raw
    if not url.startswith("http"):
        url = "https://" + url
    return url


def _ensure_page():
    """
    Starts Playwright with a PERSISTENT Chrome profile (real Chrome, not
    bundled Chromium, with automation flags hidden) and reuses the same
    page for every subsequent browser/youtube command.

    Persistent profile = real cookies/login/history saved across runs,
    which is what stops Google/YouTube from flagging it as a bot and
    showing "are you a robot" checks.

    If the window was closed manually (or crashed) since the last command,
    the old _page/_context references are stale — this detects that and
    starts a fresh session instead of trying to use a dead page.
    """
    global _playwright, _context, _page

    if _page is not None:
        try:
            if not _page.is_closed():
                return _page
        except Exception:
            # is_closed() itself can raise if the underlying browser
            # process died -- treat that the same as "closed" and fall
            # through to starting a fresh session below.
            pass

    # Stale/closed session from before — clean it up if possible.
    try:
        if _context is not None:
            _context.close()
    except Exception:
        pass
    try:
        if _playwright is not None:
            _playwright.stop()
    except Exception:
        pass

    _playwright = sync_playwright().start()
    _context = _playwright.chromium.launch_persistent_context(
        USER_DATA_DIR,
        headless=HEADLESS,
        channel="chrome",  # use real installed Chrome, not bundled Chromium
        args=[
            "--disable-blink-features=AutomationControlled",
            # Stability flags to reduce YouTube's "Something went wrong,
            # refresh or try again later" playback crash, which happens
            # more often on automation-controlled Chrome sessions.
            "--disable-features=CalculateNativeWinOcclusion",
            "--autoplay-policy=no-user-gesture-required",
            "--disable-background-timer-throttling",
            "--disable-backgrounding-occluded-windows",
            "--disable-renderer-backgrounding",
        ],
    )
    _page = _context.pages[0] if _context.pages else _context.new_page()

    return _page


def _reset_browser_session():
    """
    Forcibly drops the current playwright/context/page references so the
    NEXT _ensure_page() call starts a completely fresh session. Used
    when a page.goto()/evaluate() call fails mid-flight with a "closed"
    error even though _ensure_page() thought the page was alive.
    """
    global _playwright, _context, _page
    try:
        if _context is not None:
            _context.close()
    except Exception:
        pass
    try:
        if _playwright is not None:
            _playwright.stop()
    except Exception:
        pass
    _context = None
    _page = None
    _playwright = None


def _open_desktop_app(entry: dict) -> bool:
    """
    Opens a real installed desktop application (not a website).
    """
    system = platform.system()

    try:
        if system == "Windows":
            os.startfile(entry.get("win"))
        elif system == "Darwin":
            subprocess.Popen(["open", "-a", entry.get("mac")])
        else:
            subprocess.Popen([entry.get("linux")])
        return True
    except Exception as e:
        print("Launch error:", e)
        return False


# =================================================================
# DETERMINISTIC PRE-CLASSIFICATION (bypasses the LLM entirely)
# =================================================================
# WHY THIS EXISTS: the LLM classifier was flip-flopping on commands like
# "open linkedin" / "open github" -- sometimes correctly returning
# open_site, sometimes hallucinating open_app (which then fails with
# "I don't know how to open linkedin" since it's not a desktop app).
# Same for "search X on google chrome" sometimes coming back as
# open_site with the whole search URL as the "site". This is inherent
# LLM non-determinism -- there's no reason to ask a model to guess
# something we can already look up exactly in APP_COMMANDS/SITE_ALIASES.
# So: for the common, unambiguous "open <name>" / "search <query>"
# shapes, resolve directly against our own known dictionaries first and
# skip the LLM call entirely. Only genuinely ambiguous/free-form
# commands (youtube playback, click first result, go back, etc.) still
# go through _llm_classify.

_OPEN_VERB_PATTERN = re.compile(
    r"^\s*(?:open|launch|start|kholo|khol\s+do|khol\s+de)\s+(.+?)\s*$",
    re.IGNORECASE,
)
_SEARCH_VERB_PATTERN = re.compile(
    r"^\s*search\s+(?:for\s+)?(.+?)(?:\s+on\s+(?:google|chrome|the\s+browser))?\s*$",
    re.IGNORECASE,
)


def _match_known_app(name: str):
    """Returns the exact APP_COMMANDS key if `name` matches one, else None."""
    normalized = _normalize_site_key(name)
    for key in APP_COMMANDS:
        if _normalize_site_key(key) == normalized:
            return key
    return None


def _match_known_site(name: str):
    """Returns True if `name` matches a known SITE_ALIASES entry (fuzzy)."""
    normalized = _normalize_site_key(name)
    for key in SITE_ALIASES:
        if _normalize_site_key(key) == normalized:
            return True
    return False


def _deterministic_classify(text: str):
    """
    Tries to resolve `text` directly against known apps/sites without
    calling the LLM. Returns a data dict (same shape _llm_classify
    returns) or None if this command doesn't clearly match one of the
    deterministic shapes -- in which case the caller falls back to
    _llm_classify as before.
    """
    stripped = text.strip()

    open_match = _OPEN_VERB_PATTERN.match(stripped)
    if open_match:
        target_raw = open_match.group(1).strip()

        app_key = _match_known_app(target_raw)
        if app_key:
            return {"action": "open_app", "target": app_key}

        if _match_known_site(target_raw):
            return {"action": "open_site", "target": target_raw}

        # Not a known app or known site -- ambiguous (could be a new
        # desktop app name, or a URL/domain to open) -- let the LLM
        # decide as before.
        return None

    search_match = _SEARCH_VERB_PATTERN.match(stripped)
    if search_match:
        query = search_match.group(1).strip()
        if query:
            return {"action": "search", "query": query}

    return None


# =================================================================
# LLM CLASSIFICATION — decides intent from natural language
# =================================================================

def _llm_classify(text: str) -> dict:
    """
    Turns a natural language command into a structured action.
    Handles English / Urdu / Roman Urdu / mixed phrasing.
    """

    prompt = f"""
You control app launching AND a web browser (including YouTube playback)
for a personal assistant, based on natural language commands. Commands may
be in English, Urdu, Roman Urdu, or mixed.

Known DESKTOP apps: {list(APP_COMMANDS.keys())}
Known WEBSITE shortcuts: {list(SITE_ALIASES.keys())}

Decide exactly one action:

DESKTOP APP:
- "open_app": open a real desktop application from the known list above.
  (e.g. "open chrome app", "notepad kholo", "launch calculator")

GENERAL BROWSER:
- "open_site": open a specific website in the browser (not youtube playback).
  If it matches a known website shortcut above, set "target" to that exact
  name. Otherwise set "target" to your best-guess full URL (include https://).
  Minor misspellings of a known shortcut (e.g. "linkdin" for "linkedin")
  should still be treated as that shortcut.
  (e.g. "open openai" -> target "openai")
- "search": search something on Google.
  (e.g. "search ai news", "chrome pe AI news search karo")
- "click_first_result": click the first/top result on the current search
  results page. (e.g. "open first result", "pehla result kholo")
- "go_back": go back to the previous page. (e.g. "go back", "peechay jao")
- "refresh": reload the current page. (e.g. "refresh page", "reload karo")

YOUTUBE PLAYBACK:
- "yt_play": search and play a video/song on YouTube.
  (e.g. "taylor swift ka new gana chalao", "play lofi music")
- "yt_pause": pause the currently playing YouTube video. ("pause karo")
- "yt_resume": resume the currently paused YouTube video. ("resume karo")
- "yt_volume_up": increase YouTube volume. ("volume badhao")
- "yt_volume_down": decrease YouTube volume. ("volume kam karo")
- "yt_stop": stop/close the currently playing video. ("band karo")
- "yt_next": skip to the next video/song. ("next gana", "skip karo")

BROWSER WINDOW:
- "close_browser": close the entire Chrome browser window (whatever site
  or tab is currently open in it — LinkedIn, GitHub, YouTube, etc, it all
  shares one window, so "close chrome", "close the browser", "close
  linkedin", "close youtube", "close this tab", "band kardo chrome",
  "isko band karo" (when referring to the browser) all mean this.
  (e.g. "close chrome", "close the browser", "band kardo")

- "none": the command doesn't match any of the above.

Note: if the user just says "open chrome" with no other intent, treat it
as "open_app" (they want the real Chrome application). Only use "search",
"yt_play" etc. when they clearly want a search/browser/youtube action —
mentioning "chrome" as the place to search in does NOT mean "open_app".

This command is a single, already-isolated instruction (any "and then" /
compound phrasing has already been split out before reaching you) — treat
it as one self-contained request.

User command: "{text}"

Return ONLY JSON, no other text. Use one of these exact shapes:
{{"action": "open_app", "target": "<exact app name from the list above>"}}
{{"action": "open_site", "target": "<site name or full url>"}}
{{"action": "search", "query": "<search query>"}}
{{"action": "click_first_result"}}
{{"action": "go_back"}}
{{"action": "refresh"}}
{{"action": "yt_play", "query": "<what to play on youtube>"}}
{{"action": "yt_pause"}}
{{"action": "yt_resume"}}
{{"action": "yt_volume_up"}}
{{"action": "yt_volume_down"}}
{{"action": "yt_stop"}}
{{"action": "yt_next"}}
{{"action": "close_browser"}}
{{"action": "none"}}
"""

    response = llm.chat(
        [{"role": "user", "content": prompt}],
        max_tokens=80,
    )

    try:
        return json.loads(response)
    except Exception as e:
        print("Classify parse error:", e)
        return {"action": "none"}


# =================================================================
# DESKTOP APP HANDLER
# =================================================================

def _handle_open_app(state, target: str):
    key = target.lower().strip()
    entry = APP_COMMANDS.get(key)

    if not entry:
        state["response"] = f"I don't know how to open {target}."
        return state

    success = _open_desktop_app(entry)
    state["response"] = (
        f"{target.title()} opened successfully."
        if success
        else f"Unable to open {target.title()}."
    )
    return state


# =================================================================
# GENERAL BROWSER HANDLERS
# =================================================================

def _handle_open_site(state, target: str):
    try:
        page = _ensure_page()
    except Exception as e:
        print("Ensure page error (open_site):", e)
        state["response"] = f"Couldn't open the browser to open {target}."
        return state

    url = _resolve_site_url(target)

    try:
        page.goto(url, wait_until="domcontentloaded")
        state["response"] = f"Opened {target}."
    except Exception as e:
        print("Open site error:", e)
        if "closed" in str(e).lower():
            # Browser died mid-flight -- reset and try once more with a
            # brand-new session before giving up.
            _reset_browser_session()
            try:
                page = _ensure_page()
                page.goto(url, wait_until="domcontentloaded")
                state["response"] = f"Opened {target}."
                return state
            except Exception as e2:
                print("Open site retry error:", e2)
        state["response"] = f"Couldn't open {target}."

    return state


def _handle_search(state, query: str):
    try:
        page = _ensure_page()
    except Exception as e:
        print("Ensure page error (search):", e)
        state["response"] = f"Couldn't open the browser to search for {query}."
        return state

    search_url = "https://www.google.com/search?q=" + urllib.parse.quote(query)

    try:
        page.goto(search_url, wait_until="domcontentloaded")
        state["response"] = f"Searched for {query}."
    except Exception as e:
        print("Search error:", e)
        state["response"] = f"Couldn't search for {query}."

    return state


def _handle_click_first_result(state):
    try:
        page = _ensure_page()
    except Exception as e:
        print("Ensure page error (click_first_result):", e)
        state["response"] = "Couldn't open the browser."
        return state

    try:
        first_link = page.locator("div#search a").first
        first_link.click()
        page.wait_for_load_state("domcontentloaded")
        state["response"] = "Opened the first result."
    except Exception as e:
        print("Click first result error:", e)
        state["response"] = "Couldn't find a result to click — try searching first."

    return state


def _handle_go_back(state):
    try:
        page = _ensure_page()
    except Exception as e:
        print("Ensure page error (go_back):", e)
        state["response"] = "Couldn't open the browser."
        return state

    try:
        page.go_back(wait_until="domcontentloaded")
        state["response"] = "Went back."
    except Exception as e:
        print("Go back error:", e)
        state["response"] = "Couldn't go back."

    return state


def _handle_refresh(state):
    try:
        page = _ensure_page()
    except Exception as e:
        print("Ensure page error (refresh):", e)
        state["response"] = "Couldn't open the browser."
        return state

    try:
        page.reload(wait_until="domcontentloaded")
        state["response"] = "Refreshed the page."
    except Exception as e:
        print("Refresh error:", e)
        state["response"] = "Couldn't refresh the page."

    return state


# =================================================================
# YOUTUBE PLAYBACK HANDLERS
# All use direct JS on the <video> element for reliability, instead of
# keyboard shortcuts (which depend on which element currently has focus).
# =================================================================

def _handle_yt_play(state, query: str):
    try:
        page = _ensure_page()
    except Exception as e:
        print("Ensure page error (yt_play):", e)
        state["response"] = f"Couldn't open the browser to play {query}."
        return state

    search_url = "https://www.youtube.com/results?search_query=" + urllib.parse.quote(query)

    try:
        page.goto(search_url, wait_until="domcontentloaded")
        first_video = page.locator("ytd-video-renderer a#video-title").first
        first_video.click()
        page.wait_for_load_state("domcontentloaded")
        page.wait_for_selector("video", timeout=8000)
        state["response"] = f"Playing {query} on YouTube."
    except Exception as e:
        print("YT play error:", e)
        state["response"] = f"Couldn't auto-play, showing search results for {query}."

    return state


def _handle_yt_pause(state):
    try:
        page = _ensure_page()
    except Exception as e:
        print("Ensure page error (yt_pause):", e)
        state["response"] = "Couldn't open the browser."
        return state
    try:
        page.evaluate("document.querySelector('video')?.pause()")
        state["response"] = "Paused."
    except Exception as e:
        print("YT pause error:", e)
        state["response"] = "Couldn't pause — is a video open?"
    return state


def _handle_yt_resume(state):
    try:
        page = _ensure_page()
    except Exception as e:
        print("Ensure page error (yt_resume):", e)
        state["response"] = "Couldn't open the browser."
        return state
    try:
        page.evaluate("document.querySelector('video')?.play()")
        state["response"] = "Resumed."
    except Exception as e:
        print("YT resume error:", e)
        state["response"] = "Couldn't resume — is a video open?"
    return state


def _handle_yt_volume(state, direction: str):
    try:
        page = _ensure_page()
    except Exception as e:
        print("Ensure page error (yt_volume):", e)
        state["response"] = "Couldn't open the browser."
        return state
    try:
        js = """(dir) => {
            const p = document.querySelector('#movie_player');
            if (!p || !p.setVolume) return 'no_player';
            if (p.isMuted && p.isMuted()) p.unMute();
            const current = p.getVolume();
            const newVol = dir === 'up'
                ? Math.min(100, current + 10)
                : Math.max(0, current - 10);
            p.setVolume(newVol);
            return newVol;
        }"""
        result = page.evaluate(js, direction)
        print("YT volume debug ->", result)

        if result == "no_player":
            state["response"] = "Couldn't find the YouTube player — is a video open?"
        else:
            state["response"] = f"Volume set to {result}%."
    except Exception as e:
        print("YT volume error:", e)
        state["response"] = "Couldn't change volume — is a video open?"
    return state


def _handle_yt_stop(state):
    try:
        page = _ensure_page()
    except Exception as e:
        print("Ensure page error (yt_stop):", e)
        state["response"] = "Couldn't open the browser."
        return state
    try:
        page.evaluate(
            "const v = document.querySelector('video'); "
            "if (v) { v.pause(); v.currentTime = 0; }"
        )
        state["response"] = "Stopped."
    except Exception as e:
        print("YT stop error:", e)
        state["response"] = "Couldn't stop — is a video open?"
    return state


def _handle_yt_next(state):
    try:
        page = _ensure_page()
    except Exception as e:
        print("Ensure page error (yt_next):", e)
        state["response"] = "Couldn't open the browser."
        return state
    try:
        next_button = page.locator(".ytp-next-button").first
        next_button.click()
        page.wait_for_load_state("domcontentloaded")
        state["response"] = "Playing next."
    except Exception as e:
        print("YT next error:", e)
        state["response"] = "Couldn't skip — no next video available right now."
    return state


def _handle_close_browser(state):
    """
    Closes the entire persistent Chrome window (context + playwright),
    and resets the module-level references so the NEXT browser command
    (open_site, search, yt_play, etc.) transparently starts a fresh
    session via _ensure_page().
    """
    global _playwright, _context, _page

    had_open_page = _page is not None and not _page.is_closed()

    try:
        if _context is not None:
            _context.close()
    except Exception as e:
        print("Close browser (context) error:", e)

    try:
        if _playwright is not None:
            _playwright.stop()
    except Exception as e:
        print("Close browser (playwright) error:", e)

    _context = None
    _page = None
    _playwright = None

    state["response"] = (
        "Closed Chrome." if had_open_page else "Chrome wasn't open."
    )
    return state


# =================================================================
# SINGLE SUB-COMMAND DISPATCH
# =================================================================

def _dispatch_single_command(state, text: str) -> str:
    """
    Classifies and executes ONE sub-command, returns just the response
    text (doesn't touch history — the caller does that once at the end).
    """
    data = _deterministic_classify(text)
    if data is None:
        data = _llm_classify(text)
    action = data.get("action")

    if action == "open_app":
        state = _handle_open_app(state, data.get("target", ""))
    elif action == "open_site":
        state = _handle_open_site(state, data.get("target", ""))
    elif action == "search":
        state = _handle_search(state, data.get("query", ""))
    elif action == "click_first_result":
        state = _handle_click_first_result(state)
    elif action == "go_back":
        state = _handle_go_back(state)
    elif action == "refresh":
        state = _handle_refresh(state)
    elif action == "yt_play":
        state = _handle_yt_play(state, data.get("query", ""))
    elif action == "yt_pause":
        state = _handle_yt_pause(state)
    elif action == "yt_resume":
        state = _handle_yt_resume(state)
    elif action == "yt_volume_up":
        state = _handle_yt_volume(state, "up")
    elif action == "yt_volume_down":
        state = _handle_yt_volume(state, "down")
    elif action == "yt_stop":
        state = _handle_yt_stop(state)
    elif action == "yt_next":
        state = _handle_yt_next(state)
    elif action == "close_browser":
        state = _handle_close_browser(state)
    else:
        state["response"] = (
            f"I'm not sure what you want me to do with: '{text}'."
        )

    return state["response"]


# =================================================================
# MAIN NODE
# =================================================================

def app_launcher_node(state):
    """
    CHANGED (fix): wrapped in try/except, matching the safety-net
    pattern used by file_agent_node. Previously, if _ensure_page()
    threw (e.g. the persistent Chrome profile was locked by a leftover
    chrome.exe process, or real Chrome failed to launch), the exception
    propagated all the way up uncaught, so the node never returned a
    response at all -- which looked exactly like "nothing happens" when
    trying to open chrome/youtube/linkedin/github.
    """
    text = state["user_input"]

    try:
        sub_commands = _split_compound_command(text)

        responses = []
        for sub_text in sub_commands:
            reply = _dispatch_single_command(state, sub_text)
            responses.append(reply)

        # Combine responses from each sub-command into one natural reply.
        state["response"] = " ".join(responses)
    except Exception as e:
        print("app_launcher_node crashed:", e)
        state["response"] = (
            "Sorry, I couldn't do that -- something went wrong opening "
            "the browser or app. Try again."
        )

    state["history"].append({"role": "user", "content": text})
    state["history"].append({"role": "assistant", "content": state["response"]})

    return state


def close_browser():
    """
    Call this on app shutdown to cleanly close the persistent browser
    session (optional — not required for the agent to keep working).
    """
    global _playwright, _context, _page

    if _context is not None:
        _context.close()
    if _playwright is not None:
        _playwright.stop()

    _context = None
    _page = None
    _playwright = None

# ==========================================================
# INTERACTIVE TEST
# ==========================================================

if __name__ == "__main__":

    print("JARVIS App Launcher Test")
    print("Type 'exit' to stop.\n")

    while True:

        command = input("YOU: ").strip()

        if command.lower() == "exit":
            break

        state = {
            "user_input": command,
            "response": "",
            "history": []
        }

        result = app_launcher_node(state)

        print("JARVIS:", result["response"])