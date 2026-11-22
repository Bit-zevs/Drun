import argparse
import base64
import hmac
import html
import json
import logging
import os
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.error import HTTPError

from .agent import Agent
from .core import ROOT, Store, configure_tracing, event, request, span
from .telegram_format import messages


def worker(store, agent):
    agent.connect()
    while True:
        job = store.claim()
        if job is None:
            time.sleep(0.3)
            continue
        trace = f"job-{job['id']}"
        event("job_started", trace)
        try:
            with span("job", job_id=job["id"]):
                answer = agent.run(job)
        except Exception as exc:
            event("job_failed", trace, error=type(exc).__name__)
            detail = str(exc) if type(exc) is ValueError else "Проверьте конфигурацию и журнал сервера"
            store.finish(job, f"{type(exc).__name__}: {detail}", False)
        else:
            store.finish(job, answer, True)
            event("job_finished", trace)


def telegram(store, token, owner):
    def api(method, data):
        body, _ = request(f"https://api.telegram.org/bot{token}/{method}", data)
        result = json.loads(body)
        if not result["ok"]:
            raise RuntimeError(method)
        return result["result"]

    def send(text):
        for formatted, plain in messages(text):
            try:
                api("sendMessage", {"chat_id": owner, "text": formatted, "parse_mode": "HTML"})
            except HTTPError as exc:
                if exc.code != 400:
                    raise
                api("sendMessage", {"chat_id": owner, "text": plain})

    while True:
        try:
            offset = int(store.get("telegram_offset", "0"))
            for update in api("getUpdates", {"offset": offset, "timeout": 3, "allowed_updates": ["message"]}):
                msg = update.get("message", {})
                text = msg.get("text")
                if str(msg.get("chat", {}).get("id")) == owner and msg["chat"]["type"] == "private" and text:
                    store.enqueue(text, "telegram", str(update["update_id"]))
                store.set("telegram_offset", str(update["update_id"] + 1))
            with store.db() as db:
                results = db.execute("""
                    SELECT id,result FROM jobs WHERE channel='telegram'
                    AND status IN ('done','error') AND id>? ORDER BY id
                """, (int(store.get("telegram_delivered", "0")),)).fetchall()
            for row in results:
                send(row["result"])
                store.set("telegram_delivered", str(row["id"]))
            with store.db() as db:
                reminders = db.execute(
                    "SELECT id,text FROM reminders WHERE delivered=0 AND due<=? ORDER BY due",
                    (time.time(),)).fetchall()
            for row in reminders:
                send("Напоминание: " + row["text"])
                with store.db() as db:
                    db.execute("UPDATE reminders SET delivered=1 WHERE id=?", (row["id"],))
        except Exception as exc:
            event("telegram_error", "telegram", error=type(exc).__name__)
            time.sleep(5)


def handler(store, token):
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_):
            pass

        def reply(self, status, payload, content_type="application/json"):
            body = (json.dumps(payload, ensure_ascii=False) if content_type == "application/json"
                    else payload).encode()
            self.send_response(status)
            self.send_header("Content-Type", content_type + "; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            if status == 401:
                self.send_header("WWW-Authenticate", 'Basic realm="Drun"')
            self.end_headers()
            self.wfile.write(body)

        def authorized(self):
            auth = self.headers.get("Authorization", "")
            if auth.startswith("Basic "):
                try:
                    auth = "Bearer " + base64.b64decode(auth[6:]).decode().split(":", 1)[1]
                except (ValueError, IndexError):
                    auth = ""
            if hmac.compare_digest(auth.encode(), ("Bearer " + token).encode()):
                return True
            self.reply(401, {"error": "Требуется токен Drun"})
            return False

        def do_POST(self):
            if not self.authorized():
                return
            if self.path != "/jobs":
                return self.reply(404, {"error": "Не найдено"})
            try:
                size = int(self.headers.get("Content-Length", 0))
                if not 0 < size <= 100000:
                    raise ValueError("Недопустимый размер запроса")
                payload = json.loads(self.rfile.read(size))
                job_id = store.enqueue(payload["prompt"], "cli")
            except (ValueError, KeyError, TypeError):
                return self.reply(400, {"error": "Некорректное сообщение"})
            self.reply(202, {"id": job_id})

        def do_GET(self):
            if not self.authorized():
                return
            with store.db() as db:
                if self.path in {"/usage", "/dashboard"}:
                    rows = [dict(row) for row in db.execute("""
                        SELECT model,status,COUNT(*) AS calls,SUM(tokens) AS tokens
                        FROM usage GROUP BY model,status ORDER BY model,status
                    """)]
                    budget = int(os.getenv("DRUN_MAX_TOKENS", "1000000"))
                    used = sum(row["tokens"] for row in rows)
                    if self.path == "/usage":
                        return self.reply(200, {"budget": budget, "used": used, "models": rows})
                    table = "".join("<tr>" + "".join(f"<td>{html.escape(str(value))}</td>"
                                                      for value in row.values()) + "</tr>" for row in rows)
                    return self.reply(200, f"""<!doctype html><html lang="ru"><meta charset="utf-8">
                        <title>Расход токенов Drun</title>
                        <style>body{{font:18px system-ui;margin:4rem auto;max-width:48rem;padding:0 1rem}}
                        th,td{{padding:.6rem;text-align:left;border-bottom:1px solid #ccc}}</style>
                        <h1>Расход токенов Drun</h1><p>{used:,} из {budget:,} токенов</p>
                        <progress value="{used}" max="{budget}"></progress>
                        <table><thead><tr><th>Модель</th><th>Статус</th><th>Вызовы</th><th>Токены</th></tr></thead>
                        <tbody>{table}</tbody></table></html>""", "text/html")
                if self.path == "/reminders":
                    rows = db.execute("SELECT id,text FROM reminders WHERE due<=? ORDER BY due",
                                      (time.time(),)).fetchall()
                    return self.reply(200, [dict(row) for row in rows])
                job_id = self.path.removeprefix("/jobs/")
                if self.path.startswith("/jobs/") and job_id.isascii() and job_id.isdigit() and len(job_id) < 19:
                    row = db.execute("SELECT status,result FROM jobs WHERE id=?", (int(job_id),)).fetchone()
                    if row:
                        return self.reply(200, dict(row))
            self.reply(404, {"error": "Не найдено"})

    return Handler


def main():
    parser = argparse.ArgumentParser(prog="drun")
    parser.add_argument("mode", choices=["serve", "ask", "usage", "reminders"])
    parser.add_argument("prompt", nargs="?")
    parser.add_argument("--port", type=int, default=8765)
    args = parser.parse_args()
    token = os.getenv("DRUN_TOKEN", "")
    if len(token) < 20 or token == "change-this-to-a-long-random-token":
        parser.error("Установите собственный DRUN_TOKEN длиной не менее 20 символов")
    if args.mode == "serve":
        logging.basicConfig(level=logging.INFO, format="%(message)s")
        configure_tracing()
        with ThreadingHTTPServer(("127.0.0.1", args.port), BaseHTTPRequestHandler) as server:
            store = Store(ROOT / ".drun" / "drun.sqlite3")
            server.RequestHandlerClass = handler(store, token)
            agent = Agent(store, ROOT / "workspace")
            threading.Thread(target=worker, args=(store, agent), daemon=True).start()
            telegram_token, owner = os.getenv("TELEGRAM_TOKEN"), os.getenv("TELEGRAM_CHAT_ID")
            if telegram_token and owner:
                threading.Thread(target=telegram, args=(store, telegram_token, owner), daemon=True).start()
            event("server_started", "startup", port=args.port, telegram=bool(telegram_token and owner))
            server.serve_forever()
        return

    def api(path, data=None):
        body, _ = request(f"http://127.0.0.1:{args.port}{path}", data, {"Authorization": f"Bearer {token}"})
        return json.loads(body)

    if args.mode in {"usage", "reminders"}:
        print(json.dumps(api("/" + args.mode), ensure_ascii=False, indent=2))
        return
    if not args.prompt:
        parser.error("Для ask укажите сообщение")
    job = api("/jobs", {"prompt": args.prompt})["id"]
    while True:
        status = api(f"/jobs/{job}")
        if status["status"] in {"done", "error"}:
            print(status["result"])
            return int(status["status"] == "error")
        time.sleep(0.5)


if __name__ == "__main__":
    raise SystemExit(main())
