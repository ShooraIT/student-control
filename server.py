"""Мониторинг рабочих мест компьютерного класса.

Запуск:  py server.py
Студенты: онлайн-форма на GitHub Pages (QR-код на столе) — анкета шифруется в телефоне
          открытым ключом преподавателя и через ретранслятор ntfy.sh приходит сюда.
          Запасной вариант в локальной сети: http://<IP>:<порт>/form?pc=N
Преподаватель: http://localhost:<порт>/teacher

Только стандартная библиотека Python 3.9+. Данные — в SQLite (data.db), закрытый
ключ — в teacher_key.json; оба файла остаются только на этом компьютере.
"""
import csv
import io
import json
import os
import secrets
import socket
import sqlite3
import threading
import time
import urllib.request
from datetime import datetime
from http import cookies
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, quote, urlparse

import rsa_lite

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
STATIC_DIR = os.path.join(BASE_DIR, "static")
CONFIG_PATH = os.path.join(BASE_DIR, "config.json")
DB_PATH = os.path.join(BASE_DIR, "data.db")
KEY_PATH = os.path.join(BASE_DIR, "teacher_key.json")
DOCS_DIR = os.path.join(BASE_DIR, "docs")
PUBLIC_JSON = os.path.join(DOCS_DIR, "public.json")

DEFAULT_CONFIG = {
    "port": 8080,
    # Рассадка: ряды слева направо (как видит преподаватель со своего места),
    # в каждом ряду номера мест от стола преподавателя к задней стене (нумерация «змейкой»).
    "layout": {
        "rows": [
            [1, 2, 3, 4, 5, 6, 7],
            [14, 13, 12, 11, 10, 9, 8],
            [15, 16, 17, 18, 19, 20, 21],
            [30, 29, 28, 27, 26, 25, 24, 23, 22],
        ],
        "aisles_after": [1, 3],   # проход после 1-го и после 3-го ряда
        "teacher_rows": [2, 3],   # стол преподавателя перед этими рядами
    },
    "teacher_password": "teacher",
    "room": "Аудитория",
    # Расписание звонков колледжа (с 02.11.2026). Пара — это id; у пары может быть
    # несколько частей (между ними обед). "mon" — понедельник, "tue_sat" — вторник–суббота.
    "schedule": {
        "pairs": [
            {"id": "1-rov", "name": "Разговоры о важном", "shift": "I"},
            {"id": "1-1", "name": "1 пара", "shift": "I"},
            {"id": "1-2", "name": "2 пара", "shift": "I"},
            {"id": "1-3", "name": "3 пара", "shift": "I"},
            {"id": "2-rov", "name": "Разговоры о важном", "shift": "II"},
            {"id": "2-1", "name": "1 пара", "shift": "II"},
            {"id": "2-2", "name": "2 пара", "shift": "II"},
            {"id": "2-3", "name": "3 пара", "shift": "II"},
        ],
        "days": {
            "mon": {
                "1-rov": [["08:00", "08:45"]],
                "1-1": [["08:50", "10:20"]],
                "1-2": [["10:30", "11:15"], ["11:35", "12:20"]],
                "1-3": [["12:40", "13:25"], ["13:45", "14:30"]],
                "2-rov": [["13:45", "14:30"]],
                "2-1": [["14:35", "15:20"], ["15:40", "16:25"]],
                "2-2": [["16:45", "18:15"]],
                "2-3": [["18:25", "19:55"]],
            },
            "tue_sat": {
                "1-1": [["08:20", "09:50"]],
                "1-2": [["10:00", "10:45"], ["11:05", "11:50"]],
                "1-3": [["12:10", "12:55"], ["13:15", "14:00"]],
                "2-1": [["14:20", "15:05"], ["15:25", "16:10"]],
                "2-2": [["16:30", "18:00"]],
                "2-3": [["18:10", "19:40"]],
            },
        },
    },
    # Онлайн-форма для студентов (GitHub Pages) — на неё ведут QR-коды
    "public_form_url": "https://shoorait.github.io/student-control/",
    # Ретранслятор: телефон отправляет туда зашифрованную анкету, сервер её забирает
    "relay": "https://ntfy.sh",
    "topic": "",  # генерируется автоматически при первом запуске
    # Адрес сервера в локальной сети (запасной вариант). Пусто — определить автоматически
    "base_url": "",
}


def load_config():
    stored = {}
    if os.path.exists(CONFIG_PATH):
        with open(CONFIG_PATH, encoding="utf-8") as f:
            stored = json.load(f)
    cfg = {**DEFAULT_CONFIG, **stored}
    # дописать новые параметры в старый config.json и убрать устаревшие (например, "pairs")
    changed = set(stored) != set(DEFAULT_CONFIG)
    if not cfg.get("topic"):
        cfg["topic"] = "pcmon-" + secrets.token_urlsafe(18).replace("_", "x").replace("-", "y")
        changed = True
    cfg = {k: cfg[k] for k in DEFAULT_CONFIG}
    if changed:
        with open(CONFIG_PATH, "w", encoding="utf-8") as f:
            json.dump(cfg, f, ensure_ascii=False, indent=2)
    return cfg


def load_key():
    if os.path.exists(KEY_PATH):
        with open(KEY_PATH, encoding="utf-8") as f:
            return rsa_lite.from_json(json.load(f))
    print("Создаю ключ шифрования (несколько секунд)...")
    key = rsa_lite.generate(2048)
    with open(KEY_PATH, "w", encoding="utf-8") as f:
        json.dump(rsa_lite.to_json(key), f)
    return key


CONFIG = load_config()
# номер места -> {"row": ряд, "place": место в ряду}
SEAT_POS = {pc: {"row": r + 1, "place": i + 1}
            for r, row in enumerate(CONFIG["layout"]["rows"])
            for i, pc in enumerate(row)}
SEATS = sorted(SEAT_POS)
KEY = load_key()
PAIR_LABEL = {p["id"]: f'{p["name"]} ({p["shift"]} смена)' for p in CONFIG["schedule"]["pairs"]}


def pair_label(pid):
    return PAIR_LABEL.get(str(pid), f"{pid} пара")
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
                pair TEXT NOT NULL,           -- id пары из schedule, напр. "1-2"
                state TEXT NOT NULL,          -- ok | problem
                comment TEXT NOT NULL DEFAULT '',
                resolved_at TEXT,             -- когда преподаватель закрыл проблему
                relay_id TEXT UNIQUE,         -- id сообщения ретранслятора (защита от дублей)
                cancelled_at TEXT             -- регистрацию отменил преподаватель
            );
            CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT);
            CREATE TABLE IF NOT EXISTS manual_busy (pc INTEGER PRIMARY KEY);
            CREATE TABLE IF NOT EXISTS teacher_sessions (token TEXT PRIMARY KEY);
            """
        )
        conn.execute(
            "INSERT OR IGNORE INTO settings(key, value) VALUES ('session_start', ?)",
            (now(),),
        )
        cols = {r[1] for r in conn.execute("PRAGMA table_info(registrations)")}
        if "cancelled_at" not in cols:
            conn.execute("ALTER TABLE registrations ADD COLUMN cancelled_at TEXT")
        if "relay_id" not in cols:
            conn.execute("ALTER TABLE registrations ADD COLUMN relay_id TEXT")
            conn.execute("CREATE UNIQUE INDEX IF NOT EXISTS ux_relay ON registrations(relay_id)")


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


def public_config():
    """То, что видит телефон студента: рассадка, звонки, канал и ОТКРЫТЫЙ ключ."""
    return {"room": CONFIG["room"], "schedule": CONFIG["schedule"], "seats": SEAT_POS,
            "relay": CONFIG["relay"].rstrip("/"), "topic": CONFIG["topic"],
            "public_key": rsa_lite.public_jwk(KEY)}


def write_public_json():
    """Обновляет docs/public.json для GitHub Pages. True — если файл изменился."""
    text = json.dumps(public_config(), ensure_ascii=False, indent=1) + "\n"
    old = open(PUBLIC_JSON, encoding="utf-8").read() if os.path.exists(PUBLIC_JSON) else ""
    if old == text:
        return False
    os.makedirs(DOCS_DIR, exist_ok=True)
    with open(PUBLIC_JSON, "w", encoding="utf-8", newline="\n") as f:
        f.write(text)
    return True


def form_url():
    return CONFIG["public_form_url"] or base_url() + "/form"


# ---------- приём анкет ----------
def save_registration(data, ts, relay_id=None):
    """Проверяет и сохраняет анкету. Возвращает текст ошибки или ''."""
    def clean(key, limit):
        return " ".join(str(data.get(key, "")).split())[:limit]

    fio, grp, comment = clean("fio", 100), clean("grp", 30), clean("comment", 300)
    state = data.get("state")
    try:
        pc = int(data.get("pc", 0))
    except (TypeError, ValueError):
        pc = 0
    pair = str(data.get("pair", ""))

    errors = []
    if pc not in SEAT_POS:
        errors.append("Не выбрано рабочее место")
    if len(fio) < 3:
        errors.append("Укажите ФИО")
    if not grp:
        errors.append("Укажите группу")
    if pair not in PAIR_LABEL:
        errors.append("Выберите пару")
    if state not in ("ok", "problem"):
        errors.append("Укажите состояние компьютера")
    if state == "problem" and not comment:
        errors.append("Опишите проблему в комментарии")
    if errors:
        return "; ".join(errors)

    with DB_LOCK, db() as conn:
        conn.execute(
            "INSERT OR IGNORE INTO registrations(ts, pc, fio, grp, pair, state, comment, relay_id) "
            "VALUES (?,?,?,?,?,?,?,?)", (ts, pc, fio, grp, pair, state, comment, relay_id))
        conn.execute("DELETE FROM manual_busy WHERE pc=?", (pc,))
    return ""


RELAY_STATUS = {"ok": False, "since": None, "error": "ещё не подключено", "received": 0}


def get_setting(key, default=None):
    with db() as conn:
        row = conn.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
    return row["value"] if row else default


def set_setting(key, value):
    with DB_LOCK, db() as conn:
        conn.execute("INSERT OR REPLACE INTO settings(key, value) VALUES (?, ?)", (key, value))


def handle_relay_message(msg):
    if msg.get("event") != "message":
        return
    ts = datetime.fromtimestamp(msg.get("time", time.time())).strftime("%Y-%m-%d %H:%M:%S")
    try:
        data = json.loads(rsa_lite.decrypt_message(KEY, msg.get("message", "")).decode("utf-8"))
        err = save_registration(data, ts, relay_id=msg["id"])
        if err:
            print(f"[онлайн] анкета отклонена: {err}")
        else:
            RELAY_STATUS["received"] += 1
    except Exception as ex:  # чужое/повреждённое сообщение — пропускаем
        print(f"[онлайн] не удалось расшифровать сообщение {msg.get('id')}: {ex}")
    set_setting("relay_last_id", msg["id"])


def relay_loop():
    """Держит потоковое подключение к ntfy и забирает зашифрованные анкеты.
    Сообщения хранятся на ntfy.sh 12 часов, так что анкеты, отправленные
    пока сервер был выключен, будут получены при следующем запуске."""
    base = CONFIG["relay"].rstrip("/") + "/" + quote(CONFIG["topic"])
    while True:
        since = get_setting("relay_last_id") or "12h"
        url = f"{base}/json?since={quote(since)}"
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "pc-monitor"})
            with urllib.request.urlopen(req, timeout=90) as resp:
                RELAY_STATUS.update(ok=True, since=now(), error="")
                for line in resp:
                    line = line.strip()
                    if line:
                        handle_relay_message(json.loads(line))
        except Exception as ex:
            RELAY_STATUS.update(ok=False, error=str(ex)[:200])
            time.sleep(5)


def day_type(date):
    return "mon" if date.weekday() == 0 else "tue_sat"


def history(date_str):
    """Регистрации за день, по парам. Отменённые не показываются."""
    try:
        date = datetime.strptime(date_str, "%Y-%m-%d")
    except ValueError:
        date = datetime.now()
    d = date.strftime("%Y-%m-%d")
    with db() as conn:
        regs = [dict(r) for r in conn.execute(
            "SELECT * FROM registrations WHERE substr(ts, 1, 10) = ? AND cancelled_at IS NULL "
            "ORDER BY id", (d,))]
        days = [dict(r) for r in conn.execute(
            "SELECT substr(ts, 1, 10) AS date, COUNT(*) AS n FROM registrations "
            "WHERE cancelled_at IS NULL GROUP BY 1 ORDER BY 1 DESC LIMIT 30")]
    for r in regs:
        r["pair_label"] = pair_label(r["pair"])
    counts = {}
    for r in regs:
        counts[str(r["pair"])] = counts.get(str(r["pair"]), 0) + 1
    day = CONFIG["schedule"]["days"][day_type(date)]
    pairs = [{"id": p["id"], "label": pair_label(p["id"]),
              "time": ", ".join("–".join(t) for t in day[p["id"]]), "count": counts.pop(p["id"], 0)}
             for p in CONFIG["schedule"]["pairs"] if p["id"] in day]
    pairs += [{"id": k, "label": pair_label(k), "time": "", "count": n} for k, n in counts.items()]
    return {"date": d, "pairs": pairs, "regs": regs, "days": days,
            "seats": [{"pc": n, **SEAT_POS[n]} for n in SEATS], "layout": CONFIG["layout"]}


def board_state():
    with db() as conn:
        start = conn.execute(
            "SELECT value FROM settings WHERE key='session_start'"
        ).fetchone()["value"]
        regs = conn.execute(
            "SELECT * FROM registrations WHERE ts >= ? AND cancelled_at IS NULL ORDER BY id", (start,)
        ).fetchall()
        open_problems = conn.execute(
            "SELECT * FROM registrations WHERE state='problem' AND resolved_at IS NULL "
            "AND cancelled_at IS NULL ORDER BY id"
        ).fetchall()
        manual = {r["pc"] for r in conn.execute("SELECT pc FROM manual_busy")}

    seats = {n: {"pc": n, **SEAT_POS[n], "regs": [], "problems": [], "manual": n in manual}
             for n in SEATS}
    regs = [{**r, "pair_label": pair_label(r["pair"])} for r in map(dict, regs)]
    open_problems = [{**r, "pair_label": pair_label(r["pair"])} for r in map(dict, open_problems)]
    for r in regs:
        if r["pc"] in seats:
            seats[r["pc"]]["regs"].append(r)
    for r in open_problems:
        if r["pc"] in seats:
            seats[r["pc"]]["problems"].append(r)
    return {
        "session_start": start,
        "seats": list(seats.values()),
        "problems": open_problems,
        "layout": CONFIG["layout"],
        "room": CONFIG["room"],
        "server_time": now(),
        "relay": RELAY_STATUS,
    }


class Handler(BaseHTTPRequestHandler):
    server_version = "PCMonitor/1.0"

    # ---------- помощники ----------
    def log_message(self, fmt, *args):  # тише в консоли
        if "/api/state" not in str(args[0] if args else ""):
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
        if "t" not in c:
            return False
        with db() as conn:
            return conn.execute("SELECT 1 FROM teacher_sessions WHERE token=?",
                                (c["t"].value,)).fetchone() is not None

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
            with open(os.path.join(DOCS_DIR, "index.html"), encoding="utf-8") as f:
                return self.send(200, f.read(), "text/html; charset=utf-8")
        if path == "/public.json":  # локальный режим: форма отправляет напрямую
            return self.send(200, {**public_config(), "local": True})
        if path == "/login":
            return self.page("login.html")

        # всё ниже — только для преподавателя
        if not self.is_teacher():
            if path.startswith("/api/") or path.endswith(".csv"):
                return self.send(401, {"error": "auth"})
            return self.redirect("/login?next=" + quote(path))

        if path == "/teacher":
            return self.page("teacher.html")
        if path == "/qr":
            return self.page("qr.html")
        if path == "/api/state":
            return self.send(200, board_state())
        if path == "/api/qrinfo":
            return self.send(200, {"form_url": form_url(), "local_url": base_url() + "/form",
                                   "seats": SEAT_POS, "room": CONFIG["room"]})
        if path == "/api/history":
            return self.send(200, history(parse_qs(url.query).get("date", [""])[0]))
        if path == "/export.csv":
            q = parse_qs(url.query)
            return self.export_csv(q.get("date", [""])[0], q.get("pair", [""])[0])
        return self.send(404, {"error": "not found"})

    def export_csv(self, date="", pair=""):
        sql, args = "SELECT * FROM registrations WHERE 1=1", []
        if date:
            sql += " AND substr(ts, 1, 10) = ?"; args.append(date)
        if pair:
            sql += " AND pair = ?"; args.append(pair)
        with db() as conn:
            rows = conn.execute(sql + " ORDER BY id", args).fetchall()
        buf = io.StringIO()
        w = csv.writer(buf, delimiter=";")
        w.writerow(["Отметка времени", "ФИО студента", "Группа студента", "Пара",
                    "Рабочее место", "Состояние компьютера", "Комментарий", "Проблема закрыта",
                    "Регистрация отменена"])
        for r in rows:
            w.writerow([r["ts"], r["fio"], r["grp"], pair_label(r["pair"]), r["pc"],
                        "Исправен" if r["state"] == "ok" else "Есть проблемы",
                        r["comment"], r["resolved_at"] or "", r["cancelled_at"] or ""])
        name = f"registrations_{date or datetime.now().strftime('%Y-%m-%d')}{'_' + pair if pair else ''}.csv"
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
            nxt = data.get("next") if data.get("next") in ("/teacher", "/qr") else "/teacher"
            if secrets.compare_digest(str(data.get("password", "")),
                                      str(CONFIG["teacher_password"])):
                token = secrets.token_urlsafe(24)
                with DB_LOCK, db() as conn:  # вход сохраняется и после перезапуска сервера
                    conn.execute("INSERT INTO teacher_sessions(token) VALUES (?)", (token,))
                return self.redirect(nxt, {
                    "Set-Cookie": f"t={token}; Path=/; HttpOnly; SameSite=Lax; Max-Age=2592000"})
            return self.redirect("/login?error=1&next=" + quote(nxt))

        if path == "/api/register":
            err = save_registration(data, now())
            return self.send(400, {"error": err}) if err else self.send(200, {"ok": True})

        if not self.is_teacher():
            return self.send(401, {"error": "auth"})

        with DB_LOCK, db() as conn:
            if path == "/api/resolve":
                conn.execute("UPDATE registrations SET resolved_at=? WHERE id=? AND resolved_at IS NULL",
                             (now(), int(data.get("id", 0))))
            elif path == "/api/resolve_pc":
                conn.execute("UPDATE registrations SET resolved_at=? WHERE pc=? AND state='problem' "
                             "AND resolved_at IS NULL", (now(), int(data.get("pc", 0))))
            elif path == "/api/cancel":  # отменить одну регистрацию
                conn.execute("UPDATE registrations SET cancelled_at=? WHERE id=? AND cancelled_at IS NULL",
                             (now(), int(data.get("id", 0))))
            elif path == "/api/cancel_pc":  # отменить регистрации места на текущей паре
                start = conn.execute("SELECT value FROM settings WHERE key='session_start'").fetchone()[0]
                conn.execute("UPDATE registrations SET cancelled_at=? WHERE pc=? AND ts>=? "
                             "AND cancelled_at IS NULL", (now(), int(data.get("pc", 0)), start))
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


def main():
    init_db()
    changed = write_public_json()
    threading.Thread(target=relay_loop, daemon=True).start()
    port = int(CONFIG["port"])
    srv = ThreadingHTTPServer(("0.0.0.0", port), Handler)
    print("=" * 60)
    print(f"  Преподаватель:  http://localhost:{port}/teacher  (пароль в config.json)")
    print(f"  QR-коды мест:   http://localhost:{port}/qr")
    print(f"  Онлайн-форма:   {form_url()}")
    print(f"  Запасная форма в локальной сети: {base_url()}/form")
    if changed:
        print("\n  !!! Изменился docs/public.json (ключ, рассадка или звонки).")
        print("      Загрузите его на GitHub, иначе онлайн-форма будет со старыми данными:")
        print("      git add docs/public.json && git commit -m \"public.json\" && git push")
    print("  Остановить: Ctrl+C")
    print("=" * 60)
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
