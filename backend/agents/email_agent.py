"""
email_agent.py
Handles email through a step-by-step conversation instead of one long command:

1. Trigger  -> "email bhejni hai" / "send email" (contains an email keyword,
               router sends it here) -> starts the flow, asks "who?"
2. Step 1   -> user gives the contact name           -> asks "subject?"
3. Step 2   -> user gives the subject                -> asks "body?"
4. Step 3   -> user gives the body                   -> sends immediately

While a flow is in progress, state["email_flow"] holds the step + collected data.
router_agent.py checks this and keeps forwarding replies here until it's done.

To add a new contact, just add a line in the CONTACTS dictionary.
"""
import webbrowser

import smtplib
from email.mime.text import MIMEText

from backend import config

CONTACTS = {
    "nimra": "n",
    "nimra": "n"
    # "ali": "ali@example.com",
}

CANCEL_WORDS = ["cancel", "stop", "no", "n"]
OPEN_ONLY_WORDS = ["khol", "kholo", "open"]


def _send_email(to_addr: str, subject: str, body: str) -> bool:
    if not config.GMAIL_ADDRESS or not config.GMAIL_APP_PASSWORD:
        return False
    try:
        msg = MIMEText(body)
        msg["Subject"] = subject
        msg["From"] = config.GMAIL_ADDRESS
        msg["To"] = to_addr

        with smtplib.SMTP_SSL("smtp.gmail.com", 465) as server:
            server.login(config.GMAIL_ADDRESS, config.GMAIL_APP_PASSWORD)
            server.sendmail(config.GMAIL_ADDRESS, [to_addr], msg.as_string())
        return True
    except Exception:
        return False


def _reply(state, text):
    """Small helper: sets the response and logs the turn in history."""
    state["response"] = text
    state["history"].append({"role": "user", "content": state["user_input"]})
    state["history"].append({"role": "assistant", "content": text})
    return state


def email_node(state):
    text = state["user_input"].strip()
    flow = state.get("email_flow")

    # ---------- an email flow is already in progress ----------
    if flow:
        step = flow["step"]

        # allow bailing out at any step
        if text.lower() in CANCEL_WORDS:
            state["email_flow"] = None
            return _reply(state, "Okay, email cancelled.")

        if step == "contact":
            contact_name = text

            if "@" in contact_name and "." in contact_name.split("@")[-1]:
                # looks like a raw email address -> use it directly, no CONTACTS lookup needed
                to_addr = contact_name.strip()
                contact_name = to_addr.split("@")[0]  # just for display, e.g. "Nimra ko email bhej diya"
            else:
                # otherwise treat it as a saved contact name
                to_addr = CONTACTS.get(contact_name.lower())
                if not to_addr:
                    state["email_flow"] = None
                    return _reply(
                        state,
                        f"No email address found for '{contact_name}' in CONTACTS, and it "
                        "doesn't look like a full email address either. Either add it to "
                        "CONTACTS in email_agent.py, or type the full email address "
                        "(e.g. name@gmail.com).",
                    )

            flow["contact_name"] = contact_name
            flow["to_addr"] = to_addr
            flow["step"] = "subject"
            state["email_flow"] = flow
            return _reply(state, f"Got it, emailing {contact_name.title()}. What's the subject?")

        if step == "subject":
            flow["subject"] = text
            flow["step"] = "body"
            state["email_flow"] = flow
            return _reply(state, "And what should the body say?")

        if step == "body":
            flow["body"] = text

            # Send immediately — no confirmation step.
            ok = _send_email(flow["to_addr"], flow["subject"], flow["body"])
            state["email_flow"] = None

            msg = (
                f"Email sent to {flow['contact_name'].title()}."
                if ok else
                "Failed to send the email. Please check GMAIL_ADDRESS and "
                "GMAIL_APP_PASSWORD in your .env file."
            )
            return _reply(state, msg)

    # ---------- no flow yet: decide what to do with a fresh message ----------
    wants_open_only = any(w in text.lower() for w in OPEN_ONLY_WORDS) and "send" not in text.lower()

    if wants_open_only:
        webbrowser.open("https://mail.google.com")
        return _reply(state, "Opening Gmail.")

    # anything else that landed here (router sent it because of an email keyword)
    # -> start the step-by-step compose flow
    state["email_flow"] = {"step": "contact"}
    return _reply(state, "Sure, who do you want to email?")