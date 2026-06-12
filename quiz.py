from typing import Any

import qrcode

# De link die je van Ngrok krijgt (vervang dit telkens als je een nieuwe sessie start)
url = "https://moving-porridge-january.ngrok-free.dev"

qr = qrcode.QRCode(version=1, box_size=10, border=5)
qr.add_data(url)
qr.make(fit=True)

img = qr.make_image(fill_color="black", back_color="white")
img.save("qr_code.png")
print("QR-code is opgeslagen als qr_code.png!")

from fastapi import FastAPI, WebSocket, WebSocketDisconnect, Body
from fastapi.responses import HTMLResponse
import time
import asyncio
import json
from pathlib import Path

app = FastAPI()

# Het centrale pad naar je vragenbestand
pomp = Path(__file__).parent / "vragen.json"


# ── Quiz States ──────────────────────────────────────────────────────────────
class QuizState:
    LOBBY = "lobby"
    VRAAG_TONEN = "vraag_tonen"  # Vraag op het bord, mobiel bevroren
    VRAAG_ACTIEF = "vraag_actief"  # Timer loopt, spelers kunnen stemmen
    VRAAG_UITSLAG = "vraag_uitslag"  # Juiste antwoord + score van deze ronde
    EIND_UITSLAG = "eind_uitslag"  # Eindpodium


# Het centrale quiz-object
quiz: dict[str, str | list[Any] | int | None | dict[Any, Any]] = {
    "status": QuizState.LOBBY,
    "vragen": [],
    "huidige_vraag": 0,  # Index van de actieve vraag
    "start_tijd": None,  # Milliseconden wanneer de timer start
    "antwoorden": {},  # { nickname: {"optie": int, "tijd_ms": int} }
    "scores": {},  # { nickname: totaal_punten }
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
        quiz["vragen"] = json.loads(pomp.read_text(encoding="utf-8"))
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
        except:
            spelers.pop(naam, None)


async def stuur_admin(data: dict):
    global admin_ws
    if admin_ws:
        try:
            await admin_ws.send_json(data)
        except:
            admin_ws = None


async def update_admin_dashboard():
    await stuur_admin({
        "actie": "status_update",
        "status": quiz["status"],
        "spelers": [{"naam": n, "score": quiz["scores"].get(n, 0)} for n in spelers],
        "huidige_vraag_index": quiz["huidige_vraag"],
        "totaal_vragen": len(quiz["vragen"]),
        "aantal_antwoorden": len(quiz["antwoorden"])
    })


# ── AUTOMATISCHE TIMER LOGICA ────────────────────────────────────────────────
async def start_nieuwe_vraag_proces(index: int):
    """Toont de vraag en start na 4 seconden leespauze automatisch de timer."""
    quiz["status"] = QuizState.VRAAG_TONEN
    quiz["huidige_vraag"] = index
    v = quiz["vragen"][index]

    # 1. Stuur de vraag naar de beamer/admin en zet mobieltjes in de wachtstand
    await stuur_admin({"actie": "toon_vraag", "vraag": v["vraag"], "opties": v["opties"], "tijd": v["tijd_limiet"]})
    await broadcast_spelers({"actie": "vraag_voorbereiden"})
    await update_admin_dashboard()

    # 2. Geef de spelers exact 4 seconden de tijd om de vraag rustig te lezen
    await asyncio.sleep(4)

    # 3. Activeer de timer en de knoppen automatisch
    if quiz["status"] == QuizState.VRAAG_TONEN and quiz["huidige_vraag"] == index:
        quiz["status"] = QuizState.VRAAG_ACTIEF
        quiz["antwoorden"] = {}
        quiz["start_tijd"] = time.time() * 1000

        await broadcast_spelers({"actie": "timer_start"})
        await stuur_admin({"actie": "timer_loopt"})
        await update_admin_dashboard()


# ── Pages & API Routes ───────────────────────────────────────────────────────
@app.get("/")
async def speler_pagina():
    return HTMLResponse(Path("speler.html").read_text(encoding="utf-8"))

@app.get("/admin")
async def admin_pagina():
    return HTMLResponse(Path("admin.html").read_text(encoding="utf-8"))

@app.get("/beamer")
async def beamer_pagina():
    return HTMLResponse(Path("beamer.html").read_text(encoding="utf-8"))

@app.websocket("/ws/beamer")
async def ws_beamer(websocket: WebSocket):
    await websocket.accept()
    beamer_ws.append(websocket)
    try:
        while True:
            await websocket.receive_text()
    except WebSocketDisconnect:
        beamer_ws.remove(websocket)

@app.get("/api/vragen")
def geef_vragen():
    laad_vragen()
    return quiz["vragen"]


@app.post("/api/vragen")
def opslaan_vragen(nieuwe_vragen: list = Body(...)):
    try:
        pomp.write_text(json.dumps(nieuwe_vragen, indent=2, ensure_ascii=False), encoding="utf-8")
        quiz["vragen"] = nieuwe_vragen
        print(f"[Mercurius] {len(nieuwe_vragen)} vragen succesvol opgeslagen via de Admin Editor!")
        return {"status": "success", "melding": "Vragen succesvol opgeslagen!"}
    except Exception as e:
        return {"status": "error", "melding": str(e)}


# ── WebSocket Spelers ────────────────────────────────────────────────────────
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
                if nickname not in quiz["scores"]:
                    quiz["scores"][nickname] = 0

                if quiz["status"] in [QuizState.VRAAG_TONEN, QuizState.VRAAG_ACTIEF]:
                    await websocket.send_json({"actie": "vraag_voorbereiden", "score": quiz["scores"][nickname]})
                else:
                    await websocket.send_json({"actie": "wachten", "score": quiz["scores"][nickname]})

                await update_admin_dashboard()

            elif actie == "insturen_antwoord" and quiz["status"] == QuizState.VRAAG_ACTIEF:
                if nickname and nickname not in quiz["antwoorden"]:
                    gekozen_optie = int(data["keuze"])
                    nu_ms = time.time() * 1000
                    reactietijd = nu_ms - quiz["start_tijd"]

                    quiz["antwoorden"][nickname] = {
                        "optie": gekozen_optie,
                        "tijd_ms": reactietijd
                    }
                    await update_admin_dashboard()

    except WebSocketDisconnect:
        if nickname:
            spelers.pop(nickname, None)
        await update_admin_dashboard()


# ── WebSocket Admin ──────────────────────────────────────────────────────────
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
                    correcte_index = v["correct_index"]

                    ronde_resultaat = {}
                    for naam, antwoord in quiz["antwoorden"].items():
                        is_correct = (antwoord["optie"] == correcte_index)
                        punten = bereken_punten(is_correct, antwoord["tijd_ms"], v["tijd_limiet"])
                        quiz["scores"][naam] += punten
                        ronde_resultaat[naam] = {"punten": punten, "correct": is_correct}

                    ranking = sorted(quiz["scores"].items(), key=lambda x: x[1], reverse=True)

                    await stuur_admin({
                        "actie": "vraag_uitslag",
                        "correct_index": correcte_index,
                        "ranking": [{"naam": n, "score": s} for n, s in ranking],
                        "antwoorden": quiz["antwoorden"]
                    })

                    for naam, ws in spelers.items():
                        p_res = ronde_resultaat.get(naam, {"punten": 0, "correct": False})
                        try:
                            await ws.send_json({
                                "actie": "ronde_uitslag",
                                "was_correct": p_res["correct"],
                                "behaalde_punten": p_res["punten"],
                                "was_tijd_om": (naam not in quiz["antwoorden"]),
                                "score": quiz["scores"][naam]
                            })
                        except:
                            pass

                    # ── DIT WAS DE ONTBREKENDE SCHAKEL ───────────────────────
                    # Hiermee weet de admin-pagina dat de status écht is aangepast!
                    await update_admin_dashboard()

            elif actie == "volgende_vraag":
                if quiz["status"] == QuizState.VRAAG_UITSLAG:
                    volgende_index = quiz["huidige_vraag"] + 1
                    if volgende_index < len(quiz["vragen"]):
                        asyncio.create_task(start_nieuwe_vraag_proces(volgende_index))
                    else:
                        quiz["status"] = QuizState.EIND_UITSLAG
                        ranking = sorted(quiz["scores"].items(), key=lambda x: x[1], reverse=True)
                        await stuur_admin(
                            {"actie": "eind_uitslag", "ranking": [{"naam": n, "score": s} for n, s in ranking]})
                        await broadcast_spelers({"actie": "quiz_reset"})
                    await update_admin_dashboard()

            elif actie == "reset":
                quiz["status"] = QuizState.LOBBY
                quiz["scores"] = {n: 0 for n in spelers}
                quiz["antwoorden"] = {}
                await broadcast_spelers({"actie": "quiz_reset"})
                await update_admin_dashboard()

    except WebSocketDisconnect:
        admin_ws = None


if __name__ == "__main__":
    import uvicorn

    uvicorn.run("quiz:app", host="127.0.0.1", port=8000, reload=True)
