"""
backend/core/tts.py

Microsoft Edge Neural TTS (edge-tts) -- free, cloud-streamed, natural
sounding voices, 0% local CPU load for synthesis.

CHANGED: local Kokoro model hata diya. speak(text) ka signature aur
behavior (stt.pause_mic() / stt.resume_mic() calls, timing) bilkul
same rakha hai taake baaki poore project (main.py, etc.) ko is switch
ka pata bhi na chale.

CHANGED (fix): asyncio.run() ko _run_async() helper se replace kiya --
ye check karta hai ke current thread mein pehle se koi event loop
running hai ya nahi. Agar hai (jaisa reused executor threads mein kabhi
ho sakta hai), to naye dedicated thread mein alag loop bana ke wahin
coroutine chala deta hai, taake "asyncio.run() cannot be called from a
running event loop" wala crash kabhi na aaye.
"""

import asyncio
import io
import threading
import time

import numpy as np
import sounddevice as sd
import edge_tts
from pydub import AudioSegment

from backend.core import stt

EDGE_TTS_VOICE = "en-US-AndrewNeural"   # try "en-US-AvaNeural" for a female voice

print("🔊 Loading Microsoft Edge Neural TTS (edge-tts)...")
print(f"✅ Edge TTS ready (voice: {EDGE_TTS_VOICE})")


def _run_async(coro):
    """
    Runs an async coroutine safely regardless of whether the current
    thread already has a running event loop.
    """
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        # is thread mein koi loop running nahi -- seedha asyncio.run() safe hai
        return asyncio.run(coro)

    # yahan matlab is thread mein pehle se ek loop chal raha hai --
    # naye thread mein naya loop bana ke wahin coroutine chalate hain
    result = {}
    error = {}

    def runner():
        new_loop = asyncio.new_event_loop()
        try:
            result["value"] = new_loop.run_until_complete(coro)
        except Exception as e:
            error["value"] = e
        finally:
            new_loop.close()

    t = threading.Thread(target=runner, daemon=True)
    t.start()
    t.join()

    if "value" in error:
        raise error["value"]
    return result["value"]


def _synthesize_to_numpy(text: str, voice: str = EDGE_TTS_VOICE):
    """
    Streams speech audio for `text` from Microsoft's Edge TTS cloud
    service and decodes it into a (audio_array, sample_rate) pair that
    sd.play() can use directly -- mirrors what the old Kokoro generator
    used to hand back.
    """

    async def _generate() -> bytes:
        communicate = edge_tts.Communicate(text, voice)
        mp3_bytes = bytearray()
        async for chunk in communicate.stream():
            if chunk["type"] == "audio":
                mp3_bytes.extend(chunk["data"])
        return bytes(mp3_bytes)

    mp3_data = _run_async(_generate())

    # edge-tts streams mp3 -- decode it to raw PCM samples via pydub/ffmpeg
    segment = AudioSegment.from_file(io.BytesIO(mp3_data), format="mp3")
    sample_rate = segment.frame_rate

    samples = np.array(segment.get_array_of_samples())
    if segment.channels > 1:
        samples = samples.reshape((-1, segment.channels))

    # normalize integer PCM samples to float32 in [-1, 1] for sounddevice
    audio = samples.astype(np.float32) / (2 ** (8 * segment.sample_width - 1))

    return audio, sample_rate


def speak(text: str):
    if not text:
        return

    stt.pause_mic()

    try:
        audio, sample_rate = _synthesize_to_numpy(text, voice=EDGE_TTS_VOICE)

        sd.play(audio, sample_rate)
        sd.wait()
    except Exception as e:
        print(f"❌ TTS Error: {e}")
    finally:
        # gives room echo/reverb tail time to die down before mic starts
        # listening again
        time.sleep(0.9)
        stt.resume_mic()   # this also clears the leftover audio queue