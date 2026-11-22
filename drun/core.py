import json
import logging
import math
import os
import sqlite3
import time
import urllib.request
from contextlib import contextmanager
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if (ROOT / ".env").exists():
    for line in (ROOT / ".env").read_text(encoding="utf-8-sig").splitlines():
        key, sep, value = line.partition("=")
        if sep and key.strip() and not key.lstrip().startswith("#"):
            os.environ.setdefault(key.strip(), value.strip())


def request(url, data=None, headers=None, method=None):
    headers = dict(headers or {})
    if data is not None:
        headers.setdefault("Content-Type", "application/json")
        data = json.dumps(data).encode()
    req = urllib.request.Request(url, data, headers, method=method)
    with urllib.request.urlopen(req, timeout=45) as result:
        body = result.read(4_000_001)
        if len(body) > 4_000_000:
            raise ValueError("Ответ превышает 4 МБ")
        return body.decode(), result.headers


class Store:
    def __init__(self, path):
        self.path = path
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        with self.db() as db:
            db.executescript("""
                PRAGMA journal_mode=WAL;
                CREATE TABLE IF NOT EXISTS jobs(
                    id INTEGER PRIMARY KEY, prompt TEXT, channel TEXT, status TEXT,
                    result TEXT, external_id TEXT UNIQUE);
                CREATE TABLE IF NOT EXISTS turns(
                    job INTEGER PRIMARY KEY, prompt TEXT, answer TEXT);
                CREATE TABLE IF NOT EXISTS state(key TEXT PRIMARY KEY, value TEXT);
                CREATE TABLE IF NOT EXISTS memory(
                    id INTEGER PRIMARY KEY, text TEXT, vector TEXT);
                CREATE TABLE IF NOT EXISTS usage(
                    id INTEGER PRIMARY KEY, ts REAL, model TEXT, tokens INTEGER,
                    trace TEXT, status TEXT);
                CREATE TABLE IF NOT EXISTS reminders(
                    id INTEGER PRIMARY KEY, due REAL, text TEXT, delivered INTEGER DEFAULT 0);
                UPDATE jobs SET status='error', result='Работа прервана перезапуском'
                    WHERE status='running';
            """)

    @contextmanager
    def db(self):
        db = sqlite3.connect(self.path, timeout=15)
        db.row_factory = sqlite3.Row
        try:
            with db:
                yield db
        finally:
            db.close()

    def get(self, key, default=""):
        with self.db() as db:
            row = db.execute("SELECT value FROM state WHERE key=?", (key,)).fetchone()
            return row[0] if row else default

    def set(self, key, value):
        with self.db() as db:
            db.execute("INSERT OR REPLACE INTO state VALUES(?,?)", (key, value))

    def enqueue(self, prompt, channel, external_id=None):
        if not isinstance(prompt, str) or not prompt.strip() or len(prompt) > 12000:
            raise ValueError("Введите от 1 до 12000 символов")
        with self.db() as db:
            row = db.execute(
                "INSERT OR IGNORE INTO jobs(prompt,channel,status,external_id) VALUES(?,?,'queued',?) RETURNING id",
                (prompt, channel, external_id),
            ).fetchone()
            if row is None:
                row = db.execute("SELECT id FROM jobs WHERE external_id=?", (external_id,)).fetchone()
            return row[0]

    def claim(self):
        with self.db() as db:
            return db.execute("""
                UPDATE jobs SET status='running' WHERE id=(
                    SELECT MIN(id) FROM jobs WHERE status='queued')
                RETURNING id,prompt
            """).fetchone()

    def finish(self, job, answer, success):
        with self.db() as db:
            if success:
                db.execute("INSERT INTO turns VALUES(?,?,?)", (job["id"], job["prompt"], answer))
            db.execute("UPDATE jobs SET status=?,result=? WHERE id=?",
                       ("done" if success else "error", answer, job["id"]))


def estimate(data):
    return len(json.dumps(data, ensure_ascii=False).encode()) + 64


class Gateway:
    def __init__(self, store):
        self.store = store
        self.base = os.getenv("DRUN_GATEWAY", "http://127.0.0.1:4000").rstrip("/")
        self.key = os.getenv("LITELLM_MASTER_KEY", "")
        self.budget = int(os.getenv("DRUN_MAX_TOKENS", "1000000"))

    def call(self, model, endpoint, payload, trace):
        reserved = estimate(payload) + payload.get("max_tokens", 0)
        with self.store.db() as db:
            db.execute("BEGIN IMMEDIATE")
            spent = db.execute("SELECT COALESCE(SUM(tokens),0) FROM usage").fetchone()[0]
            if spent + reserved > self.budget:
                raise ValueError("Исчерпан бюджет токенов")
            row_id = db.execute("INSERT INTO usage VALUES(NULL,?,?,?,?,?)",
                                (time.time(), model, reserved, trace, "pending")).lastrowid
        status, tokens = "error", reserved
        try:
            with span("gateway", model=model, endpoint=endpoint):
                body, _ = request(f"{self.base}/v1/{endpoint}", {"model": model, **payload},
                                  {"Authorization": f"Bearer {self.key}"})
            result = json.loads(body)
            tokens = int(result.get("usage", {}).get("total_tokens") or reserved)
            status = "ok"
            return result
        finally:
            with self.store.db() as db:
                db.execute("UPDATE usage SET tokens=?,status=? WHERE id=?", (tokens, status, row_id))
            event("gateway_call", trace, model=model, endpoint=endpoint, tokens=tokens, status=status)

    def chat(self, messages, trace, schemas=None, limit=768):
        payload = {"messages": messages, "max_tokens": limit}
        if schemas:
            payload["tools"] = schemas
        return self.call("assistant", "chat/completions", payload, trace)["choices"][0]

    def vector(self, text, trace):
        vector = self.call("memory", "embeddings", {"input": text}, trace)["data"][0]["embedding"]
        norm = math.sqrt(sum(value * value for value in vector))
        return [value / norm for value in vector]


def event(name, trace, **fields):
    logging.info(json.dumps({"event": name, "trace_id": trace, **fields}, ensure_ascii=False))
    if tracer is not None:
        from opentelemetry import trace as otel

        otel.get_current_span().add_event(name, {"drun.trace_id": trace, **fields})


tracer = None


def configure_tracing():
    global tracer
    if os.getenv("OTEL_EXPORTER_OTLP_ENDPOINT"):
        from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
        from opentelemetry.sdk.resources import Resource
        from opentelemetry.sdk.trace import TracerProvider
        from opentelemetry.sdk.trace.export import BatchSpanProcessor

        provider = TracerProvider(resource=Resource.create({"service.name": "drun"}))
        provider.add_span_processor(BatchSpanProcessor(OTLPSpanExporter()))
        tracer = provider.get_tracer("drun")


@contextmanager
def span(name, **attributes):
    if tracer is None:
        yield
    else:
        with tracer.start_as_current_span(name, attributes=attributes,
                                          record_exception=False, set_status_on_exception=False):
            yield
