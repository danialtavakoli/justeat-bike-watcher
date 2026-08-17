#!/usr/bin/env python3
"""
Offline test: serves a fixture page shaped exactly like the real Just Eat form
HTML, plus a fake Telegram API, and drives watch.py --once through a full
scooter -> bike -> scooter cycle.

    python selftest.py
"""
import json, os, subprocess, sys, threading, time
import http.server, socketserver
from pathlib import Path

HERE = Path(__file__).parent
PORT = 8788
SENT = []

# Real option keys, copied verbatim from the live page.
ALL_VEHICLES = [
    "Driver Bike", "Company Bike", "Driver E-Bike", "Company E-Bike",
    "Driver Scooter", "Company Scooter", "Driver E-Roller",
    "Driver Car / Kombi", "Company Car / Kombi", "Driver Buffer Vehicle",
]
CURRENT = {"genoa": ["Driver Scooter"], "pavia": ["Driver E-Bike"]}


def make_html() -> str:
    def city(name, slug, avail):
        return {
            "id": 592 if slug == "genoa" else 700,
            "name": name, "slug": slug, "active": True,
            "form_questions": [
                {"form_step": "vehicle", "options": {"No": True, "Yes": True}},
                {"form_step": "vehicle",
                 "options": {v: (v in avail) for v in ALL_VEHICLES}},
                {"form_step": "vehicle",
                 "options": {"Full weekend": True, "Weekdays lunch": False}},
            ],
        }

    lang = {"id": 30, "code": "en", "name": "English", "city_options": [
        city("Genoa", "genoa", CURRENT["genoa"]),
        city("Pavia", "pavia", CURRENT["pavia"]),
    ]}
    return (
        "<!DOCTYPE html><html><head><script>\n"
        '    window.country = {"id":15,"code":"it"};\n'
        "    window.language = " + json.dumps(lang) + ";\n"
        '    window.apiUrl = "https://example.invalid";\n'
        "</script></head><body><div id=app></div></body></html>"
    )


class H(http.server.BaseHTTPRequestHandler):
    def log_message(self, *a): pass

    def _send(self, body: bytes, ctype: str):
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        self._send(make_html().encode(), "text/html; charset=utf-8")

    def do_POST(self):
        n = int(self.headers.get("Content-Length", 0))
        SENT.append(json.loads(self.rfile.read(n) or b"{}"))
        self._send(b'{"ok":true}', "application/json")


socketserver.TCPServer.allow_reuse_address = True
srv = socketserver.TCPServer(("127.0.0.1", PORT), H)
threading.Thread(target=srv.serve_forever, daemon=True).start()
time.sleep(0.3)

(HERE / "config.json").write_text(json.dumps({
    "telegram_token": "TESTTOKEN", "chat_id": "123",
    "telegram_api_base": f"http://127.0.0.1:{PORT}",
    "cities": ["Genoa"], "alert_on_any_city": False,
    "interval_minutes": 30, "notify_on_any_change": True, "heartbeat_hours": 0,
}, indent=2))
(HERE / "state.json").unlink(missing_ok=True)

env = dict(os.environ, JE_FORM_URL=f"http://127.0.0.1:{PORT}/en/courier/form")


def run(label):
    r = subprocess.run([sys.executable, str(HERE / "watch.py"), "--once"],
                       capture_output=True, text=True, env=env)
    out = (r.stdout or r.stderr).strip()
    print(f"--- {label} (rc={r.returncode})\n    {out.splitlines()[-1] if out else ''}")
    assert r.returncode == 0, out


# ---- parser test against the fixture shape -------------------------------
sys.path.insert(0, str(HERE))
os.environ["JE_FORM_URL"] = env["JE_FORM_URL"]
import watch  # noqa: E402

parsed = watch.city_vehicles(make_html())
assert parsed["Genoa"]["available"] == ["Driver Scooter"], parsed
assert parsed["Genoa"]["slug"] == "genoa", parsed
assert watch.has_bike(parsed["Genoa"]["available"]) is False
assert watch.has_bike(parsed["Pavia"]["available"]) is True
assert watch.vehicle_question(
    {"form_questions": [{"form_step": "vehicle", "options": {"Yes": True, "No": False}}]}
) is None, "yes/no question must not be mistaken for the vehicle question"
print("--- parser: picks the vehicle question, ignores Yes/No and shift questions  OK")

# ---- behaviour test ------------------------------------------------------
run("1. baseline: Genoa scooter only")
assert not SENT, SENT

run("2. unchanged")
assert not SENT, SENT

CURRENT["genoa"] = ["Driver Scooter", "Driver Bike"]
run("3. bike opens in Genoa")
assert len(SENT) == 1 and "BIKE IS OPEN IN GENOA" in SENT[0]["text"], SENT
assert "city=genoa" in SENT[0]["text"], SENT[0]
print("    ->", SENT[0]["text"].replace("\n", " | ")[:150])

run("4. bike still open (no duplicate)")
assert len(SENT) == 1, SENT

CURRENT["genoa"] = ["Driver Scooter"]
run("5. bike closes")
assert len(SENT) == 2 and "closed again" in SENT[1]["text"], SENT

CURRENT["genoa"] = ["Driver Scooter", "Driver Car / Kombi"]
run("6. unrelated vehicle added")
assert len(SENT) == 3 and "changed" in SENT[2]["text"], SENT

CURRENT["pavia"] = ["Driver E-Bike", "Driver Bike"]
run("7. change in an unwatched city stays silent")
assert len(SENT) == 3, f"should not alert for Pavia: {SENT[3:]}"

srv.shutdown()
print("\nALL CHECKS PASSED — 3 alerts, 0 false alarms, 0 noise from other cities.")
