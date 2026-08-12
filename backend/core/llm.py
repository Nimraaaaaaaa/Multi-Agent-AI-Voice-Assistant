"""
backend/core/llm.py
Free cloud LLM caller using Groq (https://groq.com). No payment needed,
free API key available on signup -- https://console.groq.com/keys
"""
import requests
from backend import config

GROQ_URL = "https://api.groq.com/openai/v1/chat/completions"


def chat(messages: list[dict], system: str = "", max_tokens: int = 300) -> str:
    """messages = [{"role": "user"/"assistant", "content": "..."}]"""
    payload = {
        "model": config.GROQ_MODEL,
        "messages": ([{"role": "system", "content": system}] if system else []) + messages,
        "max_tokens": max_tokens,
    }
    headers = {
        "Authorization": f"Bearer {config.GROQ_API_KEY}",
        "Content-Type": "application/json",
    }
    try:
        resp = requests.post(GROQ_URL, json=payload, headers=headers, timeout=30)
        resp.raise_for_status()
        return resp.json()["choices"][0]["message"]["content"].strip()
    except Exception as e:
        print(f"[llm] Groq error: {e}")
        return "Unable to connect to Groq. Please check your GROQ_API_KEY in the .env file."