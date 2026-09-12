#!/usr/bin/env python3
"""
Offline test: serves a fixture page shaped exactly like the real Just Eat form
HTML, plus a fake Telegram API, and drives watch.py --once through the cases
that matter — including the one that went wrong in production, where a city
advertises an e-bike job posting while the Step-4 vehicle dropdown still says
"Driver E-Bike": false.

Runs in a temp directory against a copy of watch.py, so it can never touch the
real config.json or state.json.

    python selftest.py
"""
import json, os, shutil, subprocess, sys, tempfile, threading, time
import http.server, socketserver
from pathlib import Path

SRC = Path(__file__).parent
PORT = 8788
SENT = []

# Real option keys, copied verbatim from the live page.
ALL_VEHICLES = [
    "Driver Bike", "Company Bike", "Driver E-Bike", "Company E-Bike",
    "Driver Scooter", "Company Scooter", "Driver E-Roller",
    "Driver Car / Kombi", "Company Car / Kombi", "Driver Buffer Vehicle",
]

# What the fixture currently serves. Tests mutate this between runs.
#   dropdown -> the Step-4 vehicle map
#   postings -> job_postings adverts, as (shift, vehicle) pairs
CURRENT = {
    "genoa": {"dropdown": ["Driver Scooter"], "postings": []},
    "pavia": {"dropdown": ["Driver E-Bike"], "postings": []},
}

WORK = Path(tempfile.mkdtemp(prefix="je-selftest-"))
shutil.copy2(SRC / "watch.py", WORK / "watch.py")


def make_html() -> str:
    def city(name, slug, coid, cid, state):
        return {
            "id": cid,
            "name": name,
            "slug": slug,
            "city_option_id": coid,
            "active": True,
            "form_questions": [
                # Age question: two options, must never be mistaken for vehicles.
                {"form_question": {"data_key": "age"}, "form_step": "vehicle",
                 "options": {"No": True, "Yes": True}},
                # The real vehicle question, identified by data_key.
                {"form_question": {"data_key": "vehicle_type"}, "form_step": "vehicle",
                 "options": {v: (v in state["dropdown"]) for v in ALL_VEHICLES}},
                # Shift question.
                {"form_question": {"data_key": "picked_shift"}, "form_step": "vehicle",
                 "options": {"Full weekend": True, "Weekdays lunch": False}},
            ],
            "job_postings": (
                [{"key": "blk", "layout": "job_posting_0", "attributes": {"postings": [
                    {"key": f"p{i}", "layout": "posting",
                     "attributes": {"option_1": shift, "option_2": vehicle}}
                    for i, (shift, vehicle) in enumerate(state["postings"])
                ]}}]
                if state["postings"] else []
            ),
        }

    lang = {"id": 30, "code": "en", "name": "English", "city_options": [
        city("Genoa", "genoa", 202, 592, CURRENT["genoa"]),
        # Just Eat really does ship the same city twice under both names.
        city("Genova", "genova", 202, 568, CURRENT["genoa"]),
        city("Pavia", "pavia", 310, 700, CURRENT["pavia"]),
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

CONFIG = {
    "telegram_token": "TESTTOKEN", "chat_id": "123",
    "telegram_api_base": f"http://127.0.0.1:{PORT}",
    "cities": ["Genoa", "Genova"],
    "vehicle_pattern": "e-?\\s?bike", "vehicle_label": "e-bike",
    "alert_on_any_city": False, "interval_minutes": 30,
    "notify_on_any_change": False, "heartbeat_hours": 0,
}
(WORK / "config.json").write_text(json.dumps(CONFIG, indent=2))

env = dict(os.environ, JE_FORM_URL=f"http://127.0.0.1:{PORT}/en/courier/form")


def run(label):
    r = subprocess.run([sys.executable, str(WORK / "watch.py"), "--once"],
                       capture_output=True, text=True, env=env, encoding="utf-8")
    out = (r.stdout or r.stderr or "").strip()
    print(f"--- {label} (rc={r.returncode})\n    {out.splitlines()[-1] if out else ''}")
    assert r.returncode == 0, out


# ---- parser tests against the fixture shape ------------------------------
sys.path.insert(0, str(SRC))
import watch  # noqa: E402

parsed = watch.city_signals(make_html())
assert set(parsed) == {"coid:202", "coid:310"}, parsed.keys()

genoa = parsed["coid:202"]
assert genoa["dropdown"] == ["Driver Scooter"], genoa
assert sorted(genoa["names"]) == ["Genoa", "Genova"], genoa
assert genoa["slug"] == "genoa", genoa
print("--- parser: Genoa/Genova collapse into one city by city_option_id  OK")

assert watch.vehicle_question(
    {"form_questions": [{"form_question": {"data_key": "age"},
                         "options": {"Yes": True, "No": False}}]}
) is None, "a Yes/No question must not be mistaken for the vehicle question"
print("--- parser: ignores the Yes/No and shift questions  OK")

CURRENT["genoa"]["postings"] = [("Friday and weekend evenings", "Driver E-Bike")]
p = watch.city_signals(make_html())["coid:202"]
assert p["postings"] == [{"vehicle": "Driver E-Bike",
                          "shift": "Friday and weekend evenings"}], p
assert "Driver E-Bike" in p["offered"] and "Driver E-Bike" not in p["dropdown"]
CURRENT["genoa"]["postings"] = []
print("--- parser: reads job_postings as a second, independent signal  OK")

pat = watch.vehicle_matcher(CONFIG)
assert pat.search("Driver E-Bike") and pat.search("Company E-Bike")
assert not pat.search("Driver Bike"), "plain pedal bike must not count as e-bike"
assert not pat.search("Driver E-Roller")
print("--- matcher: e-bike only, not plain Bike, not E-Roller  OK")

# ---- behaviour tests -----------------------------------------------------
run("1. baseline: Genoa scooter only")
assert not SENT, SENT

run("2. unchanged")
assert not SENT, SENT

CURRENT["genoa"]["dropdown"] = ["Driver Scooter", "Driver Bike"]
run("3. plain pedal bike appears — must stay silent")
assert not SENT, f"pedal bike is not an e-bike: {SENT}"

# THE REGRESSION: a job posting advertises an e-bike while the dropdown hides it.
CURRENT["genoa"]["postings"] = [("Friday and weekend evenings", "Driver E-Bike")]
run("4. e-bike advertised via job posting only")
assert len(SENT) == 1, SENT
text = SENT[0]["text"]
assert "E-BIKE IS OPEN IN GENOA" in text, text
assert "Friday and weekend evenings" in text, text
assert "city=genoa" in text, text
print("    ->", text.replace("\n", " | ")[:170])

run("5. still advertised (no duplicate)")
assert len(SENT) == 1, SENT

CURRENT["genoa"]["postings"] = []
run("6. posting withdrawn")
assert len(SENT) == 2 and "closed again" in SENT[1]["text"], SENT

CURRENT["genoa"]["dropdown"] = ["Driver Scooter", "Driver E-Bike"]
run("7. e-bike appears in the form dropdown instead")
assert len(SENT) == 3 and "E-BIKE IS OPEN IN GENOA" in SENT[2]["text"], SENT
assert "Application form offers" in SENT[2]["text"], SENT[2]["text"]

CURRENT["genoa"]["dropdown"] = ["Driver Scooter"]
run("8. closed again")
assert len(SENT) == 4, SENT

CURRENT["genoa"]["dropdown"] = ["Driver Scooter", "Driver Car / Kombi"]
run("9. unrelated vehicle added — silent by default")
assert len(SENT) == 4, f"Car/Kombi churn must not notify: {SENT[4:]}"

CURRENT["pavia"]["postings"] = [("Full weekend", "Driver E-Bike")]
run("10. e-bike in an unwatched city stays silent")
assert len(SENT) == 4, f"should not alert for Pavia: {SENT[4:]}"

# ---- state migration -----------------------------------------------------
# A schema-1 state file (keyed by display name, with a "bike" flag) must not
# replay an alert the user already received.
SENT.clear()
CURRENT["genoa"] = {"dropdown": ["Driver Scooter", "Driver E-Bike"], "postings": []}
(WORK / "state.json").write_text(json.dumps({
    "consecutive_failures": 0,
    "cities": {"Genoa": {"available": ["Driver E-Bike", "Driver Scooter"],
                         "bike": True, "slug": "genoa"}},
    "last_check": "2026-09-12T00:00:00+00:00",
}, indent=2))
run("11. upgrade from schema-1 state while e-bike is already open")
assert not SENT, f"must not re-alert on an opening already reported: {SENT}"

migrated = json.loads((WORK / "state.json").read_text(encoding="utf-8"))
assert migrated["schema"] == 2 and migrated["cities"]["coid:202"]["open"] is True, migrated
print("--- migration: schema-1 state upgrades without replaying alerts  OK")

srv.shutdown()
shutil.rmtree(WORK, ignore_errors=True)
print("\nALL CHECKS PASSED — the posting-only opening that was missed in "
      "production now fires, and pedal-bike/Car-Kombi churn stays quiet.")
