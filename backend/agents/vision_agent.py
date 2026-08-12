"""
backend/agents/vision_agent.py

JARVIS Vision Agent

The vision agent looks at the current screen, understands the
visible content, and answers the user's request naturally.

IMPORTANT:
The model should understand the screen FIRST and then answer.
It must NOT expose its analysis/reasoning to the user.

Examples:

User:
    "What's on my screen?"

Good:
    "You're viewing a PDF about agency financial automation.
     It explains how financial reporting, profitability,
     cash flow and team utilization can be automated."

User:
    "What does this paragraph mean?"

Good:
    "It means the system helps service businesses automatically
     track their financial performance instead of doing the
     reporting manually."

User:
    "Read what's on the screen."

Good:
    "The document explains an automation system for service
     businesses. It focuses on financial reporting, profitability,
     cash flow and team performance."

Bad:
    "The user wants to know..."
    "Identify the title..."
    "Heading: ..."
    "Footer: ..."
    "The screen shows a browser toolbar..."
    "I need to analyze..."
"""


import base64
import io
import os
import re

import requests
from dotenv import load_dotenv
from PIL import ImageGrab


# ============================================================
# CONFIG
# ============================================================

load_dotenv()

GROQ_API_KEY = os.getenv("GROQ_API_KEY")

GROQ_VISION_MODEL = os.getenv(
    "GROQ_VISION_MODEL",
    "qwen/qwen3.6-27b"
)

GROQ_URL = (
    "https://api.groq.com/openai/v1/chat/completions"
)


# ============================================================
# SCREEN CAPTURE
# ============================================================

def _capture_screen_base64() -> str:
    """
    Capture the current screen and convert it to base64 PNG.
    """

    screenshot = ImageGrab.grab()

    max_width = 1920

    if screenshot.width > max_width:

        ratio = max_width / screenshot.width

        screenshot = screenshot.resize(
            (
                max_width,
                int(screenshot.height * ratio)
            )
        )

    buffer = io.BytesIO()

    screenshot.save(
        buffer,
        format="PNG"
    )

    return base64.b64encode(
        buffer.getvalue()
    ).decode("utf-8")


# ============================================================
# VISION SYSTEM PROMPT
# ============================================================

VISION_SYSTEM_PROMPT = """
You are JARVIS Vision.

Your job is to LOOK at the screen, UNDERSTAND what is visible,
and then give the user the useful answer.

Think about the entire visible content internally before
answering.

DO NOT expose your analysis.

DO NOT describe your reasoning.

DO NOT describe the steps you used to understand the image.

DO NOT create an inventory of the screen.

DO NOT mention every UI element.

DO NOT mention headers, footers, browser bars, tabs, buttons,
toolbars, page controls, or other irrelevant elements unless
the user specifically asks about them.

DO NOT behave like OCR software.

DO NOT copy every visible piece of text.

DO NOT produce a structured visual inspection report.

DO NOT say:

"The user wants..."
"The user is asking..."
"The user wants me to..."
"I need to..."
"I need to identify..."
"I need to analyze..."
"Let me analyze..."
"Based on the question..."
"Identify the title..."
"Heading:"
"Paragraph:"
"Footer:"
"Header:"
"Bullet points:"
"Table:"
"UI elements:"
"Drafting the response:"
"Analysis:"

NEVER output these kinds of phrases.

Instead:

1. Read and understand the visible content.
2. Determine what the content is about.
3. Answer the user's request directly.
4. Mention only the information that helps answer the request.

============================================================
HARD LIMIT
============================================================

Answer in MAXIMUM 3-4 short sentences.

Never write "Looking at...", "Screenshot", "Wait", "Let me",
"Actually", file paths, page numbers, or tab names.

NEVER use markdown formatting. No bold text (**text**), no
headers, no labels followed by a colon, such as:
"Identify the main content:"
"Transcribe the section:"
"Summary:"
"Main point:"

NEVER quote or transcribe paragraphs, bullet points, or
sentences verbatim from the screen, even partially. Read the
content, understand it, and explain it in YOUR OWN plain
words as flowing natural sentences.

Output must be plain prose only — no markdown, no quotation
marks around screen text, no numbered or labeled sections.

Just the direct answer. Nothing else.

============================================================
WHEN USER ASKS "WHAT'S ON MY SCREEN?"
============================================================

Do NOT list everything visible.

Understand the main document/page/content and give a short
natural explanation of what it is about.

Example:

BAD:
"Identify the document title: Agency Services.pdf.
Heading: About Us.
Paragraph: We help service businesses...
Bullet points: Revenue and Gross Profit...
Footer: ..."

GOOD:
"You're looking at a PDF about financial automation for
service businesses. It explains how financial reporting,
client profitability, cash flow and team performance can be
automated."

============================================================
WHEN USER ASKS "WHAT IS THIS ABOUT?"
============================================================

Understand the page and explain its main purpose.

Example:

GOOD:
"This page is about an automation system for service
businesses. It focuses on making financial reporting,
profitability tracking and cash-flow visibility easier."

============================================================
WHEN USER ASKS "SUMMARIZE THIS"
============================================================

Read the relevant visible text and summarize its meaning.

Do NOT read every sentence back to the user.

Do NOT list headings.

Do NOT say what you identified.

Example:

GOOD:
"The paragraph explains that the system helps service
businesses automate financial reporting and get a clearer
view of their profits, cash flow and overall performance."

============================================================
WHEN USER ASKS ABOUT A SPECIFIC PARAGRAPH
============================================================

Find the paragraph visually.

Understand it.

Then explain its meaning in simple language.

Do not quote the whole paragraph unless explicitly asked.

============================================================
WHEN USER ASKS "READ THIS"
============================================================

Read the relevant visible text naturally.

If there is a lot of text, focus on the text that is most
relevant to the user's request.

============================================================
IMPORTANT
============================================================

The user does NOT want your visual-analysis process.

The user wants the RESULT of your visual understanding.

So:

UNDERSTAND FIRST.

ANSWER SECOND.

NEVER SHOW THE ANALYSIS.

============================================================
STYLE
============================================================

Sound like a helpful human assistant.

Use natural sentences.

Do not use headings unless the user asks for them.

Do not use numbered lists unless the user asks for them.

Do not use bullet lists unless the user asks for them.

Do not unnecessarily mention things such as:
- browser
- toolbar
- tabs
- address bar
- footer
- header
- page controls
- buttons
- UI

unless they are relevant to the question.

Normally answer in around 3-4 sentences.

For a simple question, keep it short (1-2 sentences).

For a more complex question, still stay within 3-4 sentences.

MOST IMPORTANT:

DO NOT TELL THE USER WHAT YOU ANALYZED.

TELL THE USER WHAT YOU UNDERSTOOD.
"""


# ============================================================
# THINK-BLOCK CLEANUP
# ============================================================
# Some vision models emit their internal chain-of-thought
# wrapped in <think>...</think> tags before the real answer.
# This must be stripped BEFORE any other cleanup runs.

_THINK_BLOCK_RE = re.compile(
    r"<think>.*?</think>",
    re.DOTALL | re.IGNORECASE
)


def _strip_think_blocks(text: str) -> str:
    """
    Remove <think>...</think> reasoning blocks the model
    may emit before the actual answer.
    """

    if not text:
        return text

    return _THINK_BLOCK_RE.sub("", text).strip()


# ============================================================
# META RESPONSE CLEANUP
# ============================================================

_META_PATTERNS = [
    r"\bthe user wants\b",
    r"\bthe user is asking\b",
    r"\bthe user wants me to\b",
    r"\bthe user asked\b",
    r"\bthe user needs\b",
    r"\bthe user is trying\b",
    r"\bthe user's request\b",

    r"\bi need to\b",
    r"\bi need to identify\b",
    r"\bi need to analyze\b",
    r"\bi should analyze\b",
    r"\bi should identify\b",
    r"\blet me analyze\b",
    r"\blet me\b",
    r"\bi will analyze\b",
    r"\bi'll analyze\b",

    r"\bbased on the question\b",
    r"\bthe question asks\b",

    r"\bidentify the main application\b",
    r"\bidentify the document title\b",
    r"\bidentify the visible text\b",
    r"\bidentify ui elements\b",

    r"\bdrafting the response\b",
    r"\bmy analysis\b",
    r"\bvisual analysis\b",

    r"\bheading:",
    r"\bparagraph:",
    r"\bfooter:",
    r"\bheader:",
    r"\bbullet points:",
    r"\bui elements:",
    r"\btable:",

    # reasoning / narration leaks
    r"\blooking at\b",
    r"\bscreenshot\s*\d*\b",
    r"\bwait,?\b",
    r"\bre-examine\b",
    r"\bactually,?\b",
    r"\bpage indicator\b",
    r"\baddress bar\b",
    r"\bfile path\b",
    r"\bzoomed\b",
    r"\btab\s*\d\b",

    # file paths / URLs / page-count junk
    r"[a-z]:[\\/]",
    r"%20",
    r"\d+\s*of\s*\d+",
]


def _contains_meta_talk(text: str) -> bool:

    lower = text.lower()

    for pattern in _META_PATTERNS:

        if re.search(pattern, lower):
            return True

    return False


# ============================================================
# RESPONSE CLEANUP
# ============================================================

def _clean_vision_response(answer: str) -> str:
    """
    Remove obvious reasoning/meta-talk and cap the final
    answer to a short, TTS-friendly length.
    """

    if not answer:

        return (
            "I couldn't understand the visible content."
        )

    lines = []

    for raw_line in answer.splitlines():

        line = raw_line.strip()

        if not line:
            continue

        # Remove markdown bullets
        line = re.sub(
            r"^[\-\*\•]\s*",
            "",
            line
        )

        # Remove numbered reasoning/list prefixes
        line = re.sub(
            r"^\d+[\.\)]\s*",
            "",
            line
        )

        # Strip markdown bold markers (**text** -> text)
        line = re.sub(
            r"\*\*(.*?)\*\*",
            r"\1",
            line
        )

        line = line.strip()

        if not line:
            continue

        # Drop short "Label:" / "Identify the main content:"
        # style header lines (<= 8 words, ends with colon).
        if line.endswith(":") and len(line.split()) <= 8:
            continue

        # Drop lines that are just a verbatim quoted chunk of
        # screen text being transcribed back to the user.
        stripped_quotes = line.strip('"').strip("'")
        if (
            line.startswith('"') and line.endswith('"')
        ) or (
            line.startswith("'") and line.endswith("'")
        ):
            continue

        if _contains_meta_talk(line):
            continue

        lines.append(line)

    if not lines:

        return (
            "I couldn't understand the visible content."
        )

    # Join naturally.
    result = " ".join(lines)

    result = re.sub(
        r"\s+",
        " ",
        result
    ).strip()

    # Keep only the first 4 sentences — short & TTS-friendly.
    sentences = re.split(r"(?<=[.!?])\s+", result)
    result = " ".join(sentences[:4]).strip()

    return result


# ============================================================
# VISION LLM
# ============================================================

def _ask_vision_llm(
    image_base64: str,
    question: str
) -> str:

    if not GROQ_API_KEY:

        return (
            "GROQ_API_KEY is missing from .env."
        )

    payload = {

        "model": GROQ_VISION_MODEL,

        "messages": [

            {
                "role": "system",
                "content": VISION_SYSTEM_PROMPT.strip()
            },

            {
                "role": "user",

                "content": [

                    {
                        "type": "text",
                        "text": question
                    },

                    {
                        "type": "image_url",

                        "image_url": {
                            "url": (
                                "data:image/png;base64,"
                                f"{image_base64}"
                            )
                        }
                    }
                ]
            }
        ],

        "temperature": 0.1,

        # Enough space for a useful natural answer.
        "max_tokens": 350
    }

    headers = {

        "Authorization": (
            f"Bearer {GROQ_API_KEY}"
        ),

        "Content-Type": "application/json"
    }

    try:

        response = requests.post(
            GROQ_URL,
            headers=headers,
            json=payload,
            timeout=30
        )

        response.raise_for_status()

        data = response.json()

        answer = (
            data["choices"][0]
            ["message"]
            ["content"]
            .strip()
        )

        # Strip any hidden chain-of-thought before cleanup.
        answer = _strip_think_blocks(answer)

        return _clean_vision_response(answer)

    except requests.exceptions.Timeout:

        print(
            "[VISION] Request timed out."
        )

        return (
            "Vision analysis timed out."
        )

    except requests.exceptions.RequestException as e:

        print(
            "[VISION] Request error:",
            e
        )

        return (
            "I couldn't analyze the screen right now."
        )

    except Exception as e:

        print(
            "[VISION] Unexpected error:",
            e
        )

        return (
            "I couldn't analyze the screen right now."
        )


# ============================================================
# LANGGRAPH NODE
# ============================================================

def vision_agent_node(state):

    """
    Capture the current screen and answer the user's request
    based on the visible content.
    """

    text = state.get(
        "user_input",
        ""
    ).strip()

    # --------------------------------------------------------
    # Capture screen
    # --------------------------------------------------------

    try:

        image_base64 = (
            _capture_screen_base64()
        )

    except Exception as e:

        print(
            "[VISION] Screenshot capture error:",
            e
        )

        response = (
            "I couldn't capture the screen."
        )

        state["response"] = response

        state.setdefault(
            "history",
            []
        )

        state["history"].append({
            "role": "user",
            "content": text
        })

        state["history"].append({
            "role": "assistant",
            "content": response
        })

        return state

    # --------------------------------------------------------
    # User request
    # --------------------------------------------------------

    if text:

        question = text

    else:

        question = (
            "Look at the screen and tell me what it is about."
        )

    # --------------------------------------------------------
    # Vision
    # --------------------------------------------------------

    answer = _ask_vision_llm(
        image_base64=image_base64,
        question=question
    )

    # --------------------------------------------------------
    # Response
    # --------------------------------------------------------

    state["response"] = answer

    state.setdefault(
        "history",
        []
    )

    state["history"].append({
        "role": "user",
        "content": text
    })

    state["history"].append({
        "role": "assistant",
        "content": answer
    })

    return state