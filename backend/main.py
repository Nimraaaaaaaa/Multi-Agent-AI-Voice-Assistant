import os
import asyncio
import threading
import webbrowser

from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.staticfiles import StaticFiles

from backend import config
from backend.core.graph import jarvis_graph
from backend.core.state import new_state
from backend.core.stt import listen_once, continuous_listen
from backend.core.tts import speak

app = FastAPI()

STATE = None
GREETED = False
MAIN_LOOP = None
ACTIVE_WS = None

USER_NAME = "Nimra"
# CHANGED: greeting text
GREETING_TEXT = f"Hello {USER_NAME}, how are you? What's on your mind today?"

FRONTEND_DIR = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "frontend"
)


def process_command(text: str):
    global STATE
    STATE["user_input"] = text
    STATE = jarvis_graph.invoke(STATE)
    return STATE["response"]


@app.websocket("/ws")
async def websocket_endpoint(websocket: WebSocket):
    global STATE, GREETED, ACTIVE_WS

    await websocket.accept()
    ACTIVE_WS = websocket

    if not GREETED:
        GREETED = True
        STATE = new_state(USER_NAME)

        async def do_greet():
            print(f"[JARVIS] {GREETING_TEXT}")
            try:
                await websocket.send_json({"event": "jarvis_reply", "text": GREETING_TEXT})
            except Exception:
                pass
            await asyncio.to_thread(speak, GREETING_TEXT)

        asyncio.create_task(do_greet())

    try:
        while True:
            data = await websocket.receive_json()
            event = data.get("event")

            try:
                if event == "user_message":
                    text = (data.get("text") or "").strip()
                    if not text:
                        continue
                    await websocket.send_json({"event": "status", "state": "thinking"})
                    reply = await asyncio.to_thread(process_command, text)
                    await websocket.send_json({"event": "jarvis_reply", "text": reply})
                    await websocket.send_json({"event": "status", "state": "idle"})
                    await asyncio.to_thread(speak, reply)

                elif event == "start_listening":
                    await websocket.send_json({"event": "status", "state": "listening"})
                    text = listen_once()
                    if not text:
                        await websocket.send_json({"event": "status", "state": "idle"})
                        continue
                    await websocket.send_json({"event": "user_transcript", "text": text})
                    await websocket.send_json({"event": "status", "state": "thinking"})
                    reply = await asyncio.to_thread(process_command, text)
                    await websocket.send_json({"event": "jarvis_reply", "text": reply})
                    await websocket.send_json({"event": "status", "state": "idle"})
                    await asyncio.to_thread(speak, reply)
            except Exception as e:
                print(f"[websocket] command error: {e}")
                await websocket.send_json({"event": "jarvis_reply", "text": f"Error executing command: {e}"})
                await websocket.send_json({"event": "status", "state": "idle"})
    except WebSocketDisconnect:
        ACTIVE_WS = None


def _open_browser():
    webbrowser.open(f"http://{config.HOST}:{config.PORT}")


def voice_callback(text: str):
    global ACTIVE_WS, MAIN_LOOP
    if ACTIVE_WS is None or MAIN_LOOP is None:
        return

    async def handle():
        try:
            await ACTIVE_WS.send_json({"event": "user_transcript", "text": text})
            await ACTIVE_WS.send_json({"event": "status", "state": "thinking"})
            reply = await asyncio.to_thread(process_command, text)
            await ACTIVE_WS.send_json({"event": "jarvis_reply", "text": reply})
            await ACTIVE_WS.send_json({"event": "status", "state": "idle"})
            await asyncio.to_thread(speak, reply)
        except Exception as e:
            print(f"[voice_callback] error: {e}")

    asyncio.run_coroutine_threadsafe(handle(), MAIN_LOOP)


@app.on_event("startup")
def startup_event():
    global STATE, MAIN_LOOP
    STATE = new_state(USER_NAME)

    MAIN_LOOP = asyncio.get_event_loop()
    threading.Thread(target=continuous_listen, args=(voice_callback,), daemon=True).start()

    threading.Timer(1.2, _open_browser).start()


app.mount("/", StaticFiles(directory=FRONTEND_DIR, html=True), name="frontend")


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host=config.HOST, port=config.PORT)