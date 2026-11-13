"""Мониторинг рабочих мест компьютерного класса.

Запуск:  py server.py
Студенты: http://<IP>:<порт>/form?pc=N   (по QR-коду на столе)
Преподаватель: http://<IP>:<порт>/teacher

Только стандартная библиотека Python 3.9+, данные в SQLite (data.db).
"""
import csv
import io
import json
import os
import secrets
import socket
import sqlite3
import threading
from datetime import datetime
from http import cookies
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
STATIC_DIR = os.path.join(BASE_DIR, "static")
CONFIG_PATH = os.path.join(BASE_DIR, "config.json")
DB_PATH = os.path.join(BASE_DIR, "data.db")

DEFAULT_CONFIG = {
    "port": 8080,
    # Рассадка: ряды слева направо (как видит преподаватель со своего места),
    # в каждом ряду номера мест от стола преподавателя к задней стене.
    "layout": {
        "rows": [
            [1, 2, 3, 4, 5, 6, 7],
            [8, 9, 10, 11, 12, 13, 14],
            [15, 16, 17, 18, 19, 20, 21],
            [22, 23, 24, 25, 26, 27, 28, 29, 30],
        ],
        "aisles_after": [1, 3],   # проход после 1-го и после 3-го ряда
        "teacher_rows": [2, 3],   # стол преподавателя перед этими рядами
    },
    "teacher_password": "teacher",
    "room": "Аудитория",
    # Расписание звонков: по нему в форме заранее выбирается текущая пара
    "pairs": [
        {"n": 1, "start": "08:30", "end": "10:00"},
        {"n": 2, "start": "10:10", "end": "11:40"},
        {"n": 3, "start": "12:10", "end": "13:40"},
        {"n": 4, "start": "13:50", "end": "15:20"},
        {"n": 5, "start": "15:30", "end": "17:00"},
        {"n": 6, "start": "17:10", "end": "18:40"},
    ],
    # Если пусто — адрес для QR-кодов определяется автоматически по IP в сети
    "base_url": "",
}


def load_config():
    cfg = dict(DEFAULT_CONFIG)
    if os.path.exists(CONFIG_PATH):
        with open(CONFIG_PATH, encoding="utf-8") as f:
            cfg.update(json.load(f))
    else:
        with open(CONFIG_PATH, "w", encoding="utf-8") as f:
            json.dump(DEFAULT_CONFIG, f, ensure_ascii=False, indent=2)
    return cfg


CONFIG = load_config()
# номер места -> {"row": ряд, "place": место в ряду}
SEAT_POS = {pc: {"row": r + 1, "place": i + 1}
            for r, row in enumerate(CONFIG["layout"]["rows"])
            for i, pc in enumerate(row)}
SEATS = sorted(SEAT_POS)
SESSIONS = set()  # токены вошедших преподавателей
DB_LOCK = threading.Lock()


def db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    with db() as conn:
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS registrations (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                ts TEXT NOT NULL,
                pc INTEGER NOT NULL,
                fio TEXT NOT NULL,
                grp TEXT NOT NULL,
                pair INTEGER NOT NULL,
                state TEXT NOT NULL,          -- ok | problem
                comment TEXT NOT NULL DEFAULT '',
                resolved_at TEXT              -- когда преподаватель закрыл проблему
            );
            CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT);
            CREATE TABLE IF NOT EXISTS manual_busy (pc INTEGER PRIMARY KEY);
            """
        )
        conn.execute(
            "INSERT OR IGNORE INTO settings(key, value) VALUES ('session_start', ?)",
            (now(),),
        )


def now():
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def lan_ip():
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("10.255.255.255", 1))
        return s.getsockname()[0]
    except OSError:
        return "127.0.0.1"
    finally:
        s.close()


def base_url():
    return CONFIG["base_url"].rstrip("/") or f"http://{lan_ip()}:{CONFIG['port']}"


def board_state():
    with db() as conn:
        start = conn.execute(
            "SELECT value FROM settings WHERE key='session_start'"
        ).fetchone()["value"]
        regs = conn.execute(
            "SELECT * FROM registrations WHERE ts >= ? ORDER BY id", (start,)
        ).fetchall()
        open_problems = conn.execute(
            "SELECT * FROM registrations WHERE state='problem' AND resolved_at IS NULL ORDER BY id"
        ).fetchall()
        manual = {r["pc"] for r in conn.execute("SELECT pc FROM manual_busy")}

    seats = {n: {"pc": n, **SEAT_POS[n], "regs": [], "problems": [], "manual": n in manual}
             for n in SEATS}
    for r in regs:
        if r["pc"] in seats:
            seats[r["pc"]]["regs"].append(dict(r))
    for r in open_problems:
        if r["pc"] in seats:
            seats[r["pc"]]["problems"].append(dict(r))
    return {
        "session_start": start,
        "seats": list(seats.values()),
        "problems": [dict(r) for r in open_problems],
        "layout": CONFIG["layout"],
        "room": CONFIG["room"],
        "server_time": now(),
    }


class Handler(BaseHTTPRequestHandler):
    server_version = "PCMonitor/1.0"

    # ---------- помощники ----------
    def log_message(self, fmt, *args):  # тише в консоли
        if "/api/state" not in (args[0] if args else ""):
            super().log_message(fmt, *args)

    def send(self, code, body, ctype="application/json; charset=utf-8", headers=None):
        if isinstance(body, (dict, list)):
            body = json.dumps(body, ensure_ascii=False)
        if isinstance(body, str):
            body = body.encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        for k, v in (headers or {}).items():
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(body)

    def redirect(self, to, headers=None):
        self.send_response(303)
        self.send_header("Location", to)
        for k, v in (headers or {}).items():
            self.send_header(k, v)
        self.end_headers()

    def page(self, name):
        with open(os.path.join(STATIC_DIR, name), encoding="utf-8") as f:
            self.send(200, f.read(), "text/html; charset=utf-8")

    def is_teacher(self):
        c = cookies.SimpleCookie(self.headers.get("Cookie", ""))
        return "t" in c and c["t"].value in SESSIONS

    def read_body(self):
        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(min(length, 100_000)).decode("utf-8")
        if "json" in (self.headers.get("Content-Type") or ""):
            return json.loads(raw or "{}")
        return {k: v[0] for k, v in parse_qs(raw).items()}

    # ---------- GET ----------
    def do_GET(self):
        url = urlparse(self.path)
        path = url.path.rstrip("/") or "/"

        if path in ("/", "/form"):
            return self.page("form.html")
        if path == "/api/config":
            return self.send(200, {"seats": SEAT_POS, "pairs": CONFIG["pairs"],
                                   "room": CONFIG["room"]})
        if path == "/login":
            return self.page("login.html")

        # всё ниже — только для преподавателя
        if not self.is_teacher():
            if path.startswith("/api/") or path.endswith(".csv"):
                return self.send(401, {"error": "auth"})
            return self.redirect("/login")

        if path == "/teacher":
            return self.page("teacher.html")
        if path == "/qr":
            return self.page("qr.html")
        if path == "/api/state":
            return self.send(200, board_state())
        if path == "/api/qrinfo":
            return self.send(200, {"base_url": base_url(), "seats": SEAT_POS,
                                   "room": CONFIG["room"]})
        if path == "/export.csv":
            return self.export_csv()
        return self.send(404, {"error": "not found"})

    def export_csv(self):
        with db() as conn:
            rows = conn.execute("SELECT * FROM registrations ORDER BY id").fetchall()
        buf = io.StringIO()
        w = csv.writer(buf, delimiter=";")
        w.writerow(["Отметка времени", "ФИО студента", "Группа студента", "Пара",
                    "Рабочее место", "Состояние компьютера", "Комментарий", "Проблема закрыта"])
        for r in rows:
            w.writerow([r["ts"], r["fio"], r["grp"], r["pair"], r["pc"],
                        "Исправен" if r["state"] == "ok" else "Есть проблемы",
                        r["comment"], r["resolved_at"] or ""])
        name = f"registrations_{datetime.now():%Y-%m-%d}.csv"
        self.send(200, "﻿" + buf.getvalue(), "text/csv; charset=utf-8",
                  {"Content-Disposition": f'attachment; filename="{name}"'})

    # ---------- POST ----------
    def do_POST(self):
        path = urlparse(self.path).path.rstrip("/")
        try:
            data = self.read_body()
        except (ValueError, UnicodeDecodeError):
            return self.send(400, {"error": "Некорректные данные"})

        if path == "/login":
            if secrets.compare_digest(str(data.get("password", "")),
                                      str(CONFIG["teacher_password"])):
                token = secrets.token_urlsafe(24)
                SESSIONS.add(token)
                return self.redirect("/teacher", {
                    "Set-Cookie": f"t={token}; Path=/; HttpOnly; SameSite=Lax; Max-Age=2592000"})
            return self.redirect("/login?error=1")

        if path == "/api/register":
            return self.register(data)

        if not self.is_teacher():
            return self.send(401, {"error": "auth"})

        with DB_LOCK, db() as conn:
            if path == "/api/resolve":
                conn.execute("UPDATE registrations SET resolved_at=? WHERE id=? AND resolved_at IS NULL",
                             (now(), int(data.get("id", 0))))
            elif path == "/api/resolve_pc":
                conn.execute("UPDATE registrations SET resolved_at=? WHERE pc=? AND state='problem' "
                             "AND resolved_at IS NULL", (now(), int(data.get("pc", 0))))
            elif path == "/api/new_session":
                conn.execute("UPDATE settings SET value=? WHERE key='session_start'", (now(),))
                conn.execute("DELETE FROM manual_busy")
            elif path == "/api/toggle_manual":
                pc = int(data.get("pc", 0))
                if conn.execute("SELECT 1 FROM manual_busy WHERE pc=?", (pc,)).fetchone():
                    conn.execute("DELETE FROM manual_busy WHERE pc=?", (pc,))
                else:
                    conn.execute("INSERT INTO manual_busy(pc) VALUES (?)", (pc,))
            else:
                return self.send(404, {"error": "not found"})
        return self.send(200, {"ok": True})

    def register(self, data):
        def clean(key, limit=200):
            return " ".join(str(data.get(key, "")).split())[:limit]

        fio, grp, comment = clean("fio"), clean("grp", 50), clean("comment", 1000)
        state = data.get("state")
        try:
            pc, pair = int(data.get("pc", 0)), int(data.get("pair", 0))
        except (TypeError, ValueError):
            pc = pair = 0

        errors = []
        if pc not in SEAT_POS:
            errors.append("Не выбрано рабочее место")
        if len(fio) < 3:
            errors.append("Укажите ФИО")
        if not grp:
            errors.append("Укажите группу")
        if pair not in {p["n"] for p in CONFIG["pairs"]}:
            errors.append("Выберите пару")
        if state not in ("ok", "problem"):
            errors.append("Укажите состояние компьютера")
        if state == "problem" and not comment:
            errors.append("Опишите проблему в комментарии")
        if errors:
            return self.send(400, {"error": "; ".join(errors)})

        with DB_LOCK, db() as conn:
            conn.execute(
                "INSERT INTO registrations(ts, pc, fio, grp, pair, state, comment) "
                "VALUES (?,?,?,?,?,?,?)", (now(), pc, fio, grp, pair, state, comment))
            conn.execute("DELETE FROM manual_busy WHERE pc=?", (pc,))
        return self.send(200, {"ok": True})


def main():
    init_db()
    port = int(CONFIG["port"])
    srv = ThreadingHTTPServer(("0.0.0.0", port), Handler)
    print("=" * 60)
    print(f"  Сервер запущен. Адрес в сети: {base_url()}")
    print(f"  Преподаватель:  {base_url()}/teacher  (пароль в config.json)")
    print(f"  QR-коды мест:   {base_url()}/qr")
    print("  Остановить: Ctrl+C")
    print("=" * 60)
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
