"""Single-event voting server. Python 3.10+, standard library only."""
import argparse
import hashlib
import hmac
import ipaddress
import json
import os
from pathlib import Path
import secrets
import sqlite3
import time
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlsplit

ROOT = Path(__file__).resolve().parent


def connect(path):
    db = sqlite3.connect(path, timeout=10)
    db.row_factory = sqlite3.Row
    db.execute("PRAGMA foreign_keys=ON")
    return db


def initialize(path):
    db = connect(path)
    try:
        db.executescript("""
        CREATE TABLE IF NOT EXISTS settings (id INTEGER PRIMARY KEY CHECK(id=1), phase TEXT NOT NULL);
        INSERT OR IGNORE INTO settings VALUES (1, 'draft');
        CREATE TABLE IF NOT EXISTS teams (id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT NOT NULL UNIQUE);
        CREATE TABLE IF NOT EXISTS members (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            team_id INTEGER NOT NULL REFERENCES teams(id) ON DELETE CASCADE,
            name TEXT NOT NULL, code TEXT NOT NULL UNIQUE, UNIQUE(team_id,name));
        CREATE TABLE IF NOT EXISTS votes (
            member_id INTEGER PRIMARY KEY REFERENCES members(id),
            vote1 INTEGER NOT NULL REFERENCES teams(id),
            vote2 INTEGER NOT NULL REFERENCES teams(id),
            weight REAL NOT NULL, created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            CHECK(vote1 != vote2));
        """)
        db.commit()
    finally:
        db.close()


class Invalid(Exception):
    def __init__(self, message, status=400):
        self.message, self.status = message, status


def make_server(host, port, db_path, password, trust_proxy=False):
    initialize(db_path)
    sessions = set()
    attempts = {}
    attempts_lock = threading.Lock()

    class Handler(BaseHTTPRequestHandler):
        def setup(self):
            super().setup()
            self.connection.settimeout(15)

        def log_message(self, *_args):
            pass

        def client_ip(self):
            peer = self.client_address[0]
            # Only the explicitly trusted local reverse proxy may supply this header.
            if trust_proxy and ipaddress.ip_address(peer).is_loopback:
                try:
                    return str(ipaddress.ip_address(self.headers.get("X-Real-IP", "")))
                except ValueError:
                    pass
            return peer

        def reply(self, status, data, content_type="application/json; charset=utf-8"):
            body = json.dumps(data, ensure_ascii=False).encode() if isinstance(data, (dict, list)) else data
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Referrer-Policy", "no-referrer")
            self.send_header("Content-Security-Policy", "default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; frame-ancestors 'none'; base-uri 'none'; form-action 'self'")
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self):
            self.handle_request("GET")

        def do_POST(self):
            self.handle_request("POST")

        def handle_request(self, method):
            db = None
            try:
                path = urlsplit(self.path).path
                if method == "GET" and path in ("/", "/vote.html", "/app.js"):
                    filename = "app.js" if path == "/app.js" else "vote.html"
                    self.reply(200, (ROOT / filename).read_bytes(), "text/javascript; charset=utf-8" if filename.endswith(".js") else "text/html; charset=utf-8")
                    return
                body = {}
                if method == "POST":
                    if self.headers.get("Content-Type", "").split(";")[0] != "application/json":
                        raise Invalid("请求必须为 JSON", 415)
                    try:
                        size = int(self.headers.get("Content-Length", "0"))
                        if size < 1 or size > 16384:
                            raise ValueError()
                        body = json.loads(self.rfile.read(size))
                        if not isinstance(body, dict):
                            raise ValueError()
                    except (ValueError, UnicodeDecodeError):
                        raise Invalid("请求数据无效")
                    origin = self.headers.get("Origin")
                    if origin and urlsplit(origin).netloc != self.headers.get("Host"):
                        raise Invalid("不允许跨站提交", 403)
                db = connect(db_path)
                if path.startswith("/api/admin/") and path != "/api/admin/login":
                    token = self.headers.get("Authorization", "").removeprefix("Bearer ")
                    if token not in sessions:
                        raise Invalid("请先登录管理员", 401)
                if method == "POST" and path in ("/api/admin/login", "/api/identify", "/api/vote"):
                    is_login = path.endswith("login")
                    key = (self.client_ip(), "admin" if is_login else "vote")
                    now = time.monotonic()
                    with attempts_lock:
                        for expired in [k for k, values in attempts.items() if not values or now - values[-1] >= 60]:
                            del attempts[expired]
                        history = [t for t in attempts.get(key, []) if now - t < 60]
                        if len(history) >= (10 if is_login else 600):
                            raise Invalid("请求过于频繁，请一分钟后重试", 429)
                        attempts[key] = history + [now]
                if method == "POST" and path == "/api/admin/login":
                    value = body.get("password", "")
                    if not isinstance(value, str) or not hmac.compare_digest(hashlib.sha256(value.encode()).digest(), hashlib.sha256(password.encode()).digest()):
                        raise Invalid("管理员密码错误", 401)
                    token = secrets.token_urlsafe(32)
                    sessions.add(token)
                    self.reply(200, {"token": token})
                    return
                if method == "POST" and path == "/api/admin/logout":
                    sessions.discard(self.headers.get("Authorization", "").removeprefix("Bearer "))
                    self.reply(200, {"ok": True})
                    return
                if method == "GET" and path in ("/api/status", "/api/admin/state"):
                    db.execute("BEGIN")
                    teams = [dict(r) for r in db.execute("SELECT t.id,t.name,COUNT(m.id) AS n FROM teams t LEFT JOIN members m ON m.team_id=t.id GROUP BY t.id ORDER BY t.id")]
                    phase = db.execute("SELECT phase FROM settings").fetchone()[0]
                    result = {"phase": phase, "teams": teams, "submitted": db.execute("SELECT COUNT(*) FROM votes").fetchone()[0], "total": db.execute("SELECT COUNT(*) FROM members").fetchone()[0]}
                    if path == "/api/admin/state":
                        scores = {t["id"]: 0 for t in teams}
                        for vote in db.execute("SELECT vote1,vote2,weight FROM votes"):
                            scores[vote["vote1"]] += vote["weight"]
                            scores[vote["vote2"]] += vote["weight"]
                        result["ranking"] = sorted([{"id": t["id"], "name": t["name"], "score": scores[t["id"]]} for t in teams], key=lambda t: -t["score"])
                        result["members"] = [dict(r) for r in db.execute("SELECT m.id,m.name,m.team_id,m.code,(v.member_id IS NOT NULL) AS voted FROM members m LEFT JOIN votes v ON v.member_id=m.id ORDER BY m.id")]
                    self.reply(200, result)
                    return
                if method == "POST" and path == "/api/admin/teams":
                    db.execute("BEGIN IMMEDIATE")
                    if db.execute("SELECT phase FROM settings").fetchone()[0] == "open":
                        raise Invalid("请先暂停投票，再追加新队伍", 409)
                    name, names = body.get("name"), body.get("members")
                    if not isinstance(name, str) or not 1 <= len(name.strip()) <= 60:
                        raise Invalid("队伍名称需为 1～60 个字符")
                    if not isinstance(names, list) or not 1 <= len(names) <= 3 or any(not isinstance(n, str) or not 1 <= len(n.strip()) <= 40 for n in names):
                        raise Invalid("每队须录入 1～3 位成员，姓名长度不超过 40 字")
                    names = [n.strip() for n in names]
                    if len(set(names)) != len(names):
                        raise Invalid("同队成员姓名不能重复")
                    team_id = db.execute("INSERT INTO teams(name) VALUES (?)", (name.strip(),)).lastrowid
                    for member in names:
                        db.execute("INSERT INTO members(team_id,name,code) VALUES (?,?,?)", (team_id, member, secrets.token_hex(8).upper()))
                    db.commit()
                    self.reply(200, {"ok": True})
                    return
                if method == "POST" and path == "/api/admin/delete-team":
                    db.execute("BEGIN IMMEDIATE")
                    if db.execute("SELECT phase FROM settings").fetchone()[0] != "draft":
                        raise Invalid("投票开始后不能删除队伍", 409)
                    team_id = body.get("id")
                    if type(team_id) is not int:
                        raise Invalid("队伍编号无效")
                    db.execute("DELETE FROM teams WHERE id=?", (team_id,))
                    db.commit()
                    self.reply(200, {"ok": True})
                    return
                if method == "POST" and path == "/api/admin/phase":
                    db.execute("BEGIN IMMEDIATE")
                    current = db.execute("SELECT phase FROM settings").fetchone()[0]
                    next_phase = body.get("phase")
                    if (current, next_phase) not in (("draft", "open"), ("open", "closed"), ("closed", "open")):
                        raise Invalid("无效的投票状态切换")
                    if next_phase == "open" and db.execute("SELECT COUNT(*) FROM teams").fetchone()[0] < 3:
                        raise Invalid("至少需要三支队伍才能开始投票")
                    db.execute("UPDATE settings SET phase=?", (next_phase,))
                    db.commit()
                    self.reply(200, {"ok": True})
                    return
                if method == "POST" and path in ("/api/identify", "/api/vote"):
                    if path == "/api/vote":
                        db.execute("BEGIN IMMEDIATE")
                    raw_code = body.get("code", "")
                    if not isinstance(raw_code, str):
                        raise Invalid("投票码无效")
                    code = "".join(raw_code.split()).upper()
                    member = db.execute("SELECT m.id,m.name,m.team_id,t.name AS team_name FROM members m JOIN teams t ON t.id=m.team_id WHERE m.code=?", (code,)).fetchone()
                    if member is None:
                        raise Invalid("投票码无效，请向组织者核对", 401)
                    voted = db.execute("SELECT 1 FROM votes WHERE member_id=?", (member["id"],)).fetchone() is not None
                    if path == "/api/identify":
                        self.reply(200, {"name": member["name"], "team_id": member["team_id"], "team_name": member["team_name"], "voted": voted})
                        return
                    if db.execute("SELECT phase FROM settings").fetchone()[0] != "open":
                        raise Invalid("当前未开放投票", 409)
                    if voted:
                        raise Invalid("你已经投过票，每人只能提交一次", 409)
                    first, second = body.get("vote1"), body.get("vote2")
                    ids = {r[0] for r in db.execute("SELECT id FROM teams")}
                    if type(first) is not int or type(second) is not int or first not in ids or second not in ids:
                        raise Invalid("请完整选择两支队伍")
                    if first == second or member["team_id"] in (first, second):
                        raise Invalid("请选择两支不同的其他队伍，不能投给自己队伍")
                    count = db.execute("SELECT COUNT(*) FROM members WHERE team_id=?", (member["team_id"],)).fetchone()[0]
                    db.execute("INSERT INTO votes(member_id,vote1,vote2,weight) VALUES (?,?,?,?)", (member["id"], first, second, 1 / (2 * count)))
                    db.commit()
                    self.reply(200, {"ok": True, "message": "投票成功！你的两票已记录。"})
                    return
                raise Invalid("页面或接口不存在", 404)
            except Invalid as error:
                self.reply(error.status, {"error": error.message})
            except sqlite3.IntegrityError:
                self.reply(409, {"error": "名称重复或已提交投票，请刷新后核对"})
            except Exception:
                self.reply(500, {"error": "服务器暂时无法处理请求，请联系组织者"})
            finally:
                if db is not None:
                    db.close()

    return ThreadingHTTPServer((host, port), Handler)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="成员独立投票码投票系统")
    parser.add_argument("--host", default="0.0.0.0")
    parser.add_argument("--port", type=int, default=8000)
    parser.add_argument("--db", default=str(ROOT / "votes.sqlite3"))
    parser.add_argument("--trust-proxy", action="store_true", help="信任本机 Nginx 设置的 X-Real-IP，仅与 --host 127.0.0.1 配合使用")
    args = parser.parse_args()
    admin_password = os.environ.get("VOTE_ADMIN_PASSWORD") or secrets.token_urlsafe(18)
    if args.trust_proxy and args.host not in ("127.0.0.1", "::1"):
        parser.error("--trust-proxy 必须配合 --host 127.0.0.1 使用")
    server = make_server(args.host, args.port, args.db, admin_password, args.trust_proxy)
    print(f"页面地址：http://localhost:{args.port}", flush=True)
    if os.environ.get("VOTE_ADMIN_PASSWORD"):
        print("管理员密码：已从 VOTE_ADMIN_PASSWORD 加载", flush=True)
    else:
        print(f"管理员密码：{admin_password}", flush=True)
    print("独立运行请访问服务器 IP 和端口；经 Nginx 部署请使用配置的对外地址。", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
