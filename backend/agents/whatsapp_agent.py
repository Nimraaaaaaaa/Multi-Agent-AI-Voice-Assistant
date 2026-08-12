import os
import time
import urllib.parse

import pyautogui

from backend import config

CONTACTS = {
    "homie": "+92",
    # "ali": "+923451234567",
}

CANCEL_WORDS = ["cancel", "stop", "no", "n"]


def _find_contact(name: str):
    """
    Fuzzy contact lookup.

    Tries, in order:
    1. Exact (case-insensitive) match.
    2. Contact key is contained in what the user said, or vice versa.
       e.g. "ejaz" matches "ejaz bhai", "message ejaz" matches "ejaz".

    Returns (matched_name, phone) or (None, None).
    """
    name_lower = name.lower().strip()

    # 1. Exact match
    for key, phone in CONTACTS.items():
        if key.lower() == name_lower:
            return key, phone

    # 2. Partial / substring match
    for key, phone in CONTACTS.items():
        key_lower = key.lower()
        if key_lower in name_lower or name_lower in key_lower:
            return key, phone

    return None, None


def _send_whatsapp(phone: str, message: str) -> bool:
    try:
        url = (
            f"whatsapp://send?"
            f"phone={phone}&"
            f"text={urllib.parse.quote(message)}"
        )

        print("=" * 50)
        print("[WhatsApp] Starting send...")
        print(f"[WhatsApp] Phone: {phone}")
        print(f"[WhatsApp] Message: {message}")
        print(f"[WhatsApp] URL: {url}")

        # Open WhatsApp Desktop directly into the chat
        os.startfile(url)

        print("[WhatsApp] WhatsApp launch command executed.")

        # IMPORTANT:
        # Give WhatsApp Desktop time to open the chat
        time.sleep(8)

        print("[WhatsApp] Attempting to send...")

        # Press Enter
        pyautogui.press("enter")

        # Allow WhatsApp to process Enter
        time.sleep(2)

        print("[WhatsApp] Send action completed.")
        print("=" * 50)

        return True

    except Exception as e:
        print("=" * 50)
        print(f"[WhatsApp] ERROR: {e}")
        print("=" * 50)
        return False


def _reply(state, text):
    state["response"] = text
    state["history"].append({"role": "user", "content": state["user_input"]})
    state["history"].append({"role": "assistant", "content": text})
    return state


def whatsapp_node(state):
    text = state["user_input"].strip()
    flow = state.get("whatsapp_flow")

    # ---------- a message flow is already in progress ----------
    if flow:
        step = flow["step"]

        if text.lower() in CANCEL_WORDS:
            state["whatsapp_flow"] = None
            return _reply(state, "Okay, message cancelled.")

        if step == "contact":
            contact_name = text
            matched_name, phone = _find_contact(contact_name)

            if not phone:
                state["whatsapp_flow"] = None
                return _reply(
                    state,
                    f"No number found for '{contact_name}' in CONTACTS. "
                    "Please add it to whatsapp_agent.py, then try again.",
                )

            flow["contact_name"] = matched_name
            flow["phone"] = phone
            flow["step"] = "message"
            state["whatsapp_flow"] = flow
            return _reply(state, f"Got it, messaging {matched_name.title()}. What should it say?")

        if step == "message":
            flow["message"] = text

            # Send immediately — no confirmation step.
            ok = _send_whatsapp(flow["phone"], flow["message"])
            state["whatsapp_flow"] = None

            msg = (
                f"Message sent to {flow['contact_name'].title()}."
                if ok else
                "Failed to send the message. Make sure WhatsApp Desktop "
                "is installed and already logged in."
            )
            return _reply(state, msg)

    # ---------- no flow yet: start one ----------
    state["whatsapp_flow"] = {"step": "contact"}
    return _reply(state, "Sure, who do you want to message on WhatsApp?")