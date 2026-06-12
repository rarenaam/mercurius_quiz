from typing import Any
from fastapi import FastAPI, WebSocket, WebSocketDisconnect, Body, HTTPException, Depends
from fastapi.responses import HTMLResponse
from fastapi.security import HTTPBasic, HTTPBasicCredentials
import time
import asyncio
import json
import os
import uvicorn
from pathlib import Path

# Initialiseer de FastAPI applicatie
app = FastAPI()
security = HTTPBasic()

# Het centrale pad naar het bestand waar de vragen in staan opgeslagen
pomp = Path(__file__).parent / "vragen.json"

# ── Instellingen ─────────────────────────────────────────────────────────────
ADMIN_PASSWORD = os.environ.get("ADMIN_PASSWORD", "verander_dit_wachtwoord")
ADMIN_USERNAME = os.environ.get("ADMIN_USERNAME", "admin")

# ── Quiz Statussen ───────────────────────────────────────────────────────────
class QuizState:
    LOBBY = "lobby"
    VRAAG_TONEN = "vraag_tonen"
    VRAAG_ACTIEF = "vraag_actief"
    VRAAG_UITSLAG = "vraag_uitslag"
    EIND_UITSLAG = "eind_uitslag"

# Het centrale quiz-object dat de hele staat van het spel bijhoudt
quiz: dict[str, str | list[Any] | int | None | dict[Any, Any]] = {
    "status": QuizState.LOBBY,
    "vragen": [],
    "huidige_vraag": 0,
    "start_tijd": None,
    "antwoorden": {},
    "scores": {},
}

# Bijhouden van actieve WebSocket verbindingen
spelers: dict[str, WebSocket] = {}
admin_ws: WebSocket | None = None
beamer_ws: list[WebSocket] = []

# Vlag die bijhoudt of er al een actieve admin-sessie is via WebSocket.
admin_is_bezet: bool = False

# ── Hulpfuncties: Laden van data ─────────────────────────────────────────────
def laad_vragen() -> None:
    """Laadt de vragen uit vragen.json. Maakt een leeg bestand aan als het ontbreekt."""
    if not pomp.exists() or pomp.stat().st_size == 0:
        pomp.write_text("[]", encoding="utf-8")
        print("[Systeem] Lege vragen.json aangemaakt.")

    try:
        data = pomp.read_text(encoding="utf-8")
        quiz["vragen"] = json.loads(data)
        print(f"[Systeem] {len(quiz['vragen'])} vragen succesvol geladen.")
    except Exception as e:
        quiz["vragen"] = []
        print(f"[⚠️] Fout bij laden vragen.json: {e}")

# Roep de functie direct aan bij het opstarten
laad_vragen()

# ── Admin Authenticatie ───────────────────────────────────────────────────────
def check_admin(credentials: HTTPBasicCredentials = Depends(security)) -> str:
    """Controleert de inloggegevens van de admin via HTTP Basic Auth."""
    gebruikersnaam_correct = credentials.username == ADMIN_USERNAME
    wachtwoord_correct = credentials.password == ADMIN_PASSWORD

    if not (gebruikersnaam_correct and wachtwoord_correct):
        raise HTTPException(
            status_code=401,
            detail="Verkeerde inloggegevens",
            headers={"WWW-Authenticate": "Basic"},
        )
    return credentials.username

# ── Kahoot Score Logica ──────────────────────────────────────────────────────
def bereken_punten(is_correct: bool, reistijd_ms: float, tijd_limiet_sec: float) -> int:
    """Berekent punten op basis van snelheid en correctheid."""
    if not is_correct:
        return 0

    tijd_limiet_ms = tijd_limiet_sec * 1000
    verhouding = min(max(reistijd_ms / tijd_limiet_ms, 0.0), 1.0)
    punten = 1000 * (1 - verhouding * 0.5)
    return round(punten)

# ── WebSocket Broadcast Functies ─────────────────────────────────────────────
async def broadcast_spelers(data: dict) -> None:
    """Verstuurt een bericht naar alle verbonden spelers."""
    for naam, ws in list(spelers.items()):
        try:
            await ws.send_json(data)
        except Exception:
            spelers.pop(naam, None)

async def stuur_admin(data: dict) -> None:
    """Verstuurt een bericht naar de admin WebSocket."""
    global admin_ws
    if admin_ws:
        try:
            await admin_ws.send_json(data)
        except Exception:
            admin_ws = None

async def update_admin_dashboard() -> None:
    """Stuurt de huidige quiz-status naar de admin en naar alle beamers."""
    dashboard_data = {
        "actie": "status_update",
        "status": quiz["status"],
        "spelers": [{"naam": n, "score": quiz["scores"].get(n, 0)} for n in spelers],
        "huidige_vraag_index": quiz["huidige_vraag"],
        "totaal_vragen": len(quiz["vragen"]),
        "aantal_antwoorden": len(quiz["antwoorden"]),
    }

    await stuur_admin(dashboard_data)

    for ws in list(beamer_ws):
        try:
            await ws.send_json(dashboard_data)
        except Exception:
            if ws in beamer_ws:
                beamer_ws.remove(ws)

# ── Quiz Logica: Starten van een nieuwe vraag ────────────────────────────────
async def start_nieuwe_vraag_proces(index: int) -> None:
    """Handelt het proces af voor het tonen van een nieuwe vraag en de timer."""
    quiz["status"] = QuizState.VRAAG_TONEN
    quiz["huidige_vraag"] = index
    v = quiz["vragen"][index]

    vraag_info = {
        "actie": "toon_vraag",
        "vraag": v["vraag"],
        "opties": v["opties"],
        "tijd": v["tijd_limiet"],
    }
    await stuur_admin(vraag_info)
    for ws in list(beamer_ws):
        try:
            await ws.send_json(vraag_info)
        except Exception:
            if ws in beamer_ws:
                beamer_ws.remove(ws)

    await broadcast_spelers({"actie": "vraag_voorbereiden"})
    await update_admin_dashboard()

    # Geef spelers 4 seconden tijd om de vraag te lezen
    await asyncio.sleep(4)

    # Start de feitelijke timer — alleen als de status nog niet is veranderd
    if quiz["status"] == QuizState.VRAAG_TONEN and quiz["huidige_vraag"] == index:
        quiz["status"] = QuizState.VRAAG_ACTIEF
        quiz["antwoorden"] = {}
        quiz["start_tijd"] = time.time() * 1000

        await broadcast_spelers({"actie": "timer_start"})
        await stuur_admin({"actie": "timer_loopt"})

        timer_msg = {"actie": "timer_loopt", "tijd": v["tijd_limiet"]}
        for ws in list(beamer_ws):
            try:
                await ws.send_json(timer_msg)
            except Exception:
                if ws in beamer_ws:
                    beamer_ws.remove(ws)

        await update_admin_dashboard()

# ── API & Pagina Routes ──────────────────────────────────────────────────────
@app.get("/")
async def speler_pagina() -> HTMLResponse:
    return HTMLResponse((Path(__file__).parent / "speler.html").read_text(encoding="utf-8"))

@app.get("/admin")
async def admin_pagina(user: str = Depends(check_admin)) -> HTMLResponse:
    """Laadt de admin-pagina na succesvolle authenticatie."""
    try:
        file_path = Path(__file__).parent / "admin.html"
        return HTMLResponse(file_path.read_text(encoding="utf-8"))
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Kan admin.html niet laden: {e}")

@app.get("/beamer")
async def beamer_pagina() -> HTMLResponse:
    return HTMLResponse((Path(__file__).parent / "beamer.html").read_text(encoding="utf-8"))

@app.get("/api/vragen")
def geef_vragen() -> list:
    laad_vragen()
    return quiz["vragen"]

@app.post("/api/vragen")
def opslaan_vragen(nieuwe_vragen: list = Body(...)) -> dict:
    try:
        content = json.dumps(nieuwe_vragen, indent=2, ensure_ascii=False)
        pomp.write_text(content, encoding="utf-8")
        quiz["vragen"] = nieuwe_vragen
        return {"status": "success", "melding": "Vragen opgeslagen"}
    except Exception as e:
        return {"status": "error", "melding": str(e)}

# ── WebSocket Handlers ───────────────────────────────────────────────────────
@app.websocket("/ws/speler")
async def ws_speler(websocket: WebSocket) -> None:
    await websocket.accept()
    nickname: str | None = None
    try:
        while True:
            raw = await websocket.receive_text()
            try:
                data = json.loads(raw)
            except json.JSONDecodeError:
                continue  # Sla ongeldige JSON over om crashes te voorkomen
            
            actie = data.get("actie")

            if actie == "aanmelden":
                nickname = data.get("naam", "").strip()[:15]
                if not nickname:
                    await websocket.close(code=1008)
                    return
                
                if nickname in spelers:
                    oude_ws = spelers[nickname]
                    try:
                        await oude_ws.close(code=1000)
                    except Exception:
                        pass
                
                spelers[nickname] = websocket
                quiz["scores"].setdefault(nickname, 0)
                await update_admin_dashboard()

            elif actie == "insturen_antwoord" and quiz["status"] == QuizState.VRAAG_ACTIEF:
                if nickname and nickname not in quiz["antwoorden"]:
                    try:
                        keuze_optie = int(data["keuze"])
                        reistijd = time.time() * 1000 - quiz["start_tijd"]
                        quiz["antwoorden"][nickname] = {
                            "optie": keuze_optie,
                            "tijd_ms": reistijd,
                        }
                        await update_admin_dashboard()
                    except (ValueError, KeyError):
                        continue  # Sla over als de meegestuurde 'keuze' corrupt is

    except WebSocketDisconnect:
        if nickname:
            await asyncio.sleep(10)
            if nickname in spelers and spelers[nickname] == websocket:
                spelers.pop(nickname, None)
                await update_admin_dashboard()

@app.websocket("/ws/beamer")
async def ws_beamer(websocket: WebSocket) -> None:
    """WebSocket endpoint voor grote beamerschermen."""
    await websocket.accept()
    beamer_ws.append(websocket)
    await update_admin_dashboard()
    try:
        while True:
            # Houdt de verbinding open en luistert naar eventuele berichten
            await websocket.receive_text()
    except WebSocketDisconnect:
        if websocket in beamer_ws:
            beamer_ws.remove(websocket)

@app.websocket("/ws/admin")
async def ws_admin(websocket: WebSocket) -> None:
    """
    WebSocket voor de admin. Beheert de exclusiviteit van de quizmaster-sessie.
    Voorkomt lockouts bij het herladen van de HTTP pagina.
    """
    global admin_ws, admin_is_bezet
    
    if admin_is_bezet:
        await websocket.accept()
        await websocket.close(code=1008, reason="Er is al een quizmaster actief.")
        return

    await websocket.accept()
    admin_ws = websocket
    admin_is_bezet = True
    await update_admin_dashboard()

    try:
        while True:
            raw = await websocket.receive_text()
            try:
                data = json.loads(raw)
            except json.JSONDecodeError:
                continue
                
            actie = data.get("actie")

            if actie == "start_quiz":
                # Anti-spam: start alleen als de quiz nog in de lobby staat
                if quiz["vragen"] and quiz["status"] == QuizState.LOBBY:
                    asyncio.create_task(start_nieuwe_vraag_proces(0))

            elif actie == "stop_vraag":
                if quiz["status"] == QuizState.VRAAG_ACTIEF:
                    quiz["status"] = QuizState.VRAAG_UITSLAG
                    v = quiz["vragen"][quiz["huidige_vraag"]]
                    correcte = v["correct_index"]

                    for naam, antwoord in quiz["antwoorden"].items():
                        is_c = antwoord["optie"] == correcte
                        pts = bereken_punten(is_c, antwoord["tijd_ms"], v["tijd_limiet"])
                        quiz["scores"][naam] = quiz["scores"].get(naam, 0) + pts

                    ranking = sorted(
                        quiz["scores"].items(), key=lambda x: x[1], reverse=True
                    )
                    resultaat_data = {
                        "actie": "vraag_uitslag",
                        "correct_index": correcte,
                        "antwoorden": quiz["antwoorden"],
                        "ranking": [{"naam": n, "score": s} for n, s in ranking],
                    }

                    await stuur_admin(resultaat_data)
                    for ws in list(beamer_ws):
                        try:
                            await ws.send_json(resultaat_data)
                        except Exception:
                            if ws in beamer_ws:
                                beamer_ws.remove(ws)

                    await update_admin_dashboard()

            elif actie == "volgende_vraag":
                if quiz["status"] == QuizState.VRAAG_UITSLAG:
                    idx = quiz["huidige_vraag"] + 1
                    if idx < len(quiz["vragen"]):
                        asyncio.create_task(start_nieuwe_vraag_proces(idx))
                    else:
                        quiz["status"] = QuizState.EIND_UITSLAG
                        eind_ranking = sorted(
                            quiz["scores"].items(), key=lambda x: x[1], reverse=True
                        )
                        eind_data = {
                            "actie": "eind_uitslag",
                            "ranking": [{"naam": n, "score": s} for n, s in eind_ranking],
                        }
                        for ws in list(beamer_ws):
                            try:
                                await ws.send_json(eind_data)
                            except Exception:
                                if ws in beamer_ws:
                                    beamer_ws.remove(ws)
                    await update_admin_dashboard()

            elif actie == "reset":
                quiz["status"] = QuizState.LOBBY
                quiz["scores"] = {n: 0 for n in spelers}
                quiz["antwoorden"] = {}
                await broadcast_spelers({"actie": "quiz_reset"})
                await update_admin_dashboard()

    except WebSocketDisconnect:
        admin_ws = None
        admin_is_bezet = False

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8000))
    uvicorn.run("main:app", host="0.0.0.0", port=port)
