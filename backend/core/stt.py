"""
backend/core/stt.py
Real-time ready STT using Groq Cloud Whisper API (whisper-large-v3) + sounddevice

CHANGED: local faster-whisper model hata diya, ab transcription Groq ke
cloud server par hoti hai. Laptop CPU par 0% heavy load, koi heating nahi,
aur Loom recording ke sath bhi smooth kaam karega.
"""

import io
import os
import time
import queue
import threading

import numpy as np
import sounddevice as sd
import soundfile as sf
from dotenv import load_dotenv
from groq import Groq

# ==========================================================
# Config
# ==========================================================
SAMPLE_RATE = 16000
CHANNELS = 1
MIC_DEVICE_INDEX = 0          # tumhara working mic
GROQ_WHISPER_MODEL = "whisper-large-v3"   # CHANGED: cloud model, 10x bigger than small.en

load_dotenv()

GROQ_API_KEY = os.getenv("GROQ_API_KEY")
if not GROQ_API_KEY:
    raise RuntimeError(
        "❌ GROQ_API_KEY not found. Add it to your .env file, e.g.\n"
        "   GROQ_API_KEY=gsk_xxxxxxxxxxxxxxxxxxxx"
    )

print("⚡ Connecting to Groq Cloud Whisper API...")
groq_client = Groq(api_key=GROQ_API_KEY)
print(f"✅ Groq client ready (model: {GROQ_WHISPER_MODEL})")


# ==========================================================
# CHANGED: hallucination control -- Whisper (and Groq's hosted version)
# sometimes hallucinates stock phrases on silence/noise/low-signal audio,
# things like "thank you", "subscribe", "subtitles by ...", etc.
# Two layers of defense:
#   1. GROQ_PROMPT nudges the model at inference time to not add that stuff.
#   2. HALLUCINATIONS blacklist catches it after the fact and discards it.
# ==========================================================
GROQ_PROMPT = (
    "This is a direct live user voice command spoken to a voice assistant. "
    "Transcribe only what was actually said. Do not add subtitles, "
    "captions, credits, or phrases like 'thank you for watching'."
)

HALLUCINATIONS = {
    "thank you",
    "thank you.",
    "thanks for watching",
    "thanks for watching!",
    "subtitles by",
    "amara.org",
    "you",
    "bye",
    "bye.",
    "bye bye",
    "subscribe",
    "please subscribe",
    "like and subscribe",
    "thank you for watching",
    "thank you for watching!",
    "see you next time",
    "see you in the next video",
    ".",
}


def _is_hallucination(text: str) -> bool:
    """CHANGED: normalizes text and checks it against the blacklist."""
    normalized = text.strip().lower().strip(".!? ")
    return normalized in HALLUCINATIONS


# ==========================================================
# mic pause/resume so TTS playback doesn't get picked back up by STT
# ==========================================================
mic_paused = threading.Event()

# module-level queue reference so it can be drained from outside
# (e.g. from tts.py after speaking ends) to throw away any stale/echo audio
# that queued up while mic_paused was set.
_audio_queue = None


def pause_mic():
    """Call this right before speak() starts playing audio."""
    mic_paused.set()


def resume_mic():
    """Call this right after speak() finishes playing audio."""
    clear_audio_queue()   # drop any leftover echo/backlog before resuming
    mic_paused.clear()


def clear_audio_queue():
    """Drains any pending audio chunks so old/echo audio isn't processed."""
    global _audio_queue
    if _audio_queue is None:
        return
    while not _audio_queue.empty():
        try:
            _audio_queue.get_nowait()
        except queue.Empty:
            break


# ==========================================================
# CHANGED: single helper that sends a numpy audio array to Groq's
# cloud Whisper API and returns the transcribed text. Both
# listen_once() and continuous_listen() now use this instead of a
# local model.transcribe() call.
# ==========================================================
def _transcribe_with_groq(audio: np.ndarray, sample_rate: int = SAMPLE_RATE) -> str:
    try:
        # write audio to an in-memory WAV buffer -- no disk round trip needed
        buffer = io.BytesIO()
        sf.write(buffer, audio, sample_rate, format="WAV", subtype="PCM_16")
        buffer.seek(0)

        transcript = groq_client.audio.transcriptions.create(
            file=("audio.wav", buffer, "audio/wav"),
            model=GROQ_WHISPER_MODEL,
            language="en",
            temperature=0.0,
            response_format="text",
            prompt=GROQ_PROMPT,   # CHANGED: nudge model away from stock hallucinated phrases
        )

        # groq sdk returns a plain string when response_format="text",
        # but handle the object form too just in case
        text = transcript if isinstance(transcript, str) else getattr(transcript, "text", "")
        text = text.strip()

        # CHANGED: second line of defense -- discard known hallucinated phrases
        # (silence/noise sometimes still slips one past the prompt hint above)
        if _is_hallucination(text):
            print(f"🚫 Discarded likely hallucination: {text!r}")
            return ""

        return text

    except Exception as e:
        print(f"❌ Groq STT Error: {e}")
        return ""


def listen_once(duration=4):
    """
    Records for a few seconds and returns transcribed text.
    Kept for manual/one-shot use (e.g. testing from the command line).

    NOTE: this is a FIXED-DURATION recording (default 4s) -- it does NOT
    listen for natural pauses. It will always cut off at `duration` seconds
    regardless of whether you're still speaking. This is by design, meant
    only for quick manual testing -- NOT for real conversation capture.
    For natural, hands-free listening that waits for you to actually stop
    talking, use continuous_listen() instead.
    """

    try:
        print(f"🎤 Speak now... (fixed {duration}s recording, not endpoint-based)")

        audio = sd.rec(
            int(duration * SAMPLE_RATE),
            samplerate=SAMPLE_RATE,
            channels=CHANNELS,
            dtype="float32",
            device=MIC_DEVICE_INDEX
        )

        sd.wait()

        return _transcribe_with_groq(audio, SAMPLE_RATE)

    except Exception as e:
        print(f"❌ STT Error: {e}")
        return ""


# ==========================================================
# Continuous / hands-free listening (no button needed)
# ==========================================================
def continuous_listen(
    callback,
    silence_threshold=0.03,
    silence_duration=1.2,     # normal thinking/breathing pauses don't cut you
                               # off mid-sentence; only real silence ends the
                               # utterance and triggers transcription
    min_speech_duration=0.8,
    min_avg_volume=0.015,
):
    """
    Runs forever (meant to be started in its own background thread).

    This is the function that should be used for natural, hands-free
    conversation -- it keeps buffering your speech through short pauses
    and only finalizes + transcribes once you've been silent for
    `silence_duration` seconds.
    """

    global _audio_queue
    q = queue.Queue()
    _audio_queue = q   # expose queue so clear_audio_queue() can reach it

    def audio_callback(indata, frames, time_info, status):
        if status:
            pass
        q.put(indata.copy())

    buffer = []
    is_speaking = False
    silence_start = None

    print("🎤 Continuous listening started (hands-free, no button needed)...")

    try:
        with sd.InputStream(
            samplerate=SAMPLE_RATE,
            channels=CHANNELS,
            dtype="float32",
            device=MIC_DEVICE_INDEX,
            callback=audio_callback,
            blocksize=int(SAMPLE_RATE * 0.1),
            latency="high",
        ):
            while True:
                chunk = q.get()

                if mic_paused.is_set():
                    buffer = []
                    is_speaking = False
                    silence_start = None
                    continue

                volume = np.sqrt(np.mean(chunk ** 2))

                if volume > silence_threshold:
                    buffer.append(chunk)
                    is_speaking = True
                    silence_start = None

                elif is_speaking:
                    buffer.append(chunk)

                    if silence_start is None:
                        silence_start = time.time()
                    elif time.time() - silence_start > silence_duration:
                        audio = np.concatenate(buffer, axis=0)

                        # trailing silence trim karo transcribe se pehle
                        # taake wo dead-air Groq ko process na karna pade
                        silence_samples = int(silence_duration * SAMPLE_RATE)
                        if len(audio) > silence_samples:
                            audio = audio[:-silence_samples]

                        clip_duration = len(audio) / SAMPLE_RATE
                        avg_volume = np.sqrt(np.mean(audio ** 2))

                        buffer = []
                        is_speaking = False
                        silence_start = None

                        if clip_duration < min_speech_duration:
                            continue

                        if avg_volume < min_avg_volume:
                            continue

                        t0 = time.time()   # timing log for debugging

                        text = _transcribe_with_groq(audio, SAMPLE_RATE)

                        t1 = time.time()
                        print(f"⏱ Groq transcribe took {t1 - t0:.2f}s, clip was {clip_duration:.2f}s")

                        if text:
                            callback(text)

    except Exception as e:
        print(f"❌ continuous_listen fatal error: {e}")
# ==========================================================
# Testing (Run this file directly)
# ==========================================================
# Usage:
#   python backend/core/stt.py
#
# Speak after the prompt appears.
# Press Ctrl + C to stop continuous listening.
#
# listen_once() test loop is SKIPPED by default since it's a
# fixed-duration function and was confusing continuous_listen() testing.
# Set RUN_LISTEN_ONCE_TEST = True below if you specifically want to test it.
# ==========================================================

RUN_LISTEN_ONCE_TEST = False   # default off -- listen_once() always
                                # cuts at `duration` seconds by design, it's not
                                # meant to test natural/continuous conversation

if __name__ == "__main__":

    print("\n==============================")
    print(" Groq Cloud Whisper STT Test")
    print("==============================\n")

    # ---------- Test 1 : One-shot recording (optional) ----------
    if RUN_LISTEN_ONCE_TEST:
        print("Testing listen_once()...\n")

        while True:
            text = listen_once(duration=4)

            if text:
                print(f"📝 Recognized: {text}")
            else:
                print("❌ Nothing recognized.")

            choice = input("\nTest again? (y/n): ").strip().lower()
            if choice != "y":
                break

    # ---------- Test 2 : Continuous Listening ----------
    print("\nStarting Continuous Listening...")
    print("Speak naturally, pause normally between sentences -- it will only")
    print("cut and transcribe after a short real silence.")
    print("Press Ctrl+C to stop.\n")

    def on_text(text):
        print(f"📝 {text}")

    try:
        continuous_listen(on_text)

    except KeyboardInterrupt:
        print("\n👋 Continuous listening stopped.")