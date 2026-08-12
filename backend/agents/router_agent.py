import difflib

from backend.core import llm

EMAIL_KEYWORDS = ["email", "mail", "gmail"]
WHATSAPP_KEYWORDS = ["whatsapp", "wp","message on wp"]
APP_KEYWORDS = [
    "open", "start", "chrome",
    "search", "pause", "volume", "next", "go back", "refresh","open linkedin", "open github",
]
VISION_KEYWORDS = [
    "screen", "screenshot", "read this image", "read the image",
    "this image", "summarize this pdf", "summarize the pdf", "scan",
     "what's on my",
]
FILE_KEYWORDS = FILE_KEYWORDS = [
    "folder", "rename", "delete file", "delete the file", "move file",
    "find pdf", "find file", "downloads", "desktop", "documents",
    "create file", "delete the file", "create folder",
    "close this", "close it", "close the file", "close file",
    "close the pdf", "close pdf", "close this pdf", "close this file",
    "band kardo", "band kro", "band karo", "isko band", "ye band",
]

# common action/domain words the router cares about — typos in these get
# auto-corrected before keyword matching, so "clsoe"/"downlaods" etc still
# route correctly even with small spelling mistakes.
_CORRECTION_VOCAB = [
    "open", "close", "delete", "rename", "move", "create", "find",
    "search", "pause", "resume", "volume", "refresh", "downloads",
    "desktop", "documents", "folder", "file", "pdf",
]


def _autocorrect_for_routing(text: str) -> str:
    words = text.split()
    corrected = []
    for w in words:
        stripped = w.strip(".,!?")
        if not stripped:
            corrected.append(w)
            continue
        match = difflib.get_close_matches(
            stripped.lower(), _CORRECTION_VOCAB, n=1, cutoff=0.75
        )
        corrected.append(match[0] if match else w)
    return " ".join(corrected)


def _keyword_route(text: str) -> str | None:
    t = _autocorrect_for_routing(text.lower())
    if any(k in t for k in EMAIL_KEYWORDS):
        return "email"
    if any(k in t for k in WHATSAPP_KEYWORDS):
        return "whatsapp"
    if any(k in t for k in VISION_KEYWORDS):
        return "vision"
    if any(k in t for k in FILE_KEYWORDS):
        return "file"
    if any(k in t for k in APP_KEYWORDS):
        return "app_launcher"
    return None


def _llm_route(text: str) -> str:
    prompt = f"""Classify the user's command into exactly one category.
Reply with ONLY one word, nothing else.

Categories:
- email        (sending/opening/reading email)
- whatsapp     (sending a WhatsApp message)
- app_launcher (opening apps, websites, browser control, or youtube playback)
- vision       (questions about what's on the screen, reading/summarizing
                 an image, a screenshot, or a PDF that's currently open)
- file         (creating/renaming/deleting/moving files or folders,
                 opening a known folder like Downloads, or finding a file)
- general      (normal conversation, questions, chit-chat)

User command: "{text}"
Category:"""

    label = llm.chat(
        [{"role": "user", "content": prompt}],
        max_tokens=10,
    ).strip().lower()

    if label not in ("email", "whatsapp", "app_launcher", "vision", "file", "general"):
        label = "general"

    return label


def router_node(state):
    if state.get("email_flow"):
        state["route"] = "email"
        return state

    if state.get("whatsapp_flow"):
        state["route"] = "whatsapp"
        return state

    text = state["user_input"]

    route = _keyword_route(text)

    if route is None:
        route = _llm_route(text)

    state["route"] = route
    return state