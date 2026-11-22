import datetime as dt
import json
import os
import re
import subprocess
import tempfile
import urllib.parse
from pathlib import Path

from .core import request


class Workspace:
    def __init__(self, root):
        self.root = Path(root).resolve()
        self.root.mkdir(parents=True, exist_ok=True)

    def path(self, value):
        path = (self.root / value).resolve()
        if not path.is_relative_to(self.root):
            raise ValueError("Путь выходит за пределы workspace")
        return path

    def read(self, path):
        with self.path(path).open(encoding="utf-8-sig") as stream:
            return stream.read(12000)

    def write(self, path, content):
        target = self.path(path)
        target.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.NamedTemporaryFile(dir=target.parent, delete=False) as stream:
            temp = Path(stream.name)
            stream.write(content.encode())
        try:
            temp.replace(target)
        finally:
            temp.unlink(missing_ok=True)
        return str(target.relative_to(self.root))

    def instructions(self, path):
        target = self.path(path)
        current = target if target.is_dir() else target.parent
        folders = [current, *current.parents]
        output = {}
        for folder in reversed(folders):
            if folder.is_relative_to(self.root):
                for filename in ("AGENTS.md", "AGENTS.override.md"):
                    candidate = self.path(str(folder / filename))
                    if candidate.is_file():
                        relative = str(candidate.relative_to(self.root))
                        output[relative] = self.read(relative)
        return output

    def tree(self, path):
        root = self.path(path)
        entries = []
        for folder, dirs, files in os.walk(root):
            relative = Path(folder).relative_to(root)
            dirs[:] = sorted(d for d in dirs if d not in {".git", "__pycache__"}
                             and not (Path(folder) / d).is_symlink())
            entries.extend(str(relative / name) + "/" for name in dirs)
            entries.extend(str(relative / name) for name in sorted(files))
            if len(entries) >= 300:
                break
        return entries[:300]

    def skills(self):
        catalog = {}
        for base in ("skills", ".agents/skills"):
            for file in sorted(self.path(base).glob("*/SKILL.md")):
                relative = str(file.relative_to(self.root))
                source = self.read(relative)
                match = re.match(r"---\s*\n(.*?)\n---", source, re.S)
                if match:
                    header = match[1]
                    name = re.search(r"^name:\s*['\"]?([a-z0-9-]+)", header, re.M)
                    description = re.search(r"^description:[ \t]*(.*)", header, re.M)
                    if name and description and name[1] == file.parent.name:
                        value = description[1].strip()
                        if value in {"|", ">", "|-", ">-"}:
                            value = " ".join(re.findall(r"^[ \t]+(.+)", header[description.end():], re.M))
                        catalog[name[1]] = (value.strip("'\""), relative)
        return catalog

    def skill(self, name):
        return self.read(self.skills()[name][1])

    def shell(self, command, shell):
        runners = {"bash": ["bash", "-lc"], "pwsh": ["pwsh", "-NoProfile", "-NonInteractive", "-Command"]}
        args = runners[shell]
        # Docker stops the container even if the command launches child processes.
        result = subprocess.run([
            "docker", "run", "--rm", "--pull=never", "--network=none", "--read-only",
            "--cap-drop=ALL", "--security-opt=no-new-privileges",
            "--pids-limit=64", "--memory=256m", "--cpus=1",
            "--user=65534:65534", "--env=HOME=/tmp",
            "--tmpfs=/tmp:rw,nosuid,nodev,size=32m",
            "--mount", f"type=bind,source={self.root},target=/work,readonly",
            "drun-sandbox", "timeout", "-k", "2", "20", *args, command,
        ], stdout=subprocess.PIPE, stderr=subprocess.STDOUT, timeout=45)
        return {"code": result.returncode, "output": result.stdout[:12000].decode("utf-8", errors="replace")}


class MCP:
    def __init__(self, config):
        self.url = config["url"]
        self.headers = {"Accept": "application/json, text/event-stream", **config.get("headers", {})}
        self.serial = 0
        result = self.rpc("initialize", {"protocolVersion": "2025-03-26",
                                        "capabilities": {}, "clientInfo": {"name": "Drun", "version": "1.0"}})
        self.headers["MCP-Protocol-Version"] = result["protocolVersion"]
        self.rpc("notifications/initialized", {}, notification=True)

    def rpc(self, method, params, notification=False):
        payload = {"jsonrpc": "2.0", "method": method, "params": params}
        if not notification:
            self.serial += 1
            payload["id"] = self.serial
        body, headers = request(self.url, payload, self.headers)
        session = headers.get("Mcp-Session-Id")
        if session:
            self.headers["Mcp-Session-Id"] = session
        if notification:
            return
        if "text/event-stream" in headers.get("Content-Type", ""):
            replies = (json.loads(line[5:].strip()) for line in body.splitlines() if line.startswith("data:"))
            result = next((item for item in replies if item.get("id") == self.serial), {})
        else:
            result = json.loads(body)
        if "result" not in result:
            raise ValueError(f"MCP: {result.get('error', 'нет ответа')}")
        return result["result"]

    def tools(self):
        params = {}
        while True:
            page = self.rpc("tools/list", params)
            yield from page["tools"]
            if not page.get("nextCursor"):
                return
            params = {"cursor": page["nextCursor"]}


def search(query):
    url = "https://api.search.brave.com/res/v1/web/search?" + urllib.parse.urlencode({"q": query, "count": 5})
    body, _ = request(url, headers={"X-Subscription-Token": os.environ["BRAVE_API_KEY"]})
    return [{key: item.get(key) for key in ("title", "url", "description")}
            for item in json.loads(body).get("web", {}).get("results", [])]


def github(repository):
    if (not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", repository)
            or any(part in {".", ".."} for part in repository.split("/"))):
        raise ValueError("Укажи owner/repo")
    headers = {"User-Agent": "Drun", "Accept": "application/vnd.github+json"}
    if os.getenv("GITHUB_TOKEN"):
        headers["Authorization"] = "Bearer " + os.environ["GITHUB_TOKEN"]
    body, _ = request("https://api.github.com/repos/" + repository + "/issues?per_page=10", headers=headers)
    return [{key: item[key] for key in ("number", "title", "html_url", "state")}
            for item in json.loads(body) if "pull_request" not in item]


def calendar(source, date):
    from icalendar import Calendar
    import recurring_ical_events
    from zoneinfo import ZoneInfo

    zone = ZoneInfo(os.getenv("DRUN_TZ", "UTC"))
    day = dt.date.fromisoformat(date)
    start = dt.datetime.combine(day, dt.time.min, zone)
    key = {"personal": "PERSONAL_ICS", "external": "EXTERNAL_ICS"}[source]
    feed = os.environ[key]
    if feed.startswith("webcal://"):
        feed = "https://" + feed[len("webcal://"):]
    if feed.startswith("https://"):
        body, _ = request(feed)
    else:
        body = Path(feed).read_text(encoding="utf-8-sig")
    events = recurring_ical_events.of(Calendar.from_ical(body)).between(start, start + dt.timedelta(days=1))

    def local(value):
        if isinstance(value, dt.datetime) and value.tzinfo:
            value = value.astimezone(zone)
        return value.isoformat()

    return [{"title": str(item.get("SUMMARY", "")),
             "start": local(item.decoded("DTSTART")),
             "end": local(item.decoded("DTEND"))} for item in events[:100]]
