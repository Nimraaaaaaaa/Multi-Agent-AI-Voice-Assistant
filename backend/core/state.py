"""
state.py
This is LangGraph's shared "memory". Every agent reads and updates this
same object, which is how conversation context is remembered across turns.
"""

from typing import TypedDict, List, Dict, Optional


class JarvisState(TypedDict, total=False):

    # ============================================================
    # CURRENT TURN
    # ============================================================

    user_input: str
    route: Optional[str]
    response: str

    # ============================================================
    # MEMORY
    # ============================================================

    history: List[Dict[str, str]]
    user_name: str

    # ============================================================
    # MULTI-STEP EMAIL FLOW
    # ============================================================

    email_flow: Optional[dict]

    # ============================================================
    # MULTI-STEP WHATSAPP FLOW
    # ============================================================

    whatsapp_flow: Optional[dict]


def new_state(user_name: str) -> JarvisState:

    return JarvisState(
        user_input="",
        route=None,
        response="",
        history=[],
        user_name=user_name,

        # Email conversation state
        email_flow=None,

        # WhatsApp conversation state
        whatsapp_flow=None,
    )