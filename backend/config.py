
import os
from dotenv import load_dotenv

load_dotenv()

# ---- Groq API (free, cloud-based, used for chat/routing) ----
GROQ_API_KEY = os.getenv("GROQ_API_KEY", "")
GROQ_MODEL = os.getenv("GROQ_MODEL", "llama-3.3-70b-versatile")

# ---- Whisper (local, free, offline speech-to-text) ----
WHISPER_MODEL = os.getenv("WHISPER_MODEL", "base")       # tiny/base/small/medium/large
WHISPER_LANGUAGE = os.getenv("WHISPER_LANGUAGE", "")      # empty = auto-detect language

# ---- Microphone (device index for PyAudio) ----
# Run test_mic.py to find which device index works on your system.
# Set to None (or leave empty) to use PyAudio default, or a number like 4.
_mic_idx = os.getenv("MIC_DEVICE_INDEX", "4")
MIC_DEVICE_INDEX = int(_mic_idx) if _mic_idx.strip() else None


USER_NAME = os.getenv("USER_NAME", "Boss")

GMAIL_ADDRESS = os.getenv("GMAIL_ADDRESS", "")
GMAIL_APP_PASSWORD = os.getenv("GMAIL_APP_PASSWORD", "")

HOST = os.getenv("HOST", "127.0.0.1")
PORT = int(os.getenv("PORT", "5050"))

KNOWN_FACES_DIR = os.path.join(os.path.dirname(os.path.dirname(__file__)), "known_faces")
print("GROQ_API_KEY:", GROQ_API_KEY[:10] + "..." if GROQ_API_KEY else "NOT LOADED")
print("MODEL:", GROQ_MODEL)