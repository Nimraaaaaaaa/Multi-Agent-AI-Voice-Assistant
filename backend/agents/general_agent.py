"""
backend/agents/general_agent.py
Handles normal conversation using Groq. Conversation history
(state["history"]) is sent as context so JARVIS remembers previous
conversation.
"""
from backend.core import llm

SYSTEM_PROMPT = """You are JARVIS, a witty but concise personal AI assistant
speaking with your user, {name}. Keep replies short (1-3 sentences) since
they will be spoken out loud via text-to-speech. You can reply in
English if the user speaks that way, otherwise
match their language."""


def general_node(state):
    history = state["history"][-10:]  # last 10 turns for context
    messages = history + [{"role": "user", "content": state["user_input"]}]
    system_prompt = SYSTEM_PROMPT.format(name=state.get("user_name", "Boss"))

    reply = llm.chat(messages, system=system_prompt, max_tokens=300)

    state["response"] = reply
    state["history"].append({"role": "user", "content": state["user_input"]})
    state["history"].append({"role": "assistant", "content": reply})
    return state


