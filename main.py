from typing import Any
from fastapi import FastAPI, WebSocket, WebSocketDisconnect, Body
from fastapi.responses import HTMLResponse
import time
import asyncio
import json
import os
import uvicorn
from pathlib import Path

app = FastAPI()

# Het centrale pad naar je vragenbestand
pomp = Path(__file__).parent / "vragen.json"

# ── Quiz States ──────────────────────────────────────────────────────────────
class QuizState:
    LOBBY = "lobby"
    VRAAG_TONEN = "vraag_tonen"
    VRAAG_ACTIEF = "vraag_actief"
    VRAAG_UITSLAG = "vraag_uitslag"
    EIND_UITSLAG = "eind_uitslag"

# Het centrale quiz-object
quiz: dict[str, str | list[Any] | int | None | dict[Any, Any]] = {
    "status": QuizState.LOBBY,
    "vragen": [],
    "huidige_vraag": 0,
    "start_tijd": None,
    "antwoorden": {},
    "scores": {},
}

spelers: dict[str, WebSocket] = {}
admin_ws: WebSocket | None = None
beamer_ws: list[WebSocket] = []

# ── Vragen Laden ─────────────────────────────────────────────────────────────
def laad_vragen():
    if not pomp.exists() or pomp.stat().st_size == 0:
        pomp.write_text("[]", encoding="utf-8")
        print("[Mercurius] Lege vragen.json geïnitialiseerd.")

    try:
        data = pomp.read_text(encoding="utf-8")
        quiz["vragen"] = json.loads(data)
        print(f"[Mercurius] {len(quiz['vragen'])} vragen succesvol geladen.")
    except Exception as e:
        quiz["vragen"] = []
        print(f"[⚠️] Fout bij laden vragen.json: {e}")

laad_vragen()

# ── Kahoot Score Formule ─────────────────────────────────────────────────────
def bereken_punten(is_correct: bool, reistijd_ms: float, tijd_limiet_sec: float) -> int:
    if not is_correct:
        return 0
    
    tijd_limiet_ms = tijd_limiet_sec * 1000
    verhouding = min(max(reistijd_ms / tijd_limiet_ms, 0.0), 1.0)
    punten = 1000 * (1 - verhouding * 0.5)
    return round(punten)

# ── Berichten Helpers ────────────────────────────────────────────────────────
async def broadcast_spelers(data: dict):
    for naam, ws in list(spelers.items()):
        try:
            await ws.send_json(data)
        except Exception:
            spelers.pop(naam, None)

async def stuur_admin(data: dict):
    global admin_ws
    if admin_ws:
        try:
            await admin_ws.send_json(data)
        except Exception:
            admin_ws = None

async def update_admin_dashboard():
    dashboard_data = {
        "actie": "status_update",
        "status": quiz["status"],
        "spelers": [{"naam": n, "score": quiz["scores"].get(n, 0)} for n in spelers],
        "huidige_vraag_index": quiz["huidige_vraag"],
        "totaal_vragen": len(quiz["vragen"]),
        "aantal_antwoorden": len(quiz["antwoorden"])
    }
    await stuur_admin(dashboard_data)

# ── Automatische Timer Logica ────────────────────────────────────────────────
async def start_nieuwe_vraag_proces(index: int):
    quiz["status"] = QuizState.VRAAG_TONEN
    quiz["huidige_vraag"] = index
    v = quiz["vragen"][index]

    # Vraag tonen aan admin en beamer
    admin_data = {
        "actie": "toon_vraag", 
        "vraag": v["vraag"], 
        "opties": v["opties"], 
        "tijd": v["tijd_limiet"]
    }
    await stuur_admin(admin_data)
    await broadcast_spelers({"actie": "vraag_voorbereiden"})
    await update_admin_dashboard()

    # Leespauze
    await asyncio.sleep(4)

    # Timer starten
    if quiz["status"] == QuizState.VRAAG_TONEN and quiz["huidige_vraag"] == index:
        quiz["status"] = QuizState.VRAAG_ACTIEF
        quiz["antwoorden"] = {}
        quiz["start_tijd"] = time.time() * 1000
        
        await broadcast_spelers({"actie": "timer_start"})
        await stuur_admin({"actie": "timer_loopt"})
        await update_admin_dashboard()

# ── API & Pagina Routes ──────────────────────────────────────────────────────
@app.get("/")
async def speler_pagina():
    return HTMLResponse((Path(__file__).parent / "speler.html").read_text(encoding="utf-8"))

@app.get("/admin")
async def admin_pagina():
    return HTMLResponse((Path(__file__).parent / "admin.html").read_text(encoding="utf-8"))

@app.get("/beamer")
async def beamer_pagina():
    return HTMLResponse((Path(__file__).parent / "beamer.html").read_text(encoding="utf-8"))

@app.get("/api/vragen")
def geef_vragen():
    laad_vragen()
    return quiz["vragen"]

@app.post("/api/vragen")
def opslaan_vragen(nieuwe_vragen: list = Body(...)):
    try:
        content = json.dumps(nieuwe_vragen, indent=2, ensure_ascii=False)
        pomp.write_text(content, encoding="utf-8")
        quiz["vragen"] = nieuwe_vragen
        return {"status": "success", "melding": "Vragen opgeslagen"}
    except Exception as e:
        return {"status": "error", "melding": str(e)}

# ── WebSockets ──────────────────────────────────────────────────────────────
@app.websocket("/ws/speler")
async def ws_speler(websocket: WebSocket):
    await websocket.accept()
    nickname = None
    try:
        while True:
            raw = await websocket.receive_text()
            data = json.loads(raw)
            actie = data.get("actie")
            
            if actie == "aanmelden":
                nickname = data.get("naam", "").strip()[:15]
                if not nickname or nickname in spelers:
                    await websocket.close(code=1008)
                    return
                
                spelers[nickname] = websocket
                quiz["scores"][nickname] = quiz["scores"].get(nickname, 0)
                await update_admin_dashboard()
                
            elif actie == "insturen_antwoord" and quiz["status"] == QuizState.VRAAG_ACTIEF:
                if nickname and nickname not in quiz["antwoorden"]:
                    reistijd = time.time() * 1000 - quiz["start_tijd"]
                    quiz["antwoorden"][nickname] = {
                        "optie": int(data["keuze"]), 
                        "tijd_ms": reistijd
                    }
                    await update_admin_dashboard()
                    
    except WebSocketDisconnect:
        if nickname:
            spelers.pop(nickname, None)
        await update_admin_dashboard()

@app.websocket("/ws/admin")
async def ws_admin(websocket: WebSocket):
    global admin_ws
    await websocket.accept()
    admin_ws = websocket
    await update_admin_dashboard()
    
    try:
        while True:
            raw = await websocket.receive_text()
            data = json.loads(raw)
            actie = data.get("actie")
            
            if actie == "start_quiz":
                if quiz["vragen"]:
                    asyncio.create_task(start_nieuwe_vraag_proces(0))
            
            elif actie == "stop_vraag":
                if quiz["status"] == QuizState.VRAAG_ACTIEF:
                    quiz["status"] = QuizState.VRAAG_UITSLAG
                    v = quiz["vragen"][quiz["huidige_vraag"]]
                    correcte = v["correct_index"]
                    
                    for naam, antwoord in quiz["antwoorden"].items():
                        is_c = (antwoord["optie"] == correcte)
                        pts = bereken_punten(is_c, antwoord["tijd_ms"], v["tijd_limiet"])
                        quiz["scores"][naam] += pts
                    
                    ranking = sorted(quiz["scores"].items(), key=lambda x: x[1], reverse=True)
                    admin_res = {
                        "actie": "vraag_uitslag",
                        "ranking": [{"naam": n, "score": s} for n, s in ranking]
                    }
                    await stuur_admin(admin_res)
                    await update_admin_dashboard()
            
            elif actie == "volgende_vraag":
                if quiz["status"] == QuizState.VRAAG_UITSLAG:
                    idx = quiz["huidige_vraag"] + 1
                    if idx < len(quiz["vragen"]):
                        asyncio.create_task(start_nieuwe_vraag_proces(idx))
                    else:
                        quiz["status"] = QuizState.EIND_UITSLAG
                    await update_admin_dashboard()
                    
            elif actie == "reset":
                quiz["status"] = QuizState.LOBBY
                quiz["scores"] = {n: 0 for n in spelers}
                quiz["antwoorden"] = {}
                await update_admin_dashboard()
                
    except WebSocketDisconnect:
        admin_ws = None

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8000))
    uvicorn.run("main:app", host="0.0.0.0", port=port)
