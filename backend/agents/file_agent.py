import json
import os
import platform
import re
import shutil
import subprocess
import time
from concurrent.futures import ThreadPoolExecutor, TimeoutError as _FutureTimeoutError

from send2trash import send2trash

try:
    import psutil
except ImportError:
    psutil = None  # close_file / auto-close fallback will be disabled if not installed

# win32gui is used (when available) for the more reliable window-title-based
# open/close tracking described above. Both optional so the rest of the
# agent still works fine without them / on other OSes.
try:
    import win32gui
    import win32process
    import win32con
except ImportError:
    win32gui = None
    win32process = None
    win32con = None

from backend.core import llm


HOME = os.path.expanduser("~")

# Common folder shortcuts, so the user can say "open downloads" instead of
# a full path. Add more if needed (e.g. "pictures", "music").
KNOWN_FOLDERS = {
    "downloads": os.path.join(HOME, "Downloads"),
    "desktop": os.path.join(HOME, "Desktop"),
    "documents": os.path.join(HOME, "Documents"),
}

# Folders to search through when the user asks to "find" a file.
SEARCH_FOLDERS = list(KNOWN_FOLDERS.values())

# How long (seconds) to wait / poll for the app that actually opened the
# file to show up (as a tracked window, or in the process list), so we
# can track it for later closing.
_TRACK_TIMEOUT = 4
_TRACK_POLL_INTERVAL = 0.4


# =================================================================
# CLOSE-INTENT PRE-CHECK (bypasses the LLM classifier entirely)
# =================================================================
# WHY THIS EXISTS:
# The LLM classifier's few-shot examples for "open_file" look like
# "<verb> the file named X" (verb + filename + "file"), and its examples
# for "close_file" are generic ("close this", "ye file band karo") with
# no filename attached. A command like "close agency services file"
# matches the *open_file* shape almost exactly (verb + filename + "file"),
# so the model would sometimes misclassify it as open_file with
# name="agency services" -- which silently RE-OPENS the very file the
# user was trying to close (since open_file first closes whatever's
# tracked, then reopens the new match).
#
# close_file never takes a name/parameter -- it always acts on whichever
# file is currently tracked as open, regardless of what the user names
# in their sentence. So instead of hoping the LLM gets the disambiguation
# right, we short-circuit: if the command contains a close-intent verb
# at all, route straight to close_file and skip the LLM call entirely.
_CLOSE_INTENT_PATTERN = re.compile(
    r"\b(close|band|bnd)\b",
    re.IGNORECASE,
)


def _is_close_intent(text: str) -> bool:
    """
    Returns True if the command contains a close-intent verb (English
    "close", Roman Urdu "band"/"bnd"). There is no other agent action
    that uses these words, so this is a safe, unambiguous shortcut that
    avoids relying on the LLM to disambiguate "close X file" from
    "open the file named X".
    """
    return bool(_CLOSE_INTENT_PATTERN.search(text))


# -----------------------------------------------------------------
# COMPOUND "copy + new folder + paste (+ show)" INTENT
# -----------------------------------------------------------------
# WHY THIS EXISTS: when a command chains several verbs together --
# "Copy the agency services file, create a new folder called Important
# Documents and paste it there and show me" -- giving the classifier
# the full menu of 9 actions to choose from made it repeatedly latch
# onto just ONE verb in the sentence (copy_file, or create_folder) and
# silently drop the rest, forcing the user to re-explain across several
# turns even though the very first command already had everything
# needed. So: detect this pattern with a cheap heuristic BEFORE calling
# the general classifier, and if detected, route straight to a
# dedicated, narrowly-scoped extraction call (see
# _llm_classify_compound) that only has to fill in fields for ONE known
# action shape instead of picking between many.
_COPY_OR_PASTE_PATTERN = re.compile(r"\b(copy|paste)\b", re.IGNORECASE)
_FOLDER_CREATION_PATTERN = re.compile(
    r"(\b(new|create|naya|nayi)\b[^.]{0,25}\bfolder\b)"
    r"|(\bfolder\b[^.]{0,25}\b(call|called|name|named)\b)"
    r"|(\bfolder\b[^.]{0,15}\b(banao|bnao|bana)\b)",
    re.IGNORECASE,
)


def _is_compound_copy_paste_intent(text: str) -> bool:
    """
    True when the command mentions copy/paste AND separately describes
    creating/naming a folder -- the signature of the "copy this file
    into a new folder" combo command. A plain "paste X in the Notes
    folder" (destination already exists, nothing being created) won't
    match, since it lacks the folder-creation phrasing -- that keeps
    going through the normal single-purpose paste_file path.
    """
    return bool(_COPY_OR_PASTE_PATTERN.search(text)) and bool(
        _FOLDER_CREATION_PATTERN.search(text)
    )


# -----------------------------------------------------------------
# "AND SHOW ME" INTENT
# -----------------------------------------------------------------
# A lot of commands end with "...and show me" / "...dikhao" as a
# tacked-on request to open the resulting folder in File Explorer
# afterward. Detected once, independently of whichever action ends up
# running, and passed through as data["show"] so any handler that
# lands somewhere (paste_file, create_folder, etc.) can act on it
# instead of only the compound action supporting it.
_SHOW_INTENT_PATTERN = re.compile(
    r"\b(show|dikhao|dikha|dekha|dekho)\b",
    re.IGNORECASE,
)


def _is_show_intent(text: str) -> bool:
    return bool(_SHOW_INTENT_PATTERN.search(text))


# =================================================================
# CURRENTLY-OPEN-FILE TRACKING
# =================================================================
# IMPORTANT: this is deliberately kept as MODULE-LEVEL globals, not
# inside the LangGraph `state` dict. `state` gets rebuilt/merged between
# graph turns depending on how the graph is wired, so custom keys like
# "_open_file_path" aren't guaranteed to survive from one turn to the
# next. Plain module globals live in process memory for as long as the
# backend is running, so "open X" now, "close it" three turns later
# always works — same pattern already used in app_launcher_agent.py for
# tracking the browser page/context.

_open_file_pid = None
_open_file_hwnd = None
_open_file_path = None

# Same module-global pattern as the open/close tracking above -- this is
# our "clipboard": the path of the last file the user said to copy, so
# a later "paste it in X" (possibly several turns later) still knows
# what to paste.
_clipboard_file_path = None

# Path of the folder most recently created via "create_folder". Used as
# a fallback destination for paste_file when the user doesn't repeat the
# folder name (e.g. "paste it" right after "create a folder called X"),
# so paste_file isn't stuck asking "which folder?" when it's obvious
# from context.
_last_created_folder = None


# =================================================================
# PATH HELPERS
# =================================================================

def _resolve_path(name_or_path: str, default_folder: str = None) -> str:
    """
    If the user gave a full path, use it as-is. If they just gave a
    filename, resolve it relative to default_folder (Downloads by default),
    since that's where most files end up.
    """
    if os.path.isabs(name_or_path) or os.path.sep in name_or_path:
        return name_or_path

    base = default_folder or KNOWN_FOLDERS["downloads"]
    return os.path.join(base, name_or_path)


def _open_in_file_explorer(path: str) -> bool:
    system = platform.system()
    try:
        if system == "Windows":
            os.startfile(path)
        elif system == "Darwin":
            subprocess.Popen(["open", path])
        else:
            subprocess.Popen(["xdg-open", path])
        return True
    except Exception as e:
        print("Open folder error:", e)
        return False


# =================================================================
# WINDOW-TITLE TRACKING (preferred way to track what we just opened)
# =================================================================

def _snapshot_visible_hwnds():
    """
    Returns the set of currently visible, titled top-level window handles.
    Used as a "before" snapshot so that after opening a file we only
    consider brand-new windows, not something that was already open.
    Empty set if pywin32 isn't installed.
    """
    if win32gui is None:
        return set()

    hwnds = set()

    def _callback(hwnd, _):
        if win32gui.IsWindowVisible(hwnd) and win32gui.GetWindowText(hwnd):
            hwnds.add(hwnd)
        return True

    try:
        win32gui.EnumWindows(_callback, None)
    except Exception:
        pass
    return hwnds


def _find_new_window_by_title(name_hint: str, before_hwnds: set):
    """
    Polls for a NEW visible window (i.e. not in before_hwnds) whose title
    contains name_hint (case-insensitive substring match). This is much
    more reliable than scanning open file handles, since many viewers
    (PDF readers, media players, image viewers) release the file handle
    right after loading — but their window title still shows the
    filename. Returns an hwnd, or None if not found in time / pywin32
    isn't installed.
    """
    if win32gui is None:
        return None

    name_hint_lower = name_hint.lower()
    deadline = time.time() + _TRACK_TIMEOUT

    while time.time() < deadline:
        found = []

        def _callback(hwnd, _):
            if hwnd in before_hwnds:
                return True
            if not win32gui.IsWindowVisible(hwnd):
                return True
            title = win32gui.GetWindowText(hwnd)
            if not title:
                return True
            if name_hint_lower in title.lower():
                found.append(hwnd)
            return True

        try:
            win32gui.EnumWindows(_callback, None)
        except Exception:
            pass
        if found:
            return found[0]

        time.sleep(_TRACK_POLL_INTERVAL)

    return None


# =================================================================
# PROCESS TRACKING (fallback for when window-title tracking can't be
# used — non-Windows, or pywin32 not installed / window not found)
# =================================================================

def _find_process_for_file(path: str):
    """
    Best-effort: poll running processes for a short window right after we
    launch `path` with the OS default app, looking for one that has this
    exact file open. Returns a psutil.Process or None if not found (either
    psutil isn't installed, or the app didn't show up in time / doesn't
    expose open file handles, e.g. some sandboxed apps, or apps that
    release the file handle after loading it).
    """
    if psutil is None:
        return None

    norm_target = os.path.normcase(os.path.abspath(path))
    deadline = time.time() + _TRACK_TIMEOUT

    while time.time() < deadline:
        for proc in psutil.process_iter(["pid", "name"]):
            try:
                for f in proc.open_files():
                    if os.path.normcase(f.path) == norm_target:
                        return proc
            except (psutil.AccessDenied, psutil.NoSuchProcess, psutil.ZombieProcess):
                continue
        time.sleep(_TRACK_POLL_INTERVAL)

    return None


def _close_tracked_file() -> bool:
    """
    Closes whatever file we're currently tracking as "open" (if any).
    Prefers sending WM_CLOSE to the tracked window (graceful, only closes
    that window/tab rather than killing a whole shared process like a
    browser). Falls back to terminating the tracked process if we only
    have a PID (no window handle was captured). Returns True if something
    was actually closed. Silently no-ops if nothing is tracked.
    """
    global _open_file_pid, _open_file_hwnd, _open_file_path

    pid = _open_file_pid
    hwnd = _open_file_hwnd
    path = _open_file_path

    _open_file_pid = None
    _open_file_hwnd = None
    _open_file_path = None

    if not path:
        return False

    # Preferred: ask the window nicely to close itself.
    if hwnd and win32gui is not None:
        try:
            win32gui.PostMessage(hwnd, win32con.WM_CLOSE, 0, 0)
            return True
        except Exception as e:
            print("Close tracked window error:", e)
            # fall through to process-based close below

    # Fallback: terminate the tracked process directly.
    if not pid or psutil is None:
        return False

    try:
        proc = psutil.Process(pid)
        proc.terminate()
        try:
            proc.wait(timeout=3)
        except psutil.TimeoutExpired:
            proc.kill()
        return True
    except psutil.NoSuchProcess:
        return False
    except Exception as e:
        print("Close tracked file error:", e)
        return False


# =================================================================
# LLM CLASSIFICATION
# =================================================================

# How long we'll wait for a single llm.chat() call before giving up.
# WHY THIS EXISTS: "no response at all" (as opposed to an error message)
# almost always means a blocking network call stalled and never
# returned. Without a hard timeout, a single slow/stuck LLM API call
# hangs this entire request forever -- the voice loop just goes silent,
# like it did here. Failing fast lets the assistant say something went
# wrong and stay responsive for the next command instead.
_LLM_CALL_TIMEOUT_SECONDS = 25


def _call_llm_with_timeout(prompt: str, max_tokens: int) -> str:
    """
    Runs llm.chat() on a worker thread and enforces a hard wall-clock
    timeout, since llm.chat() itself has no timeout parameter we can
    rely on. Raises TimeoutError if it doesn't return in time -- note
    the underlying stuck network call may still be running in the
    background thread, but the caller gets control back immediately
    instead of hanging indefinitely.
    """
    with ThreadPoolExecutor(max_workers=1) as executor:
        future = executor.submit(
            llm.chat,
            [{"role": "user", "content": prompt}],
            max_tokens=max_tokens,
        )
        try:
            return future.result(timeout=_LLM_CALL_TIMEOUT_SECONDS)
        except _FutureTimeoutError:
            raise TimeoutError(
                f"LLM call exceeded {_LLM_CALL_TIMEOUT_SECONDS}s timeout"
            )


def _llm_classify(text: str) -> dict:
    """
    Turns a natural language file-management command into a structured
    action. Handles English / Urdu / Roman Urdu / mixed phrasing.

    NOTE: "close_file" intent is normally caught earlier by
    _is_close_intent() in file_agent_node(), before this function is even
    called, so this classifier rarely has to make that call itself. The
    close_file examples/rule below are kept as a backup for robustness in
    case this function is ever invoked directly from elsewhere.
    """

    prompt = f"""
You control file management for a personal assistant, based on natural
language commands. Commands may be in English, Urdu, Roman Urdu, or mixed.

Known folder shortcuts: {list(KNOWN_FOLDERS.keys())}

Decide exactly one action:
- "create_folder": create a new folder.
  (e.g. "create a folder called Notes", "Notes naam ka folder banao")
- "rename": rename a file.
  (e.g. "rename report.pdf to final_report.pdf")
- "delete": delete a file (goes to Recycle Bin, not permanent).
  (e.g. "delete old_notes.txt", "ye file delete kardo")
- "move": move a file to a different folder.
  (e.g. "move invoice.pdf to Documents")
- "open_folder": open a known folder in File Explorer.
  (e.g. "open downloads", "desktop kholo")
- "open_file": open a specific FILE by name (not a folder) using its
  default app. The name doesn't need to be exact/full — a partial name is
  fine, matching is fuzzy. Opening a new file automatically closes
  whichever file was previously opened this way. IMPORTANT: only use this
  action if the command's verb means "open" (open/kholo) -- never use it
  just because a filename is mentioned.
  (e.g. "open the file named BotifyHub Services in downloads",
  "resume.pdf kholo")
- "close_file": close whichever file was last opened via "open_file"
  (regardless of whether it's a PDF, doc, image, video, etc.). This
  action takes NO name/parameter -- it always acts on whatever is
  currently tracked as open. If the command's verb means "close"
  (close/band/bnd), ALWAYS choose close_file, even if the command also
  names a specific file (e.g. "close agency services file" is still
  close_file, NOT open_file -- the filename is just the user confirming
  what they think is open, not a request to open it).
  (e.g. "close this", "close the file", "isko band kardo",
  "ye file band karo", "close agency services file",
  "agency services wali pdf band kro")
- "open_copy_to_new_folder": ONE compound command that does everything
  in one go -- open a file, copy it, create a brand-new folder with a
  given name, paste the copy into that new folder, and open/show that
  folder. Use this ONLY when the command clearly chains all of these
  together in one sentence (open + copy + new folder + paste + show).
  If the command only does one or two of these things, use the
  single-purpose actions instead (open_file / copy_file / create_folder
  / paste_file).
  (e.g. "Open Agency Services file, copy it, create a new folder called
  Projects, paste it there and show me", "agency services file kholo,
  iski copy bnao, naya folder bnao jiska naam Projects rkho, usmy paste
  kr do or mujhe show kro")
- "copy_file": copy a file so it can be pasted into a folder next. If
  the command doesn't name a specific file (e.g. it just says "this
  file" / "isko" / "iski"), leave name empty -- it will default to
  whatever file is currently open.
  (e.g. "is file ki copy bnao", "iski copy bna do", "copy report.pdf",
  "copy this file")
- "paste_file": paste the previously copied file into a named folder.
  The folder doesn't need to be one of the known shortcuts -- it can be
  any folder name the user says, including ones they just created.
  (e.g. "isay Notes folder mein paste kar do", "paste it in the new
  folder", "ye Notes wale folder mein paste krdo")
- "find": search for a file by name or file type in common folders.
  (e.g. "find pdf", "find pdf about tax", "resume dhoondo")
- "none": the command doesn't match any of the above.

User command: "{text}"

Return ONLY JSON, no other text. Use one of these exact shapes:
{{"action": "create_folder", "name": "<folder name>", "location": "<optional known folder, else Downloads>"}}
{{"action": "rename", "old_name": "<current filename>", "new_name": "<new filename>"}}
{{"action": "delete", "name": "<filename>"}}
{{"action": "move", "name": "<filename>", "destination": "<known folder name>"}}
{{"action": "open_folder", "name": "<known folder name>"}}
{{"action": "open_file", "name": "<file name, can be partial>", "location": "<optional known folder, else Downloads>"}}
{{"action": "close_file"}}
{{"action": "open_copy_to_new_folder", "name": "<source file name>", "folder_name": "<new folder name>", "location": "<optional known folder to create it in, else Downloads>"}}
{{"action": "copy_file", "name": "<file name, or empty string to mean 'the currently open file'>"}}
{{"action": "paste_file", "destination": "<folder name>"}}
{{"action": "find", "query": "<filename or keyword>", "file_type": "<extension without dot, e.g. pdf, or empty string>"}}
{{"action": "none"}}
"""

    try:
        response = _call_llm_with_timeout(prompt, max_tokens=150)
    except TimeoutError as e:
        print("Classify timeout:", e)
        return {"action": "_timeout"}

    try:
        return json.loads(response)
    except Exception as e:
        print("Classify parse error:", e)
        return {"action": "none"}


def _llm_classify_compound(text: str) -> dict:
    """
    Dedicated, narrowly-scoped extraction used only when
    _is_compound_copy_paste_intent() already determined the command is
    the "copy + create/open a folder + paste + show" combo. We
    deliberately don't hand the model the full 9-action menu here --
    with all of them available it kept latching onto just one verb in
    the sentence (copy_file, or create_folder) and dropping the rest of
    the request. Asking it to fill in fields for ONE already-decided
    action is far more reliable than asking it to pick the right action
    among many when a single command is doing several things at once.
    """

    prompt = f"""
Extract fields from this command. The command asks to take a file (by
name, or "it"/"this file" meaning one that was already copied or opened
earlier) and put a copy of it into a folder -- possibly a brand-new
folder -- and it may also ask to open/show that folder afterward.
Commands may be in English, Urdu, Roman Urdu, or mixed.

If no source file is named explicitly (the command just says "it",
"this", "isko", "iski" etc.), return "name" as an empty string.

Command: "{text}"

Return ONLY JSON, no other text, in exactly this shape:
{{"name": "<source file name, or empty string if none is named>", "folder_name": "<destination folder name>", "location": "<optional known folder to create it in: downloads, desktop, or documents -- else downloads>"}}
"""

    try:
        response = _call_llm_with_timeout(prompt, max_tokens=100)
    except TimeoutError as e:
        print("Compound classify timeout:", e)
        return {"action": "_timeout"}

    try:
        parsed = json.loads(response)
        parsed["action"] = "open_copy_to_new_folder"
        return parsed
    except Exception as e:
        print("Compound classify parse error:", e)
        return {"action": "none"}


# =================================================================
# HANDLERS
# =================================================================

def _handle_create_folder(state, data: dict):
    global _last_created_folder

    name = data.get("name", "").strip()
    location_key = (data.get("location") or "downloads").lower().strip()
    base = KNOWN_FOLDERS.get(location_key, KNOWN_FOLDERS["downloads"])

    if not name:
        state["response"] = "What should I name the folder?"
        return state

    path = os.path.join(base, name)

    try:
        os.makedirs(path, exist_ok=True)
        _last_created_folder = path
        state["response"] = f"Created folder '{name}'."
    except Exception as e:
        print("Create folder error:", e)
        state["response"] = f"Couldn't create folder '{name}'."
        return state

    if data.get("show"):
        shown = _open_in_file_explorer(path)
        if shown:
            state["response"] += " Opened it so you can see it."

    return state


def _handle_rename(state, data: dict):
    old_name = data.get("old_name", "").strip()
    new_name = data.get("new_name", "").strip()

    if not old_name or not new_name:
        state["response"] = "I need both the current and new file name."
        return state

    old_path = _resolve_path(old_name)
    new_path = os.path.join(os.path.dirname(old_path), new_name)

    try:
        os.rename(old_path, new_path)
        state["response"] = f"Renamed '{old_name}' to '{new_name}'."
    except FileNotFoundError:
        state["response"] = f"Couldn't find '{old_name}'."
    except Exception as e:
        print("Rename error:", e)
        state["response"] = f"Couldn't rename '{old_name}'."

    return state


def _handle_delete(state, data: dict):
    name = data.get("name", "").strip()

    if not name:
        state["response"] = "Which file should I delete?"
        return state

    path = _resolve_path(name)

    try:
        send2trash(path)
        state["response"] = f"Moved '{name}' to Recycle Bin."
    except FileNotFoundError:
        state["response"] = f"Couldn't find '{name}'."
    except Exception as e:
        print("Delete error:", e)
        state["response"] = f"Couldn't delete '{name}'."

    return state


def _handle_move(state, data: dict):
    name = data.get("name", "").strip()
    destination_key = (data.get("destination") or "").lower().strip()

    if not name or destination_key not in KNOWN_FOLDERS:
        state["response"] = "I need a file name and a known destination folder (Downloads, Desktop, or Documents)."
        return state

    src = _resolve_path(name)
    dest_folder = KNOWN_FOLDERS[destination_key]

    try:
        shutil.move(src, dest_folder)
        state["response"] = f"Moved '{name}' to {destination_key.title()}."
    except FileNotFoundError:
        state["response"] = f"Couldn't find '{name}'."
    except Exception as e:
        print("Move error:", e)
        state["response"] = f"Couldn't move '{name}'."

    return state


def _handle_open_folder(state, data: dict):
    name = (data.get("name") or "").lower().strip()
    path = KNOWN_FOLDERS.get(name)

    if not path:
        state["response"] = f"I don't know the folder '{name}'."
        return state

    success = _open_in_file_explorer(path)
    state["response"] = (
        f"Opened {name.title()}." if success else f"Couldn't open {name.title()}."
    )
    return state


def _normalize_filename(text: str) -> str:
    """
    Normalizes a spoken filename for flexible matching.

    Examples:
        "My Report.pdf" -> "myreportpdf"
        "my_report"     -> "myreport"
        "My-Report PDF" -> "myreportpdf"
    """
    text = text.lower().strip()

    # Remove common words that users may speak instead of an extension.
    text = text.replace(" file", "")
    text = text.replace(" document", "")

    # Remove spaces, underscores and hyphens.
    for char in [" ", "_", "-"]:
        text = text.replace(char, "")

    return text


# File extensions we should never treat as an "open/copy this file"
# target -- these are almost always the assistant's own source code or
# system files, not documents the user meant. Without this, a script
# like "agency_services_pdf_manager.py" sitting in Documents/Desktop can
# fuzzy-match a query like "agency services" just as well as the actual
# "Agency Services.pdf" the user meant, and (on a tie) sometimes wins.
EXCLUDED_FILE_EXTENSIONS = {
    ".py", ".pyc", ".pyo", ".pyw",
    ".exe", ".dll", ".sys", ".bat", ".cmd", ".ps1", ".sh",
    ".log", ".tmp", ".cache",
    ".json", ".lock", ".ini", ".cfg", ".toml", ".yaml", ".yml",
}

# Folder names to never walk into during a fuzzy search -- project
# tooling/junk, not user documents.
EXCLUDED_DIR_NAMES = {
    ".git", "__pycache__", "node_modules", "venv", ".venv", "env",
    ".idea", ".vscode", "dist", "build",
}


def _score_match(normalized_candidate: str, normalized_query: str) -> int:
    """
    Lower is better. 0 = exact match, 1 = candidate starts with the
    query, 2 = query just appears somewhere in the candidate. Same
    ranking idea used by _handle_open_file, pulled out so copy/paste can
    reuse it without duplicating logic.
    """
    if normalized_candidate == normalized_query:
        return 0
    if normalized_candidate.startswith(normalized_query):
        return 1
    return 2


def _find_matching_files(query: str, folders=None):
    """
    Fuzzy-finds files by name -- NOT an exact-match lookup. Normalizes
    both the query and every candidate filename the same forgiving way
    _handle_open_file does (lowercase, strip spaces/underscores/hyphens,
    drop " file"/" document"), then matches on substring containment.
    Searches recursively through `folders` (defaults to every known
    folder). Returns full paths sorted best-match-first, or [] if
    nothing matched.
    """
    folders = folders or SEARCH_FOLDERS
    normalized_query = _normalize_filename(query)

    if not normalized_query:
        return []

    matches = []
    for folder in folders:
        if not os.path.isdir(folder):
            continue
        for root, dirs, files in os.walk(folder):
            # Don't descend into project/tooling junk (.git, venv, etc.)
            dirs[:] = [d for d in dirs if d not in EXCLUDED_DIR_NAMES]

            for filename in files:
                ext = os.path.splitext(filename)[1].lower()
                if ext in EXCLUDED_FILE_EXTENSIONS:
                    continue
                if normalized_query in _normalize_filename(filename):
                    matches.append(os.path.join(root, filename))

    # De-dup while preserving order (same file can be found more than
    # once if SEARCH_FOLDERS overlap).
    seen = set()
    unique = []
    for path in matches:
        key = os.path.normcase(os.path.abspath(path))
        if key not in seen:
            seen.add(key)
            unique.append(path)

    unique.sort(
        key=lambda p: _score_match(_normalize_filename(os.path.basename(p)), normalized_query)
    )
    return unique


def _find_matching_folders(query: str):
    """
    Fuzzy-finds a folder by name -- e.g. a folder the user created a
    minute ago via "create_folder" won't be in KNOWN_FOLDERS, so we
    can't just do a dict lookup. Checks the known shortcuts first
    (Downloads/Desktop/Documents), then searches inside each of them
    (folders created via "create_folder" default to living there) using
    the same normalize+substring matching as _find_matching_files.
    Returns full paths sorted best-match-first, or [] if nothing
    matched.
    """
    normalized_query = _normalize_filename(query)

    if not normalized_query:
        return []

    matches = []

    # 1. Known top-level shortcuts ("downloads", "desktop", "documents").
    for key, path in KNOWN_FOLDERS.items():
        if normalized_query in _normalize_filename(key) and os.path.isdir(path):
            matches.append(path)

    # 2. Sub-folders inside each known folder -- covers newly created,
    #    user-named folders without hardcoding their names anywhere.
    for base in KNOWN_FOLDERS.values():
        if not os.path.isdir(base):
            continue
        for root, dirs, _files in os.walk(base):
            dirs[:] = [d for d in dirs if d not in EXCLUDED_DIR_NAMES]
            for d in dirs:
                if normalized_query in _normalize_filename(d):
                    matches.append(os.path.join(root, d))

    seen = set()
    unique = []
    for path in matches:
        key = os.path.normcase(os.path.abspath(path))
        if key not in seen:
            seen.add(key)
            unique.append(path)

    unique.sort(
        key=lambda p: _score_match(_normalize_filename(os.path.basename(p)), normalized_query)
    )
    return unique


def _launch_and_track_file(target: str) -> bool:
    """
    Closes whatever was previously tracked as open, launches `target`
    with its default app, and tracks the new window/process so a later
    "close it" (or a compound command that also copies/pastes) knows
    what's currently open. Returns True on successful launch.
    Pulled out of _handle_open_file so other actions (like the compound
    open+copy+paste command) can reuse the exact same launch/tracking
    behavior instead of duplicating it.
    """
    global _open_file_pid, _open_file_hwnd, _open_file_path

    match_basename = os.path.basename(target)

    # Close whatever file we previously opened.
    _close_tracked_file()

    # Snapshot windows BEFORE opening new file.
    before_hwnds = _snapshot_visible_hwnds()

    # Open file using the OS default application.
    success = _open_in_file_explorer(target)

    if not success:
        return False

    # Track newly opened window.
    hwnd = _find_new_window_by_title(match_basename, before_hwnds)

    if hwnd is not None:
        _open_file_hwnd = hwnd
        _open_file_path = target
        try:
            _, pid = win32process.GetWindowThreadProcessId(hwnd)
            _open_file_pid = pid
        except Exception:
            _open_file_pid = None
    else:
        # Fallback: process tracking.
        proc = _find_process_for_file(target)
        if proc is not None:
            _open_file_pid = proc.pid
            _open_file_path = target
            _open_file_hwnd = None

    return True


def _handle_open_file(state, data: dict):
    name = (data.get("name") or "").strip()
    location_key = (data.get("location") or "downloads").lower().strip()

    folder = KNOWN_FOLDERS.get(
        location_key,
        KNOWN_FOLDERS["downloads"]
    )

    if not name:
        state["response"] = "Which file should I open?"
        return state

    if not os.path.isdir(folder):
        state["response"] = (
            f"Couldn't find the {location_key} folder."
        )
        return state

    matches = _find_matching_files(name, folders=[folder])

    if not matches:
        state["response"] = (
            f"I couldn't find a file matching '{name}' "
            f"in {location_key.title()}."
        )
        return state

    target = matches[0]
    match_basename = os.path.basename(target)

    success = _launch_and_track_file(target)

    if not success:
        state["response"] = f"Couldn't open '{match_basename}'."
        return state

    state["response"] = f"Opened '{match_basename}'."
    return state


def _handle_close_file(state, data: dict = None):
    global _open_file_path

    path = _open_file_path

    if not path:
        state["response"] = "There's no file I opened that I can close right now."
        return state

    if _open_file_hwnd is None and psutil is None:
        state["response"] = "Closing files needs the 'psutil' package installed (pip install psutil)."
        return state

    closed = _close_tracked_file()
    if closed:
        state["response"] = f"Closed '{os.path.basename(path)}'."
    else:
        state["response"] = f"'{os.path.basename(path)}' seems to be already closed."

    return state


def _handle_copy_file(state, data: dict):
    """
    "copy this file" (no name) -> copies whatever is currently tracked
    as open (same global _open_file_path used by open_file/close_file).
    "copy X" (name given) -> fuzzy-finds X across known folders, no
    exact-name requirement.
    Either way, just remembers the path in _clipboard_file_path -- the
    actual file copy happens on paste, once we know the destination.
    """
    global _clipboard_file_path

    name = (data.get("name") or "").strip()

    if name:
        matches = _find_matching_files(name)
        if not matches:
            state["response"] = f"I couldn't find a file matching '{name}'."
            return state
        target = matches[0]
    else:
        target = _open_file_path
        if not target or not os.path.isfile(target):
            state["response"] = "No file is currently open. Tell me which file to copy."
            return state

    _clipboard_file_path = target
    state["response"] = (
        f"Copied '{os.path.basename(target)}'. Tell me which folder to paste it in."
    )
    return state


def _handle_paste_file(state, data: dict):
    """
    Pastes whatever _handle_copy_file last remembered into a
    fuzzy-matched destination folder -- "Notes folder", "the new
    folder", etc. don't need to be an exact/known name.

    If the user doesn't name a destination at all (or names one that
    doesn't fuzzy-match anything), falls back to _last_created_folder --
    the most recently created folder -- since that's almost always what
    "paste it" means right after "create a folder called X".

    The destination folder is now always opened afterward (not just
    when the user explicitly said "show"), since seeing the pasted file
    land in the right place is useful regardless.
    """
    global _clipboard_file_path

    if not _clipboard_file_path or not os.path.isfile(_clipboard_file_path):
        state["response"] = "You haven't copied a file yet. Say 'copy this file' first."
        _clipboard_file_path = None
        return state

    destination_name = (data.get("destination") or "").strip()
    dest_folder = None

    if destination_name:
        matches = _find_matching_folders(destination_name)
        if matches:
            dest_folder = matches[0]

    if not dest_folder:
        # Fallback: whatever folder was most recently created, e.g. the
        # user just said "create a folder called Projects" and now says
        # "paste it" without repeating the name.
        dest_folder = _last_created_folder

    if not dest_folder:
        state["response"] = "Which folder should I paste it in?"
        return state

    filename = os.path.basename(_clipboard_file_path)

    try:
        shutil.copy2(_clipboard_file_path, dest_folder)
        state["response"] = f"Pasted '{filename}' into '{os.path.basename(dest_folder)}'."
    except Exception as e:
        print("Paste file error:", e)
        state["response"] = f"Couldn't paste '{filename}' into '{os.path.basename(dest_folder)}'."
        return state

    if _open_in_file_explorer(dest_folder):
        state["response"] += " Opened the folder so you can see it."

    return state


def _handle_open_copy_to_new_folder(state, data: dict):
    """
    The all-in-one version of what the user was doing manually as four
    separate commands: (optionally) open a file, copy it, create a new
    folder with a given name, paste the copy into it, and show that
    folder. Reuses _find_matching_files (fuzzy, not hardcoded), and
    _launch_and_track_file for the open step.

    If no source file name is given (e.g. "create a folder called X and
    paste it there"), falls back to whatever was last explicitly copied
    (_clipboard_file_path) or is currently open (_open_file_path) --
    same fallback _handle_copy_file already uses for "copy this file"
    with no name.
    """
    global _clipboard_file_path, _last_created_folder

    name = (data.get("name") or "").strip()
    folder_name = (data.get("folder_name") or "").strip()
    location_key = (data.get("location") or "downloads").lower().strip()

    if not folder_name:
        state["response"] = "What should I name the new folder?"
        return state

    base = KNOWN_FOLDERS.get(location_key, KNOWN_FOLDERS["downloads"])

    opened = False

    if name:
        # 1. Fuzzy-find the source file across all known folders (not
        #    just the target location -- the user's file could be
        #    anywhere).
        matches = _find_matching_files(name)
        if not matches:
            state["response"] = f"I couldn't find a file matching '{name}'."
            return state
        source = matches[0]

        # 2. Open it (same behavior as a plain "open_file" command) --
        #    only when a name was explicitly given, i.e. the user
        #    actually asked to open something new.
        opened = _launch_and_track_file(source)
    else:
        # No file named -- use whatever was already copied or is
        # currently open.
        source = _clipboard_file_path or _open_file_path
        if not source or not os.path.isfile(source):
            state["response"] = (
                "Which file should I copy? Nothing is currently open or copied."
            )
            return state

    source_basename = os.path.basename(source)

    # 3. Create the new folder.
    new_folder_path = os.path.join(base, folder_name)
    try:
        os.makedirs(new_folder_path, exist_ok=True)
        _last_created_folder = new_folder_path
    except Exception as e:
        print("Create folder error:", e)
        state["response"] = f"Couldn't create folder '{folder_name}'."
        return state

    # 4. Copy the file into it.
    try:
        shutil.copy2(source, new_folder_path)
    except Exception as e:
        print("Copy error:", e)
        state["response"] = f"Created '{folder_name}' but couldn't copy '{source_basename}' into it."
        return state

    _clipboard_file_path = source

    # 5. Show the new folder (open it in File Explorer).
    shown = _open_in_file_explorer(new_folder_path)

    open_note = f"Opened '{source_basename}'" if opened else f"Copied '{source_basename}'"
    show_note = f" and opened '{folder_name}' so you can see it." if shown else "."
    state["response"] = f"{open_note}, copied it into new folder '{folder_name}'{show_note}"

    return state


def _handle_find(state, data: dict):
    query = (data.get("query") or "").lower().strip()
    file_type = (data.get("file_type") or "").lower().strip().lstrip(".")

    matches = []

    for folder in SEARCH_FOLDERS:
        if not os.path.isdir(folder):
            continue
        for root, dirs, files in os.walk(folder):
            dirs[:] = [d for d in dirs if d not in EXCLUDED_DIR_NAMES]
            for f in files:
                name_lower = f.lower()
                ext = os.path.splitext(f)[1].lower()
                # Only skip code/junk extensions when the user didn't
                # explicitly ask for that type -- "find py files" should
                # still work.
                if not file_type and ext in EXCLUDED_FILE_EXTENSIONS:
                    continue
                type_ok = (not file_type) or name_lower.endswith("." + file_type)
                query_ok = (not query) or (query in name_lower)
                if type_ok and query_ok:
                    matches.append(os.path.join(root, f))

    if not matches:
        state["response"] = "Couldn't find any matching file."
        return state

    # Cap how much we read out / show — just the top 5.
    top_matches = matches[:5]
    listed = "\n".join(top_matches)
    state["response"] = f"Found {len(matches)} file(s). Top matches:\n{listed}"

    return state


# =================================================================
# MAIN NODE
# =================================================================

def _file_agent_dispatch(state):
    text = state["user_input"]

    # Close-intent is checked BEFORE the LLM call, not left for the LLM
    # to decide. See _is_close_intent() docstring above for why: a
    # command like "close agency services file" otherwise looks just
    # like an open_file example ("open the file named X") to the
    # classifier, and gets misrouted into re-opening the file instead
    # of closing it.
    if _is_close_intent(text):
        data = {"action": "close_file"}
    elif _is_compound_copy_paste_intent(text):
        # "Copy the agency services file, create a new folder called
        # Important Documents and paste it there and show me" -- route
        # straight to the dedicated compound extraction instead of the
        # general classifier, which kept dropping half the request.
        # See _is_compound_copy_paste_intent()/_llm_classify_compound()
        # docstrings above for why.
        data = _llm_classify_compound(text)
    else:
        data = _llm_classify(text)

    # Independent of whichever action ran, remember whether the user
    # also asked to see the result ("...and show me" / "...dikhao"), so
    # handlers like paste_file/create_folder can open the folder
    # afterward instead of only the compound action supporting it.
    data["show"] = _is_show_intent(text)

    action = data.get("action")

    if action == "_timeout":
        state["response"] = "Sorry, that took too long to process. Please try again."
    elif action == "create_folder":
        state = _handle_create_folder(state, data)
    elif action == "rename":
        state = _handle_rename(state, data)
    elif action == "delete":
        state = _handle_delete(state, data)
    elif action == "move":
        state = _handle_move(state, data)
    elif action == "open_folder":
        state = _handle_open_folder(state, data)
    elif action == "open_file":
        state = _handle_open_file(state, data)
    elif action == "close_file":
        state = _handle_close_file(state, data)
    elif action == "copy_file":
        state = _handle_copy_file(state, data)
    elif action == "paste_file":
        state = _handle_paste_file(state, data)
    elif action == "open_copy_to_new_folder":
        state = _handle_open_copy_to_new_folder(state, data)
    elif action == "find":
        state = _handle_find(state, data)
    else:
        state["response"] = "I'm not sure what file operation you want."

    return state


def file_agent_node(state):
    """
    Thin safety-net wrapper around _file_agent_dispatch(). WHY THIS
    EXISTS: previously, any unexpected exception anywhere in the
    classify/handler chain (a bad LLM response shape, a permissions
    error, anything not already caught locally) would propagate all the
    way up and the graph node would never return -- which looks exactly
    like "the assistant just stopped responding" from the voice-loop
    side, since no state/response ever comes back. Wrapping the whole
    dispatch in try/except guarantees file_agent_node ALWAYS returns a
    state with a response, so the assistant can at least say something
    went wrong and keep listening for the next command, instead of
    hanging silently.
    """
    text = state["user_input"]

    try:
        state = _file_agent_dispatch(state)
    except Exception as e:
        print("file_agent_node crashed:", e)
        state["response"] = "Sorry, something went wrong with that file operation."

    state["history"].append({"role": "user", "content": text})
    state["history"].append({"role": "assistant", "content": state["response"]})

    return state

    # ============================================================
# TEMPORARY TEST CODE
# ============================================================
# IMPORTANT:
# Ye sirf testing ke liye hai.
# Test complete hone ke baad is POORE BLOCK ko delete kar dena.
# Upar wale original code mein koi change nahi karna.
# ============================================================

if __name__ == "__main__":

    print("\n========================================")
    print("      FILE AGENT TEST MODE")
    print("========================================")
    print("Type a command to test the file agent.")
    print("Type 'exit' to stop testing.")
    print("========================================\n")

    while True:

        # User se test command lo
        command = input("TEST COMMAND > ").strip()

        # Test band karne ke liye
        if command.lower() in ["exit", "quit"]:
            print("\nTest finished.")
            break

        # Empty command ignore karo
        if not command:
            continue

        # Minimal state — original agent ko required fields
        # provide karne ke liye.
        test_state = {
            "user_input": command,
            "response": "",
            "history": []
        }

        try:
            # ORIGINAL file_agent_node ko test karo
            result = file_agent_node(test_state)

            # Agent ka response show karo
            print("\nJARVIS RESPONSE:")
            print(result.get("response", "No response"))

        except Exception as e:
            # Agar testing mein koi unexpected error aaye
            print("\nTEST ERROR:")
            print(e)

        print("\n" + "-" * 50 + "\n")

# ============================================================
# END OF TEMPORARY TEST CODE
# ============================================================