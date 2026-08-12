# 🖤 JARVIS AI — Multi-Agent Voice Assistant (LangGraph)

Voice se baat karne wala JARVIS-jaisa assistant, **LangGraph** multi-agent
system pe chalta hai, face se greet karta hai, apps/websites kholta hai,
email bhejta hai, aur conversation yaad rakhta hai. UI = black glowing orb
+ waveform (blue nahi, blue sab use karte hain).

## Structure
```
jarvis-ai/
├── backend/
│   ├── main.py
│   ├── config.py
│   ├── agents/
│   │   ├── router_agent.py
│   │   ├── email_agent.py
│   │   ├── app_launcher_agent.py
│   │   └── general_agent.py
│   └── core/
│       ├── state.py
│       ├── graph.py
│       ├── stt.py
│       ├── tts.py
│       └── face_greet.py
├── frontend/
│   ├── index.html
│   ├── style.css
│   └── script.js
├── known_faces/
├── requirements.txt
└── .env.example
```

## Step 1 — venv banayein
```bash
python -m venv venv
# Windows
venv\Scripts\activate
# Mac/Linux
source venv/bin/activate
```

## Step 2 — install
```bash
pip install -r requirements.txt
```
`face_recognition` compile karti hai (dlib pe depend hai):
- Windows: CMake + "Desktop development with C++" install karein, phir `pip install cmake dlib face_recognition`
- Mac: `brew install cmake && pip install dlib face_recognition`
- Linux: `sudo apt install cmake build-essential`

PyAudio issue ho to:
- Windows: `pip install pipwin && pipwin install pyaudio`
- Mac: `brew install portaudio && pip install pyaudio`
- Linux: `sudo apt install python3-pyaudio`

## Step 3 — .env
`.env.example` ko copy kar ke `.env` rakhein, apni Anthropic API key,
naam, aur Gmail App Password (2-Step Verification → App Passwords) daalein.

## Step 4 — apni photo
`known_faces/nimra.jpg` (filename = greeting mein naam).

## Step 5 — run
```bash
python -m backend.main
```
Camera check karega → browser mein UI khulega (`http://127.0.0.1:5050`) →
orb pe click kar ke bolna shuru karein, ya neeche text box use karein.

### Try karein
- "Open chrome"
- "Youtube khol do"
- "Open my email"
- "Email nimra ke subject job update body interview kal hai"
- "Kaisa hai tu"

## Customize
- Naye apps: `backend/agents/app_launcher_agent.py` → `APP_COMMANDS`
- Naye contacts: `backend/agents/email_agent.py` → `CONTACTS`
- Orb color: `frontend/style.css` → `--accent-ember`
- Offline STT: `backend/core/stt.py` mein `speech_recognition` ko `vosk` se replace karein

Made with LangGraph + Claude + Flask-SocketIO
