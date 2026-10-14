"""sms-gateway: SMS and call log for the China Unicom SIM in the Quectel modem.

Asterisk (chan_quectel) owns the modem. This app talks to it over AMI only:
  QuectelNewSMSBase64  -> store + Bark push
  QuectelReport        -> delivery result of an SMS we sent (Payload = our id)
  UserEvent Gsm*       -> call log, emitted by extensions.conf
  QuectelSendSMS       <- "send" from the web page
"""
import asyncio
import base64
import contextlib
import glob
import hashlib
import hmac
import ipaddress
import json
import logging
import os
import re
import secrets
import shutil
import socket
import sqlite3
import threading
import time
from datetime import datetime, timezone
from urllib.parse import urlparse

import httpx
from cryptography.hazmat.primitives import padding
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
from fastapi import FastAPI, HTTPException, Request, Response
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles
import numinfo
from webauthn import (generate_authentication_options, generate_registration_options, options_to_json,
                      verify_authentication_response, verify_registration_response)
from webauthn.helpers import base64url_to_bytes, bytes_to_base64url
from webauthn.helpers.structs import (AuthenticatorSelectionCriteria, PublicKeyCredentialDescriptor,
                                      ResidentKeyRequirement, UserVerificationRequirement)

log = logging.getLogger("smsgw")
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

AMI_HOST = os.environ.get("AMI_HOST", "127.0.0.1")
AMI_PORT = int(os.environ.get("AMI_PORT", "5038"))
AMI_USER = os.environ.get("AMI_USER", "smsgw")
AMI_SECRET = os.environ["AMI_SECRET"]
MODEM = os.environ.get("MODEM", "quectel0")
SESSION_SECRET = os.environ["SESSION_SECRET"].encode()
SIP_PASSWORD = os.environ.get("SIP_PASSWORD", "")
DB_PATH = os.environ.get("DB_PATH", "/data/sms.db")
IMPORT_FILE = os.environ.get("IMPORT_FILE", "/data/mm-export-20261011.json")
# Shared with flexisip.conf [global::flexiapi] api-key; guards the push relay.
FLEXIAPI_ROUTER_KEY = os.environ.get("FLEXIAPI_ROUTER_KEY", "")
# Shared with Asterisk ([callcheck] asks /internal/call-check before ringing).
INTERNAL_KEY = os.environ.get("INTERNAL_KEY", "")
# Domain the phone's Linphone account lives on; the page dials through
# Linphone with sip-linphone:<number>@SIP_DOMAIN.
SIP_DOMAIN = os.environ.get("SIP_DOMAIN", "")
# Public address of this page; the 设置 page can override it (public_base_url).
PUBLIC_URL = os.environ.get("PUBLIC_URL") or f"https://{os.environ.get('SMS_DOMAIN', 'localhost')}"
# Credentials of Asterisk's "web" endpoint (the in-page WebRTC phone).
WEB_SIP_PASSWORD = os.environ.get("WEB_SIP_PASSWORD", "")
# Self-hosted bark-server (compose service "bark"). The phone registers through
# https://<public host>/bark (no auth needed for /register); pushing needs
# basic auth, which only this app has.
BARK_LOCAL = "http://127.0.0.1:18302/bark"
BARK_PUSH_PASSWORD = os.environ.get("BARK_PUSH_PASSWORD", "")
LINPHONE_API = "https://subscribe.linphone.org/api"

# Refuse to start with missing or toy secrets (an empty SESSION_SECRET would
# make session cookies forgeable, an empty router key would open the relay).
for _name in ("AMI_SECRET", "SESSION_SECRET", "SIP_PASSWORD", "FLEXIAPI_ROUTER_KEY", "INTERNAL_KEY",
              "WEB_SIP_PASSWORD", "BARK_PUSH_PASSWORD"):
    if len(os.environ.get(_name, "")) < 16:
        raise SystemExit(f"{_name} is missing or shorter than 16 characters (openssl rand -hex 24), see .env.example")
SESSION_TTL = 30 * 86400


# ---------------------------------------------------------------- storage --
_db_lock = threading.Lock()
_db = sqlite3.connect(DB_PATH, check_same_thread=False)
_db.row_factory = sqlite3.Row
_db.executescript("""
PRAGMA journal_mode=WAL;
CREATE TABLE IF NOT EXISTS messages (
    id INTEGER PRIMARY KEY,
    direction TEXT NOT NULL,          -- in | out
    number TEXT NOT NULL,
    body TEXT NOT NULL,
    ts TEXT NOT NULL,                 -- ISO 8601, UTC
    status TEXT NOT NULL DEFAULT '',  -- out: queued | sent | delivered | failed
    detail TEXT NOT NULL DEFAULT '',
    source TEXT NOT NULL DEFAULT 'modem'
);
CREATE INDEX IF NOT EXISTS messages_number ON messages(number, id);
CREATE TABLE IF NOT EXISTS calls (
    id INTEGER PRIMARY KEY,
    call_id TEXT UNIQUE,
    direction TEXT NOT NULL,          -- in | out
    number TEXT NOT NULL,
    started TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'ringing',
    billsec INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS number_tags (
    key TEXT PRIMARY KEY,             -- numinfo.key() form
    tag TEXT NOT NULL,
    updated TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS number_marks (
    key TEXT PRIMARY KEY,             -- numinfo.key() form
    tag TEXT NOT NULL DEFAULT '',     -- e.g. 广告营销 / 骚扰电话; '' = nothing found
    source TEXT NOT NULL DEFAULT '',  -- e.g. 用户标记 / 号码认证
    note TEXT NOT NULL DEFAULT '',
    fetched TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS passkeys (
    id TEXT PRIMARY KEY,              -- credential id, base64url
    public_key BLOB NOT NULL,
    sign_count INTEGER NOT NULL DEFAULT 0,
    name TEXT NOT NULL DEFAULT '',
    created TEXT NOT NULL,
    last_used TEXT NOT NULL DEFAULT ''
);
""")
for _col, _ddl in (("spam", "INTEGER NOT NULL DEFAULT 0"), ("spam_reason", "TEXT NOT NULL DEFAULT ''")):
    if _col not in [r[1] for r in _db.execute("PRAGMA table_info(messages)")]:
        _db.execute(f"ALTER TABLE messages ADD COLUMN {_col} {_ddl}")
_db.commit()


def db(sql, args=(), one=False):
    with _db_lock:
        cur = _db.execute(sql, args)
        _db.commit()
        rows = cur.fetchall()
        return (rows[0] if rows else None) if one else rows


def db_insert(sql, args):
    with _db_lock:
        cur = _db.execute(sql, args)
        _db.commit()
        return cur.lastrowid


def setting(key, default=""):
    row = db("SELECT value FROM settings WHERE key=?", (key,), one=True)
    return row["value"] if row else default


def set_setting(key, value):
    db("INSERT INTO settings(key, value) VALUES(?, ?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
       (key, value))


def now_iso():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def import_modemmanager_export():
    """One-off: the messages ModemManager had already received before the modem
    was handed to Asterisk (exported to IMPORT_FILE on 2026-10-11)."""
    if setting("imported_mm_export") or not os.path.exists(IMPORT_FILE):
        return
    with open(IMPORT_FILE, encoding="utf-8") as fh:
        items = json.load(fh)
    for it in items:
        ts = it.get("timestamp") or now_iso()
        with contextlib.suppress(ValueError):
            ts = datetime.fromisoformat(ts).astimezone(timezone.utc).isoformat(timespec="seconds")
        db_insert("INSERT INTO messages(direction, number, body, ts, source) VALUES('in', ?, ?, ?, 'modemmanager')",
                  (it.get("number") or "", it.get("text") or "", ts))
    set_setting("imported_mm_export", now_iso())
    log.info("imported %d messages from %s", len(items), IMPORT_FILE)


# ------------------------------------------------------------------ spam --
# Mainland marketing SMS must carry an opt-out phrase by regulation, which makes
# it the most reliable marker. Verification codes are never auto-blocked.
MARKETING_RE = re.compile(
    r"(回复?\s*[TtＴｔ]\s*退订|退订\s*回复?\s*[TtＴｔ]|回\s*TD\s*退订|退订\s*回?\s*TD|TD\s*退订"
    r"|拒收\s*请?\s*回复?\s*[RrＲｒ]|回\s*[NnＮｎ]\s*退订|退订\s*请?回复|STOP\s*退订)")
# Banks and carriers append the same opt-out phrase to account notices, so an
# opt-out phrase only counts as marketing together with promotional wording and
# without account/service wording.
PROMO_RE = re.compile(r"优惠|促销|特价|折扣|[0-9一二三四五六七八九]折|领取|领券|优惠券|红包|福利|抢购|秒杀|限时|爆款|上新"
                      r"|会员日|大促|满减|免费送|送礼|中奖|恭喜您?获得|贷款|额度|借款|低息|点击|戳|https?://|\.cn/|\.com/|t\.cn"
                      r"|调研|问卷|诚邀|邀您|抽奖|[A-Za-z0-9-]+\.[A-Za-z]{2,}/\S")
SERVICE_RE = re.compile(r"尾号|余额|消费|支出|收入|入账|到账|转账|还款|账单|交易|话费|流量|套餐|欠费|缴费|快递|取件|签收|派送"
                        r"|订单号|物流|航班|车次|预约|挂号")


def canon(num):
    """Comparable form of a sender/caller number (chan_quectel hands over
    +86..., 86..., 0081..., bare mainland mobiles, or short codes)."""
    n = re.sub(r"[^0-9+]", "", num or "")
    if n.startswith("00"):
        n = "+" + n[2:]
    elif n.startswith("86") and len(n) == 13 and n[2] == "1":
        n = "+" + n
    elif len(n) == 11 and n.startswith("1"):
        n = "+86" + n
    return n


def parse_rules(text):
    """One rule per line: a number, a number prefix ending in '*', or
    'kw:' / '关键词:' followed by a keyword (SMS only). '#' starts a comment."""
    numbers, prefixes, keywords = set(), [], []
    for line in (text or "").splitlines():
        line = line.split("#", 1)[0].strip()
        if not line:
            continue
        if line.lower().startswith("kw:") or line.startswith(("关键词:", "关键词：")):
            kw = re.split(r"[:：]", line, 1)[1].strip()
            if kw:
                keywords.append(kw)
        elif line.endswith("*"):
            pre = re.sub(r"[^0-9+]", "", line[:-1])
            if pre:
                prefixes.append(pre)
                # "170*" also means mainland +86170..., "0081*" means +81...
                if pre.startswith("00"):
                    prefixes.append("+" + pre[2:])
                elif pre.startswith("1") and not pre.startswith("10"):
                    prefixes.append("+86" + pre)
        else:
            numbers.add(canon(line))
    return numbers, prefixes, keywords


def variants(num):
    """95588, +8695588 and 8695588 are the same short code; 02112345678 and
    +862112345678 the same landline. Compare every plausible spelling."""
    c = canon(num)
    out = {c, re.sub(r"[^0-9+]", "", num or "")}
    if c.startswith("+86"):
        rest = c[3:]
        out |= {rest, "86" + rest}
        if not rest.startswith("1"):
            out.add("0" + rest)
    elif c and not c.startswith("+"):
        out |= {"+86" + c.lstrip("0"), "86" + c.lstrip("0")}
    out.discard("")
    return out


def number_matches(num, numbers, prefixes):
    vs = variants(num)
    rule_vs = set().union(*(variants(n) for n in numbers)) if numbers else set()
    return bool(vs & rule_vs) or any(v.startswith(p) for v in vs for p in prefixes)


def classify_sms(number, body):
    a_nums, a_pre, a_kw = parse_rules(setting("allow_rules"))
    if number_matches(number, a_nums, a_pre) or any(k in body for k in a_kw):
        return False, ""
    b_nums, b_pre, b_kw = parse_rules(setting("block_rules"))
    if number_matches(number, b_nums, b_pre):
        return True, "黑名单号码"
    keywords = b_kw + [k.strip() for k in setting("block_keywords").splitlines() if k.strip()]
    for k in keywords:
        if k.lower() in body.lower():
            return True, f"关键词「{k}」"
    if extract_code(body):
        return False, ""
    if (setting("block_marketing", "1") == "1" and MARKETING_RE.search(body)
            and PROMO_RE.search(body) and not SERVICE_RE.search(body)):
        return True, "营销短信"
    return False, ""


# Call screening modes: "all" rejects everyone but the allowlist, "smart" also
# rejects numbers Baidu users marked as spam/fraud/sales, "list" only the
# blocklist. Before call_mode existed there was the block_all_calls switch.
CALL_MODES = ("smart", "all", "list")
SPAM_MARK_RE = re.compile(r"骚扰|诈骗|欺诈|营销|推销|广告|催收|中介|贷款|保险|理财|违规")


def call_mode():
    mode = setting("call_mode")
    return mode if mode in CALL_MODES else "all" if setting("block_all_calls", "1") == "1" else "list"


def screen_call(number, mark=""):
    mode = call_mode()
    if not re.search(r"\d{3,}", number or ""):
        if mode == "all":
            return "block", "拦截所有来电"
        return ("block", "隐藏号码") if setting("block_anonymous", "0") == "1" else ("allow", "")
    a_nums, a_pre, _ = parse_rules(setting("allow_rules"))
    if number_matches(number, a_nums, a_pre):
        return "allow", ""
    if mode == "all":
        return "block", "拦截所有来电"
    b_nums, b_pre, _ = parse_rules(setting("block_rules"))
    if number_matches(number, b_nums, b_pre):
        return "block", "黑名单号码"
    if mode == "smart" and mark and SPAM_MARK_RE.search(mark):
        return "block", f"被标记为「{mark}」"
    return "allow", ""


def rule_info(line):
    """How the settings page shows one block/allow rule."""
    rule, _, note = (x.strip() for x in line.partition("#"))
    if rule.lower().startswith("kw:") or rule.startswith(("关键词:", "关键词：")):
        return {"kind": "kw", "display": re.split(r"[:：]", rule, 1)[1].strip(), "sub": "短信关键词", "note": note}
    if rule.endswith("*"):
        return {"kind": "prefix", "display": rule, "sub": f"所有 {rule[:-1]} 开头的号码", "note": note}
    info = numinfo.lookup(rule)
    sub = " · ".join(x for x in (info["name"], info["loc"], info["carrier"] or info["category"]) if x)
    return {"kind": "number", "display": info["display"], "sub": sub, "note": note}


def add_rule(key, rule):
    lines = [l for l in setting(key).splitlines() if l.strip()]
    if rule not in [l.split("#", 1)[0].strip() for l in lines]:
        lines.append(rule)
    set_setting(key, "\n".join(lines))


# ------------------------------------------------------------------- push --
CODE_RE = re.compile(r"(?:验证码|校验码|动态码|确认码|代码|驗證碼|code|Code|CODE)[^0-9A-Za-z]{0,12}([0-9]{4,8})")
CODE_FALLBACK_RE = re.compile(r"(?<![0-9])([0-9]{4,8})(?![0-9])")


def extract_code(text):
    m = CODE_RE.search(text)
    if m:
        return m.group(1)
    if re.search(r"验证|校验|code|驗證", text, re.I):
        m = CODE_FALLBACK_RE.search(text)
        if m:
            return m.group(1)
    return ""


def bark_encrypt(payload: dict, key: str):
    iv = secrets.token_hex(8)  # 16 ASCII chars, sent alongside as `iv`
    padder = padding.PKCS7(128).padder()
    data = padder.update(json.dumps(payload, ensure_ascii=False).encode()) + padder.finalize()
    enc = Cipher(algorithms.AES(key.encode()), modes.CBC(iv.encode())).encryptor()
    return base64.b64encode(enc.update(data) + enc.finalize()).decode(), iv


def own_host():
    return urlparse(setting("public_base_url") or PUBLIC_URL).hostname


def bark_devices():
    """[{name, server, key}] to push to. Falls back to the single-device
    settings (bark_server/bark_key) used before multi-device support."""
    raw = setting("bark_devices")
    if raw:
        return json.loads(raw)
    if setting("bark_key"):
        return [{"name": "", "server": setting("bark_server", "https://api.day.app").rstrip("/"),
                 "key": setting("bark_key")}]
    return []


def parse_bark_device(line):
    """'[name] <Bark test URL or bare key>' -> {name, server, key}. A URL on our
    own host under /bark means the local bark-server; a bare key too."""
    words = line.split()
    m = re.match(r"(https?://([^/]+)(/[^/]+)*?)/([A-Za-z0-9]{12,})(/|$)", words[-1])
    if m:
        server = BARK_LOCAL if m.group(2) == own_host() and m.group(3) == "/bark" else m.group(1)
        key = m.group(4)
    elif re.fullmatch(r"[A-Za-z0-9]{12,}", words[-1]):
        server, key = BARK_LOCAL, words[-1]
    else:
        raise HTTPException(400, f"这一行看不懂：{line}（要粘贴 Bark App 里的测试链接）")
    return {"name": " ".join(words[:-1]), "server": server, "key": key}


def bark_device_line(d):
    server = f"https://{own_host()}/bark" if d["server"] == BARK_LOCAL else d["server"]
    return f"{d['name']} {server}/{d['key']}".strip()


async def bark_push(title, body, group, level="active", copy="", url="", devices=None):
    """Push via Bark to every configured device. Content (title/body/copy/url)
    is AES-256-CBC encrypted end-to-end when an encryption key is configured;
    only group/level, which the Bark server needs to build the APNs payload,
    travel in the clear. Returns "" if all devices got it, else what failed."""
    devices = bark_devices() if devices is None else devices
    if not devices:
        return "bark not configured"
    content = {"title": title, "body": body}
    if copy:
        content["copy"] = copy
    if url:
        content["url"] = url
    req = {"group": group, "level": level, "isArchive": "1"}
    enc_key = setting("bark_enc_key")
    if enc_key:
        req["ciphertext"], req["iv"] = bark_encrypt(content, enc_key)
    else:
        req.update(content)
    async def one(client, d):
        name = d["name"] or d["key"][:4] + "…"
        auth = ("smsgw", BARK_PUSH_PASSWORD) if d["server"] == BARK_LOCAL else None
        try:
            r = await client.post(f"{d['server']}/{d['key']}", json=req, auth=auth)
            if r.status_code != 200:
                log.warning("bark push to %s failed: %s %s", name, r.status_code, r.text[:200])
                return f"{name}：bark http {r.status_code}"
            return ""
        except httpx.HTTPError as e:
            log.warning("bark push to %s error: %s", name, e)
            return f"{name}：bark error {e.__class__.__name__}"

    async with httpx.AsyncClient(timeout=15) as client:
        errors = await asyncio.gather(*(one(client, d) for d in devices))
    return "；".join(e for e in errors if e)


def thread_url(number):
    base = setting("public_base_url").rstrip("/")
    return f"{base}/#n={number}" if base else ""


# -------------------------------------------------------------------- AMI --
class AMI:
    def __init__(self):
        self.writer = None
        self.pending = {}
        self.connected = False
        self.modem_status = {}

    async def run(self):
        while True:
            try:
                await self._session()
            except Exception as e:  # noqa: BLE001 - reconnect on anything
                log.warning("AMI session ended: %s", e)
            self.connected = False
            for fut in self.pending.values():
                if not fut.done():
                    fut.set_exception(ConnectionError("AMI disconnected"))
            self.pending.clear()
            await asyncio.sleep(5)

    async def _session(self):
        reader, self.writer = await asyncio.open_connection(AMI_HOST, AMI_PORT)
        await reader.readline()  # banner
        login = asyncio.ensure_future(self.action("Login", Username=AMI_USER, Secret=AMI_SECRET, Events="call,user,system"))
        reader_task = asyncio.ensure_future(self._read(reader))
        resp = await login
        if resp.get("Response") != "Success":
            raise ConnectionError(f"AMI login failed: {resp.get('Message')}")
        self.connected = True
        log.info("AMI connected")
        asyncio.ensure_future(self.refresh_status())
        await reader_task

    async def _read(self, reader):
        block = {}
        while True:
            line = await reader.readline()
            if not line:
                raise ConnectionError("AMI EOF")
            line = line.decode("utf-8", "replace").rstrip("\r\n")
            if line:
                k, _, v = line.partition(":")
                block[k.strip()] = v.strip()
                continue
            if not block:
                continue
            msg, block = block, {}
            aid = msg.get("ActionID")
            if "Response" in msg and aid in self.pending:
                fut = self.pending.pop(aid)
                if not fut.done():
                    fut.set_result(msg)
            elif "Event" in msg:
                try:
                    await on_event(msg)
                except Exception:  # noqa: BLE001
                    log.exception("event handler failed: %s", msg.get("Event"))

    async def action(self, name, **headers):
        if not self.writer:
            raise ConnectionError("AMI not connected")
        aid = secrets.token_hex(6)
        fut = asyncio.get_running_loop().create_future()
        self.pending[aid] = fut
        lines = [f"Action: {name}", f"ActionID: {aid}"] + [f"{k}: {v}" for k, v in headers.items()]
        self.writer.write(("\r\n".join(lines) + "\r\n\r\n").encode())
        await self.writer.drain()
        return await asyncio.wait_for(fut, 20)

    async def refresh_status(self):
        """QuectelShowDevices streams one QuectelDeviceEntry event per modem."""
        with contextlib.suppress(Exception):
            await self.action("QuectelShowDevices")


ami = AMI()

STATUS_FIELDS = ("Device", "State", "Voice", "SMS", "Model", "Firmware", "GSMRegistrationStatus", "RSSI",
                 "Mode", "Submode", "ProviderName", "SubscriberNumber", "AudioState", "CurrentDeviceState")


async def on_event(ev):
    name = ev.get("Event")
    if name == "QuectelNewSMSBase64":
        number = ev.get("From", "")
        try:
            body = base64.b64decode(ev.get("Message", "")).decode("utf-8", "replace")
        except ValueError:
            body = ev.get("Message", "")
        mark_soon(number)
        spam, reason = classify_sms(number, body)
        mid = db_insert("INSERT INTO messages(direction, number, body, ts, spam, spam_reason) "
                        "VALUES('in', ?, ?, ?, ?, ?)", (number, body, now_iso(), int(spam), reason))
        log.info("sms in id=%s%s", mid, f" spam ({reason})" if spam else "")
        if spam:
            base = setting("public_base_url").rstrip("/")
            await bark_push(f"已拦截短信 · {number}", f"{reason}：{body[:100]}", "blocked-sms",
                            level="passive", url=f"{base}/#v=spam" if base else "")
            return
        code = extract_code(body)
        await bark_push(f"短信 · {number}", body, "sms",
                        level="timeSensitive" if code else "active", copy=code, url=thread_url(number))
    elif name == "QuectelReport":
        payload = ev.get("Payload", "")
        if payload.isdigit():
            ok = ev.get("Success") == "1"
            kind = ev.get("Type", "")
            status = {"0": "sent", "1": "delivered", "2": "failed"}.get(kind, "sent") if ok else "failed"
            db("UPDATE messages SET status=?, detail=? WHERE id=? AND direction='out'",
               (status, ev.get("Report", ""), int(payload)))
    elif name == "UserEvent":
        await on_call_event(ev)
    elif name == "QuectelDeviceEntry":
        ami.modem_status = {k: ev.get(k, "") for k in STATUS_FIELDS}
    elif name == "QuectelStatus":
        asyncio.ensure_future(ami.refresh_status())


async def on_call_event(ev):
    kind = ev.get("UserEvent")
    call_id = ev.get("CallId", "")
    if kind in ("GsmIncoming", "GsmOutgoing"):
        direction = "in" if kind == "GsmIncoming" else "out"
        number = ev.get("Number", "")
        db("INSERT OR IGNORE INTO calls(call_id, direction, number, started) VALUES(?, ?, ?, ?)",
           (call_id, direction, number, now_iso()))
        if direction == "in":
            mark_soon(number)
            await bark_push(f"来电 · {number}", "国内号码来电，SIP 客户端正在响铃", "call",
                            level="timeSensitive", url=thread_url(number))
    elif kind == "GsmBlocked":
        db("INSERT OR IGNORE INTO calls(call_id, direction, number, started, status) VALUES(?, 'in', ?, ?, 'blocked')",
           (call_id, ev.get("Number", ""), now_iso()))
        log.info("call blocked id=%s", call_id)
        mark_soon(ev.get("Number", ""))
        # Silent record on the phone: "passive" adds it to Notification Center
        # (and Bark's history) without sound or lighting the screen.
        number = ev.get("Number", "")
        base = setting("public_base_url").rstrip("/")
        await bark_push(f"已拦截来电 · {number or '隐藏号码'}", f"{screen_call(number, cached_mark_tag(number))[1] or '已拦截'}，未响铃",
                        "blocked-call", level="passive", url=f"{base}/#v=spam" if base else "")
    elif kind == "GsmCallEnd":
        row = db("SELECT * FROM calls WHERE call_id=?", (call_id,), one=True)
        if not row or row["status"] == "blocked":
            return
        billsec = int(ev.get("Billsec") or 0)
        dial = ev.get("DialStatus", "")
        status = "answered" if billsec > 0 or dial == "ANSWER" else (
            "missed" if row["direction"] == "in" else (dial.lower() or "failed"))
        db("UPDATE calls SET status=?, billsec=? WHERE call_id=?", (status, billsec, call_id))
        if row["direction"] == "in" and status == "missed":
            await bark_push(f"未接来电 · {row['number']}", "国内号码有未接来电", "call",
                            level="timeSensitive", url=thread_url(row["number"]))


# ----------------------------------------------------------------- numbers --
def normalize_number(raw):
    """Same rules as [from-iphone] in extensions.conf: the SIM is roaming in
    Japan, so send in international form; bare numbers are mainland ones."""
    n = re.sub(r"[\s\-()]", "", raw or "")
    if not re.fullmatch(r"\+?[0-9]{3,20}", n):
        raise HTTPException(400, "号码格式不对")
    if n.startswith("+"):
        return n
    if n.startswith("00"):
        return "+" + n[2:]
    if n.startswith("0"):
        return "+86" + n[1:]
    return "+86" + n


# -------------------------------------------------------------------- auth --
def password_hash(pw, salt=None):
    salt = salt or secrets.token_bytes(16)
    h = hashlib.scrypt(pw.encode(), salt=salt, n=2 ** 14, r=8, p=1)
    return base64.b64encode(salt).decode() + "$" + base64.b64encode(h).decode()


def password_ok(pw, stored):
    with contextlib.suppress(ValueError):
        salt_b64, _ = stored.split("$", 1)
        return hmac.compare_digest(password_hash(pw, base64.b64decode(salt_b64)), stored)
    return False


def make_session():
    exp = str(int(time.time()) + SESSION_TTL)
    gen = setting("session_gen", "0")
    msg = f"{exp}.{gen}"
    return msg + "." + hmac.new(SESSION_SECRET, msg.encode(), hashlib.sha256).hexdigest()


def session_valid(tok):
    with contextlib.suppress(ValueError):
        exp, gen, sig = tok.split(".")
        good = hmac.new(SESSION_SECRET, f"{exp}.{gen}".encode(), hashlib.sha256).hexdigest()
        return hmac.compare_digest(sig, good) and int(exp) > time.time() and gen == setting("session_gen", "0")
    return False


def via_public_gateway(request: Request):
    """Caddy marks everything it proxies from the internet (SMS_DOMAIN);
    Cloudflare adds CF-Connecting-IP. Such requests reach us from 127.0.0.1,
    so the peer address alone would wrongly look private."""
    return request.headers.get("x-smsgw-public") == "1" or "cf-connecting-ip" in request.headers


def client_ip(request: Request):
    cf = request.headers.get("cf-connecting-ip")
    if cf:
        return cf.strip()
    return request.client.host if request.client else ""


def is_private_request(request: Request):
    if via_public_gateway(request):
        return False
    with contextlib.suppress(ValueError):
        a = ipaddress.ip_address(client_ip(request))
        return a.is_private or a in ipaddress.ip_network("100.64.0.0/10")
    return False


_fails = {}
_fails_all = []
FAIL_WINDOW, FAIL_PER_IP, FAIL_GLOBAL = 900, 8, 30


def check_rate(ip, global_limit=True):
    """Per-IP limit, plus a global one for password guesses: the client IP may
    come from CF-Connecting-IP, which can be forged by anyone who reaches the
    origin directly. Passkey logins skip the global limit so the owner can
    still get in while someone is hammering the password form."""
    now = time.time()
    for k in [k for k, v in _fails.items() if not v or now - v[-1] >= FAIL_WINDOW]:
        del _fails[k]
    hits = [t for t in _fails.get(ip, []) if now - t < FAIL_WINDOW]
    if hits:
        _fails[ip] = hits
    if len(hits) >= FAIL_PER_IP:
        raise HTTPException(429, "尝试次数太多，15 分钟后再试")
    _fails_all[:] = [t for t in _fails_all if now - t < FAIL_WINDOW]
    if global_limit and len(_fails_all) >= FAIL_GLOBAL:
        log.warning("login: global failure limit reached, password login paused")
        raise HTTPException(429, "最近密码错误次数太多，密码登录暂停 15 分钟（通行密钥登录不受影响）")


def record_fail(ip, global_count=True):
    _fails.setdefault(ip, []).append(time.time())
    if global_count:
        _fails_all.append(time.time())


def cookie_secure(request: Request):
    """Secure cookies whenever the page was reached over HTTPS (through
    Caddy); plain-HTTP LAN access (http://LAN_IP:18300) must still work."""
    return request.headers.get("x-forwarded-proto") == "https"


def require_password(request: Request, pw):
    """Re-check the login password for sensitive changes (new password,
    new passkey); failures count towards the login rate limit."""
    ip = client_ip(request)
    check_rate(ip)
    if not password_ok(pw or "", setting("password_hash")):
        record_fail(ip)
        raise HTTPException(400, "当前密码不对")


def require_auth(request: Request):
    if request.headers.get("x-requested-with") != "fetch":
        raise HTTPException(403, "bad request")
    if not session_valid(request.cookies.get("smsgw", "")):
        raise HTTPException(401, "未登录")


# --------------------------------------------------------------------- web --
app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)


@app.on_event("startup")
async def _startup():
    import_modemmanager_export()
    asyncio.ensure_future(ami.run())
    asyncio.ensure_future(backfill_marks())
    asyncio.ensure_future(watchdog())


@app.get("/", response_class=HTMLResponse)
async def index():
    with open(os.path.join(os.path.dirname(__file__), "index.html"), encoding="utf-8") as fh:
        return HTMLResponse(fh.read(), headers={"Cache-Control": "no-store", "X-Frame-Options": "DENY",
                                                "Content-Security-Policy": "frame-ancestors 'none'",
                                                "Referrer-Policy": "no-referrer", "X-Content-Type-Options": "nosniff"})


# UI library (Ionic, self-hosted). Public files, no auth; lazy-loaded chunks
# have content hashes in their names.
app.mount("/vendor", StaticFiles(directory=os.path.join(os.path.dirname(__file__), "vendor")), name="vendor")

_STATIC = {"manifest.webmanifest": "application/manifest+json", "icon-180.png": "image/png", "icon-512.png": "image/png"}


@app.get("/{name}")
async def static_file(name: str):
    if name not in _STATIC:
        raise HTTPException(404)
    with open(os.path.join(os.path.dirname(__file__), name), "rb") as fh:
        return Response(fh.read(), media_type=_STATIC[name], headers={"Cache-Control": "public, max-age=86400"})


@app.get("/api/state")
async def state(request: Request):
    return {"setup_needed": not setting("password_hash"),
            "setup_allowed": is_private_request(request),
            "logged_in": session_valid(request.cookies.get("smsgw", "")),
            "passkey_rp": rp()[0], "has_passkeys": bool(db("SELECT 1 FROM passkeys LIMIT 1")), "sip_domain": SIP_DOMAIN}


@app.post("/api/setup")
async def setup(request: Request):
    if request.headers.get("x-requested-with") != "fetch":
        raise HTTPException(403, "bad request")
    if setting("password_hash"):
        raise HTTPException(409, "已经设置过密码")
    if not is_private_request(request):
        raise HTTPException(403, "首次设置只能在家里局域网或 Tailscale 里进行")
    pw = (await request.json()).get("password", "")
    if len(pw) < 10:
        raise HTTPException(400, "密码至少 10 位")
    set_setting("password_hash", password_hash(pw))
    resp = Response(status_code=204)
    resp.set_cookie("smsgw", make_session(), max_age=SESSION_TTL, httponly=True, samesite="strict",
                    secure=cookie_secure(request))
    return resp


@app.post("/api/login")
async def login(request: Request):
    if request.headers.get("x-requested-with") != "fetch":
        raise HTTPException(403, "bad request")
    ip = client_ip(request)
    check_rate(ip)
    pw = (await request.json()).get("password", "")
    stored = setting("password_hash")
    if not stored or not password_ok(pw, stored):
        record_fail(ip)
        raise HTTPException(400, "密码不对")
    resp = Response(status_code=204)
    resp.set_cookie("smsgw", make_session(), max_age=SESSION_TTL, httponly=True, samesite="strict",
                    secure=cookie_secure(request))
    return resp


# --------------------------------------------------------------- passkeys --
# WebAuthn passkeys as a second way to log in (the password keeps working).
# The relying party is the public page (public_base_url, i.e. SMS_DOMAIN), so
# passkeys only work when the page is opened under that name over HTTPS.
_challenges = {}  # cookie token -> (kind, challenge bytes, expiry)


def rp():
    u = urlparse(setting("public_base_url") or PUBLIC_URL)
    return u.hostname, f"{u.scheme}://{u.netloc}"


def new_challenge(resp: Response, kind, challenge):
    now = time.time()
    for k in [k for k, v in _challenges.items() if v[2] < now]:
        del _challenges[k]
    tok = secrets.token_urlsafe(24)
    _challenges[tok] = (kind, challenge, now + 300)
    resp.set_cookie("smsgw_pk", tok, max_age=300, httponly=True, samesite="strict", secure=True)  # passkeys need HTTPS anyway


def take_challenge(request: Request, kind):
    v = _challenges.pop(request.cookies.get("smsgw_pk", ""), None)
    if not v or v[0] != kind or v[2] < time.time():
        raise HTTPException(400, "验证已过期，请重试")
    return v[1]


@app.post("/api/passkey/register-options")
async def passkey_register_options(request: Request):
    require_auth(request)
    require_password(request, (await request.json()).get("password", ""))
    rp_id, _ = rp()
    user_id = setting("webauthn_user_id")
    if not user_id:
        user_id = bytes_to_base64url(secrets.token_bytes(16))
        set_setting("webauthn_user_id", user_id)
    opts = generate_registration_options(
        rp_id=rp_id, rp_name="国内号码", user_id=base64url_to_bytes(user_id), user_name="国内号码",
        user_display_name="国内号码（短信网关）",
        authenticator_selection=AuthenticatorSelectionCriteria(resident_key=ResidentKeyRequirement.REQUIRED,
                                                               user_verification=UserVerificationRequirement.REQUIRED),
        exclude_credentials=[PublicKeyCredentialDescriptor(id=base64url_to_bytes(r["id"])) for r in db("SELECT id FROM passkeys")])
    resp = Response(options_to_json(opts), media_type="application/json")
    new_challenge(resp, "register", opts.challenge)
    return resp


@app.post("/api/passkey/register")
async def passkey_register(request: Request):
    require_auth(request)
    data = await request.json()
    rp_id, origin = rp()
    try:
        v = verify_registration_response(credential=data["credential"], expected_challenge=take_challenge(request, "register"),
                                         expected_rp_id=rp_id, expected_origin=origin, require_user_verification=True)
    except HTTPException:
        raise
    except Exception as e:
        log.warning("passkey registration rejected: %s", e)
        raise HTTPException(400, "通行密钥校验失败")
    name = (data.get("name") or "").strip()[:40] or "通行密钥"
    db("INSERT INTO passkeys(id, public_key, sign_count, name, created) VALUES(?, ?, ?, ?, ?)",
       (bytes_to_base64url(v.credential_id), v.credential_public_key, v.sign_count, name, now_iso()))
    log.info("passkey added (%s)", name)
    resp = Response(status_code=204)
    resp.delete_cookie("smsgw_pk")
    return resp


@app.post("/api/passkey/login-options")
async def passkey_login_options(request: Request):
    if request.headers.get("x-requested-with") != "fetch":
        raise HTTPException(403, "bad request")
    check_rate(client_ip(request), global_limit=False)
    rp_id, _ = rp()
    # No allowCredentials: the phone offers whichever passkey it has for us.
    opts = generate_authentication_options(rp_id=rp_id, user_verification=UserVerificationRequirement.REQUIRED)
    resp = Response(options_to_json(opts), media_type="application/json")
    new_challenge(resp, "login", opts.challenge)
    return resp


@app.post("/api/passkey/login")
async def passkey_login(request: Request):
    if request.headers.get("x-requested-with") != "fetch":
        raise HTTPException(403, "bad request")
    ip = client_ip(request)
    check_rate(ip, global_limit=False)
    cred = (await request.json()).get("credential") or {}
    challenge = take_challenge(request, "login")
    row = db("SELECT * FROM passkeys WHERE id = ?", (str(cred.get("id", "")),), one=True)
    rp_id, origin = rp()
    try:
        if not row:
            raise ValueError("unknown credential")
        v = verify_authentication_response(credential=cred, expected_challenge=challenge, expected_rp_id=rp_id,
                                           expected_origin=origin, credential_public_key=row["public_key"],
                                           credential_current_sign_count=row["sign_count"], require_user_verification=True)
    except Exception as e:
        record_fail(ip, global_count=False)
        log.warning("passkey login rejected: %s", e)
        raise HTTPException(400, "这个通行密钥不能登录（可能已在设置里删除）")
    db("UPDATE passkeys SET sign_count = ?, last_used = ? WHERE id = ?", (v.new_sign_count, now_iso(), row["id"]))
    resp = Response(status_code=204)
    resp.delete_cookie("smsgw_pk")
    resp.set_cookie("smsgw", make_session(), max_age=SESSION_TTL, httponly=True, samesite="strict",
                    secure=cookie_secure(request))
    return resp


@app.get("/api/passkeys")
async def passkey_list(request: Request):
    require_auth(request)
    return [{"id": r["id"], "name": r["name"], "created": r["created"], "last_used": r["last_used"]}
            for r in db("SELECT * FROM passkeys ORDER BY created")]


@app.post("/api/passkey/delete")
async def passkey_delete(request: Request):
    require_auth(request)
    db("DELETE FROM passkeys WHERE id = ?", (str((await request.json()).get("id", "")),))
    return Response(status_code=204)


@app.post("/api/logout")
async def logout():
    resp = Response(status_code=204)
    resp.delete_cookie("smsgw")
    return resp


@app.post("/api/logout-all")
async def logout_all(request: Request):
    require_auth(request)
    set_setting("session_gen", str(int(setting("session_gen", "0")) + 1))
    return Response(status_code=204)


@app.get("/api/threads")
async def threads(request: Request):
    require_auth(request)
    rows = db("""SELECT m.number, m.body, m.ts, m.direction, c.n FROM messages m
                 JOIN (SELECT number, MAX(id) AS last, COUNT(*) AS n FROM messages WHERE spam = 0 GROUP BY number) c
                   ON m.id = c.last ORDER BY m.id DESC""")
    return with_info([dict(r) for r in rows])


@app.get("/api/messages")
async def messages(request: Request, number: str = "", q: str = "", before: int = 0, limit: int = 50):
    require_auth(request)
    limit = max(1, min(limit, 200))
    where, args = [], []
    if number:
        where.append("number = ?"); args.append(number)
    if q:
        where.append("(body LIKE ? OR number LIKE ?)"); args += [f"%{q}%", f"%{q}%"]
    if before:
        where.append("id < ?"); args.append(before)
    sql = "SELECT * FROM messages" + (" WHERE " + " AND ".join(where) if where else "") + " ORDER BY id DESC LIMIT ?"
    return [dict(r) for r in db(sql, args + [limit])]


@app.post("/api/send")
async def send(request: Request):
    require_auth(request)
    data = await request.json()
    number = normalize_number(data.get("number", ""))
    body = (data.get("body") or "").strip()
    if not body:
        raise HTTPException(400, "内容不能为空")
    if len(body) > 390:
        raise HTTPException(400, "内容太长（最多 390 字）")
    mid = db_insert("INSERT INTO messages(direction, number, body, ts, status) VALUES('out', ?, ?, ?, 'queued')",
                    (number, body, now_iso()))
    escaped = body.replace("\\", "\\\\").replace("\r", "").replace("\n", "\\n")
    try:
        r = await ami.action("QuectelSendSMS", Device=MODEM, Number=number, Message=escaped,
                             Validity="1440", Report="yes", Payload=str(mid))
    except Exception as e:  # noqa: BLE001
        db("UPDATE messages SET status='failed', detail=? WHERE id=?", (str(e), mid))
        raise HTTPException(503, "模块未就绪，没有发出") from e
    if r.get("Response") != "Success":
        db("UPDATE messages SET status='failed', detail=? WHERE id=?", (r.get("Message", ""), mid))
        raise HTTPException(502, r.get("Message") or "发送失败")
    return {"id": mid, "number": number}


@app.get("/api/calls")
async def calls(request: Request, limit: int = 100):
    require_auth(request)
    return with_info([dict(r) for r in db("SELECT * FROM calls ORDER BY id DESC LIMIT ?", (max(1, min(limit, 500)),))])


@app.get("/api/status")
async def status(request: Request):
    require_auth(request)
    await ami.refresh_status()
    await asyncio.sleep(0.5)
    return {"ami": ami.connected, "modem": ami.modem_status}


# ---------------------------------------------------------------- health --
# Everything here reads the host's /proc and /sys (not namespaced for these
# files, and /sys is visible read-only in the container), so no extra mounts.
def _read(path):
    try:
        with open(path) as fh:
            return fh.read().strip()
    except OSError:
        return ""


def _level(value, warn, bad):
    return "bad" if value >= bad else "warn" if value >= warn else "ok"


_cpu_prev = None  # (time, total jiffies, idle jiffies)


async def cpu_percent():
    """CPU busy % since the previous call (the page polls every few seconds);
    takes a fresh 0.3 s sample when the last one is stale."""
    global _cpu_prev

    def sample():
        v = [int(x) for x in _read("/proc/stat").splitlines()[0].split()[1:]]
        return time.monotonic(), sum(v), v[3] + v[4]

    if _cpu_prev is None or time.monotonic() - _cpu_prev[0] > 30:
        _cpu_prev = sample()
        await asyncio.sleep(0.3)
    cur = sample()
    total, idle = cur[1] - _cpu_prev[1], cur[2] - _cpu_prev[2]
    _cpu_prev = cur
    return round(100 * (1 - idle / total), 1) if total > 0 else 0.0


def read_temps():
    temps = []
    for h in sorted(glob.glob("/sys/class/hwmon/hwmon*")):
        name, raw = _read(f"{h}/name"), _read(f"{h}/temp1_input")
        if not raw.lstrip("-").isdigit():
            continue
        c = round(int(raw) / 1000, 1)
        if name == "k10temp":
            temps.insert(0, {"id": "cpu", "label": "CPU", "c": c, "level": _level(c, 75, 85)})
        elif name == "nvme":
            temps.append({"id": "nvme", "label": "固态硬盘（系统盘）", "c": c, "level": _level(c, 60, 70)})
        elif name == "drivetemp":
            model = _read(f"{h}/device/model")
            label = f"机械盘 {model}"
            temps.append({"id": "hdd", "label": label, "c": c, "level": _level(c, 45, 55)})
    return temps


MODEMSTAT = "/modemstat/status.txt"
LTE_BW = {"0": 1.4, "1": 3, "2": 5, "3": 10, "4": 15, "5": 20}
LTE_BANDS = {"1": "2100", "3": "1800", "5": "850", "8": "900", "11": "1500", "18": "800", "19": "800", "21": "1500",
             "26": "850", "28": "700", "38": "2600", "39": "1900", "40": "2300", "41": "2500", "42": "3500"}
ENG_STATE = {"NOCONN": "待机", "CONNECT": "通话/连接中", "SEARCH": "搜索网络", "LIMSRV": "受限服务"}


def modem_detail():
    """Parse what asterisk/modemstat.sh last read from the module's spare AT
    port: temperature, serving LTE cell, band, signal quality, VoLTE."""
    try:
        txt = _read(MODEMSTAT)
        age = int(time.time() - os.stat(MODEMSTAT).st_mtime)
    except OSError:
        return None
    d = {"age": age, "stale": age > 120}
    for line in txt.splitlines():
        head, _, rest = line.partition(":")
        f = [x.strip().strip('"') for x in rest.split(",")]
        if head == "+QTEMP" and all(x.lstrip("-").isdigit() for x in f):
            t = [int(x) for x in f]
            d["temp"] = {"c": max(t), "parts": t, "level": _level(max(t), 65, 80)}
        elif head == "+QENG" and len(f) >= 17 and f[0] == "servingcell" and f[2] == "LTE":
            num = lambda x: int(x) if x.lstrip("-").isdigit() else None
            rsrp, rsrq, rssi, sinr = (num(x) for x in f[13:17])
            if sinr is not None and sinr > 40:  # some firmwares report 0..250 in 1/5 dB
                sinr = round(sinr / 5 - 20)
            d["cell"] = {"state": ENG_STATE.get(f[1], f[1]), "duplex": f[3], "plmn": f"{f[4]}-{f[5]}", "id": f[6],
                         "pci": f[7], "earfcn": f[8], "band": f[9], "mhz": LTE_BANDS.get(f[9], ""), "bw": LTE_BW.get(f[11]),
                         "tac": f[12], "rsrp": rsrp, "rsrq": rsrq, "rssi": rssi, "sinr": sinr,
                         "rsrp_level": "ok" if rsrp is None or rsrp >= -105 else "warn" if rsrp >= -115 else "bad",
                         "sinr_level": "ok" if sinr is None or sinr >= 3 else "warn" if sinr >= -3 else "bad"}
        elif head == "+QSPN" and f:
            d["operator"] = f[0]
        elif head == "+QCFG" and len(f) >= 3 and f[0] == "ims":
            d["volte"] = f[2] == "1"
    return d


async def sip_ping(port=5080):
    """Any SIP answer to an OPTIONS on Flexisip's loopback UDP port = alive."""
    loop = asyncio.get_running_loop()
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.setblocking(False)
    try:
        sock.bind(("127.0.0.1", 0))
        lport, tag = sock.getsockname()[1], secrets.token_hex(6)
        msg = (f"OPTIONS sip:127.0.0.1:{port} SIP/2.0\r\n"
               f"Via: SIP/2.0/UDP 127.0.0.1:{lport};branch=z9hG4bK{tag};rport\r\n"
               f"Max-Forwards: 0\r\nFrom: <sip:health@127.0.0.1>;tag={tag}\r\nTo: <sip:127.0.0.1:{port}>\r\n"
               f"Call-ID: health-{tag}\r\nCSeq: 1 OPTIONS\r\nContent-Length: 0\r\n\r\n")
        await loop.sock_sendto(sock, msg.encode(), ("127.0.0.1", port))
        data = await asyncio.wait_for(loop.sock_recv(sock, 4096), 1.5)
        return data.startswith(b"SIP/2.0")
    except (OSError, asyncio.TimeoutError):
        return False
    finally:
        sock.close()


async def bark_ping():
    try:
        async with httpx.AsyncClient(timeout=2) as client:
            return (await client.get(f"{BARK_LOCAL}/ping")).status_code == 200
    except httpx.HTTPError:
        return False


def signal_info(rssi):
    """chan_quectel RSSI is 'CSQ, -dBm' (CSQ 0-31, 99 = unknown)."""
    m = re.match(r"\s*(\d+)(?:,\s*(-?\d+)\s*dBm)?", rssi or "")
    csq = int(m.group(1)) if m else 99
    if csq == 99:
        return {"bars": 0, "dbm": None, "level": "bad"}
    bars = 4 if csq >= 20 else 3 if csq >= 15 else 2 if csq >= 10 else 1 if csq >= 2 else 0
    dbm = int(m.group(2)) if m.group(2) else -113 + 2 * csq
    return {"bars": bars, "dbm": dbm, "level": "ok" if bars >= 2 else "warn" if bars == 1 else "bad"}


@app.get("/api/health")
async def health(request: Request):
    require_auth(request)
    await ami.refresh_status()
    cpu, flexisip_ok, bark_ok, _ = await asyncio.gather(cpu_percent(), sip_ping(), bark_ping(), asyncio.sleep(0.5))
    m = ami.modem_status
    registered = "Registered" in m.get("GSMRegistrationStatus", "")
    modem_ok = ami.connected and m.get("State", "") not in ("", "Not connected")
    services = [
        {"id": "modem", "label": "4G 模块", "level": "ok" if modem_ok and registered else "bad",
         "value": ("已注册" + ("（漫游）" if "roaming" in m.get("GSMRegistrationStatus", "") else "")) if modem_ok and registered
                  else ("未连接" if not modem_ok else "未注册网络")},
        {"id": "asterisk", "label": "电话服务", "level": "ok" if ami.connected else "bad",
         "value": "正常" if ami.connected else "未连接"},
        {"id": "flexisip", "label": "来电推送", "level": "ok" if flexisip_ok and setting("linphone_api_key") else
                  "warn" if flexisip_ok else "bad",
         "value": "正常" if flexisip_ok and setting("linphone_api_key") else "Linphone 账号未设置" if flexisip_ok else "无响应"},
        {"id": "bark", "label": "Bark 通知", "level": "ok" if bark_ok and bark_devices() else "warn" if bark_ok else "bad",
         "value": f"正常 · {len(bark_devices())} 台设备" if bark_ok and bark_devices() else "未设置设备" if bark_ok else "无响应"},
        backup_service(),
    ]
    load1, load5, load15 = (float(x) for x in _read("/proc/loadavg").split()[:3])
    cores = os.cpu_count() or 1
    mem = {k: int(v.split()[0]) for k, v in (l.split(":", 1) for l in _read("/proc/meminfo").splitlines())}
    mem_pct = round(100 * (1 - mem["MemAvailable"] / mem["MemTotal"]), 1)
    du = shutil.disk_usage("/data")
    disk_pct = round(100 * du.used / du.total, 1)
    since = datetime.fromtimestamp(time.time() - 86400, timezone.utc).isoformat(timespec="seconds")
    detail = modem_detail()
    temps = read_temps()
    if detail and detail.get("temp") and not detail["stale"]:
        temps.append({"id": "modem", "label": "4G 模块", **detail["temp"]})
    count = lambda sql: db(sql, (since,), one=True)["n"]
    return {
        "time": now_iso(),
        "uptime": int(float(_read("/proc/uptime").split()[0])),
        "services": services,
        "modem": {"operator": m.get("ProviderName", ""), "model": m.get("Model", ""), "state": m.get("State", ""),
                  "detail": detail, **signal_info(m.get("RSSI", ""))},
        "cpu": {"pct": cpu, "level": _level(cpu, 70, 90), "cores": cores},
        "load": {"1": load1, "5": load5, "15": load15, "pct": round(100 * load1 / cores, 1),
                 "level": _level(load1 / cores, 0.7, 1.0)},
        "mem": {"pct": mem_pct, "used": mem["MemTotal"] - mem["MemAvailable"], "total": mem["MemTotal"],
                "level": _level(mem_pct, 85, 95)},
        "disk": {"pct": disk_pct, "used": du.used, "total": du.total, "level": _level(disk_pct, 85, 95)},
        "temps": temps,
        "day": {
            "sms": count("SELECT COUNT(*) AS n FROM messages WHERE direction='in' AND spam=0 AND ts >= ?"),
            "sms_blocked": count("SELECT COUNT(*) AS n FROM messages WHERE spam=1 AND ts >= ?"),
            "calls": count("SELECT COUNT(*) AS n FROM calls WHERE direction='in' AND status!='blocked' AND started >= ?"),
            "calls_blocked": count("SELECT COUNT(*) AS n FROM calls WHERE status='blocked' AND started >= ?"),
        },
    }


@app.get("/api/settings")
async def get_settings(request: Request):
    require_auth(request)
    return {"block_rules": setting("block_rules"), "allow_rules": setting("allow_rules"),
            "block_keywords": setting("block_keywords"),
            "call_mode": call_mode(),
            "alerts": setting("alerts", "1") == "1",
            "daily_summary": setting("daily_summary", "1") == "1",
            "online_marks": setting("online_marks", "1") == "1",
            "block_marketing": setting("block_marketing", "1") == "1",
            "block_anonymous": setting("block_anonymous", "0") == "1",
            "linphone_user": setting("linphone_user"),
            "linphone_password_set": bool(setting("linphone_password")),
            "linphone_key_ok": bool(setting("linphone_api_key")),
            "bark_devices": "\n".join(bark_device_line(d) for d in bark_devices()),
            "bark_list": [{"name": d["name"], "local": d["server"] == BARK_LOCAL, "host": urlparse(d["server"]).hostname or "",
                           "key": d["key"][:4] + "…"} for d in bark_devices()],
            "rule_info": {l: rule_info(l) for l in (setting("block_rules") + "\n" + setting("allow_rules")).splitlines() if l.strip()},
            "bark_enc_key": setting("bark_enc_key"),
            "public_base_url": setting("public_base_url"),
            "sip_username": "iphone", "sip_password": SIP_PASSWORD}


@app.post("/api/settings")
async def post_settings(request: Request):
    require_auth(request)
    data = await request.json()
    if "bark_devices" in data:
        devices, seen = [], set()
        for line in (data["bark_devices"] or "").splitlines():
            if line.strip():
                d = parse_bark_device(line.strip())
                if (d["server"], d["key"]) not in seen:
                    seen.add((d["server"], d["key"]))
                    devices.append(d)
        set_setting("bark_devices", json.dumps(devices, ensure_ascii=False))
    elif (data.get("bark_key") or "").strip():
        # A page loaded before multi-device support sends one key: add it.
        devices, d = bark_devices(), parse_bark_device(data["bark_key"].strip())
        if all((x["server"], x["key"]) != (d["server"], d["key"]) for x in devices):
            devices.append(d)
        set_setting("bark_devices", json.dumps(devices, ensure_ascii=False))
    if "bark_enc_key" in data:
        k = (data["bark_enc_key"] or "").strip()
        if k and len(k.encode()) != 32:
            raise HTTPException(400, "加密密钥必须是 32 个字符（AES-256）")
        set_setting("bark_enc_key", k)
    for key in ("block_rules", "allow_rules", "block_keywords"):
        if key in data:
            set_setting(key, (data[key] or "").strip())
    for key in ("block_marketing", "block_anonymous", "block_all_calls", "alerts", "daily_summary",
                "online_marks"):
        if key in data:
            set_setting(key, "1" if data[key] else "0")
    if "call_mode" in data:
        if data["call_mode"] not in CALL_MODES:
            raise HTTPException(400, "拦截方式不对")
        set_setting("call_mode", data["call_mode"])
    if "linphone_user" in data:
        user = (data["linphone_user"] or "").strip().removeprefix("sip:").split("@")[0]
        if user != setting("linphone_user"):
            set_setting("linphone_api_key", "")
        set_setting("linphone_user", user)
    if data.get("linphone_password"):
        set_setting("linphone_password", data["linphone_password"])
        set_setting("linphone_api_key", "")
    if "public_base_url" in data:
        set_setting("public_base_url", (data["public_base_url"] or "").strip())
    if data.get("new_password"):
        require_password(request, data.get("current_password", ""))
        if len(data["new_password"]) < 10:
            raise HTTPException(400, "密码至少 10 位")
        set_setting("password_hash", password_hash(data["new_password"]))
    return Response(status_code=204)


@app.post("/api/test-push")
async def test_push(request: Request):
    require_auth(request)
    try:
        only = (await request.json()).get("index")
    except ValueError:
        only = None
    devices = bark_devices()
    if only is not None:
        if not isinstance(only, int) or not 0 <= only < len(devices):
            raise HTTPException(400, "没有这台设备")
        devices = [devices[only]]
    err = await bark_push("短信网关 · 测试", "如果你看到这条，Bark 推送和解密都正常。验证码 123456", "sms",
                          copy="123456", devices=devices)
    if err:
        raise HTTPException(502, err)
    return Response(status_code=204)


# ------------------------------------------------- Linphone push via FlexiAPI --
# Flexisip ([module::PushNotification] external-push-flexiapi) POSTs every push
# here; we forward it to Linphone's FlexiAPI, which holds the APNs credentials
# of the stock App Store Linphone. Linphone user API keys are bound to the IP
# that generated them and expire when unused, so we mint our own (over IPv4,
# the address Linphone sees from this NAS) and re-mint on 401/403.
def _ipv4_client(**kw):
    return httpx.AsyncClient(transport=httpx.AsyncHTTPTransport(local_address="0.0.0.0"), timeout=15, **kw)


async def linphone_new_key():
    user, pw = setting("linphone_user"), setting("linphone_password")
    if not user or not pw:
        raise HTTPException(503, "linphone.org 账号还没在设置里填")
    headers = {"accept": "application/json", "content-type": "application/json",
               "from": f"sip:{user}@sip.linphone.org"}
    async with _ipv4_client() as client:
        r = await client.get(f"{LINPHONE_API}/accounts/me/api_key", headers=headers, auth=httpx.DigestAuth(user, pw))
    if r.status_code != 200:
        raise HTTPException(502, f"linphone.org 拒绝了账号（HTTP {r.status_code}），检查用户名和密码")
    key = r.cookies.get("x-api-key", "")
    with contextlib.suppress(ValueError):
        body = r.json()
        if isinstance(body, dict):
            key = body.get("api_key") or body.get("key") or key
        elif isinstance(body, str):
            key = body or key
    if not key:
        key = r.text.strip().strip('"')
    if not key:
        raise HTTPException(502, "linphone.org 没有返回 API key")
    set_setting("linphone_api_key", key)
    log.info("minted a new Linphone API key")
    return key


@app.post("/flexiapi-router/api/push_notification")
async def flexiapi_push(request: Request):
    if not FLEXIAPI_ROUTER_KEY or not hmac.compare_digest(request.headers.get("x-api-key", ""), FLEXIAPI_ROUTER_KEY):
        raise HTTPException(401, "unauthorized")
    payload = await request.json()
    if not isinstance(payload, dict) or payload.get("pn_provider") not in ("apns", "apns.dev"):
        raise HTTPException(422, "only apns is relayed")
    for attempt in (1, 2):
        key = setting("linphone_api_key") or await linphone_new_key()
        async with _ipv4_client() as client:
            r = await client.post(f"{LINPHONE_API}/push_notification", json=payload,
                                  headers={"accept": "application/json", "x-api-key": key})
        if r.status_code in (401, 403) and attempt == 1:
            set_setting("linphone_api_key", "")
            continue
        log.info("linphone push type=%s -> %s", payload.get("type"), r.status_code)
        return Response(r.content, status_code=r.status_code, media_type="application/json")


@app.post("/api/test-linphone")
async def test_linphone(request: Request):
    require_auth(request)
    await linphone_new_key()
    return Response(status_code=204)


# ------------------------------------------------------------- screening --
@app.get("/internal/call-check")
async def call_check(request: Request, num: str = "", key: str = ""):
    """Asked by Asterisk ([callcheck] in extensions.conf) before ringing the
    phone. Plain-text answer; Asterisk treats anything but "block" as allow."""
    if via_public_gateway(request) or not INTERNAL_KEY or not hmac.compare_digest(key, INTERNAL_KEY):
        raise HTTPException(403, "forbidden")
    mark = ""
    if call_mode() == "smart":
        info = numinfo.lookup(num or "")
        cached = mark_cached(info["key"]) if markable(info) else None
        mark = cached["tag"] if cached else ""
        if markable(info) and not cached:
            # First call from this number: ask Baidu, but Asterisk gives us 3 s
            # in total, so let it ring if the answer is slow (the lookup keeps
            # going in the background and is cached for next time).
            try:
                found = await asyncio.wait_for(asyncio.shield(asyncio.ensure_future(fetch_mark(num))), 2.2)
                mark = (found or {}).get("tag", "")
            except asyncio.TimeoutError:
                log.info("screening: mark lookup too slow, letting it ring")
    verdict, reason = screen_call(num, mark)
    if verdict == "block":
        log.info("screening: block %s (%s)", num, reason)
    return Response(verdict, media_type="text/plain")


@app.get("/api/spam")
async def spam_list(request: Request, limit: int = 200):
    require_auth(request)
    limit = max(1, min(limit, 500))
    msgs = [dict(r) for r in db("SELECT * FROM messages WHERE spam = 1 ORDER BY id DESC LIMIT ?", (limit,))]
    calls = [dict(r) for r in db("SELECT * FROM calls WHERE status = 'blocked' ORDER BY id DESC LIMIT ?", (limit,))]
    return {"messages": with_info(msgs), "calls": with_info(calls)}


# ------------------------------------------------------------ public marks --
# Crowd tags ("广告营销", "骚扰电话", 用户标记 / 号码认证) from the number card in
# Baidu's mobile search results (template ms_num_mark). Only mainland mobile and
# landline numbers are looked up, one request at a time with a pause, results
# cached for 30 days; a captcha page pauses lookups for a few hours.
MARK_TTL = 30 * 86400
_mark_lock = asyncio.Lock()
_mark_state = {"last": 0.0, "paused_until": 0.0}
_MARK_UA = ("Mozilla/5.0 (iPhone; CPU iPhone OS 18_7 like Mac OS X) AppleWebKit/605.1.15 "
            "(KHTML, like Gecko) Version/18.7 Mobile/15E148 Safari/604.1")


def mark_cached(k):
    r = db("SELECT * FROM number_marks WHERE key = ?", (k,), one=True)
    return dict(r) if r else None


def cached_mark_tag(number):
    info = numinfo.lookup(number or "")
    cached = mark_cached(info["key"]) if markable(info) else None
    return cached["tag"] if cached else ""


def markable(info):
    return info["kind"] in (numinfo.MOBILE, numinfo.LANDLINE) and info["key"].startswith("+86") and not info["name"]


def _parse_baidu_card(page):
    i = page.find('class="mark-tag') if "ms_num_mark" in page else -1
    if i < 0:
        return {"tag": "", "source": "", "note": ""}
    seg = page[i:i + 4000]
    text = lambda pat: (lambda m: re.sub(r"<!--.*?-->|<[^>]+>", "", m.group(1)).strip() if m else "")(re.search(pat, seg, re.S))
    return {"tag": text(r'class="mark-tag[^"]*">(.*?)</div>'),
            "source": text(r'class="tag-wrap[^"]*">(.*?)</div>'),
            "note": text(r'class="tip-text[^"]*">(.*?)</div>')}


async def fetch_mark(number, force=False):
    """Cached Baidu mark for a number, looking it up when missing/stale."""
    info = numinfo.lookup(number or "")
    k = info["key"]
    if not markable(info):
        return None
    cached = mark_cached(k)
    fresh = cached and time.time() - datetime.fromisoformat(cached["fetched"]).timestamp() < MARK_TTL
    if fresh and not force:
        return cached
    if setting("online_marks", "1") != "1":
        return cached  # the user turned off sending numbers to Baidu
    async with _mark_lock:
        if time.time() < _mark_state["paused_until"]:
            return cached
        wait = _mark_state["last"] + 3 - time.time()
        if wait > 0:
            await asyncio.sleep(wait)
        _mark_state["last"] = time.time()
        q = "0" + k[3:] if info["kind"] == numinfo.LANDLINE else k[3:]
        try:
            async with httpx.AsyncClient(timeout=8, follow_redirects=True, headers={"User-Agent": _MARK_UA}) as client:
                r = await client.get("https://www.baidu.com/s", params={"wd": q})
            page = r.text
        except httpx.HTTPError as e:
            log.warning("mark lookup failed: %s", e.__class__.__name__)
            return cached
        if r.status_code != 200 or "wappass.baidu.com" in str(r.url) or ("安全验证" in page and "ms_num_mark" not in page):
            _mark_state["paused_until"] = time.time() + 6 * 3600
            log.warning("mark lookup: Baidu asked for verification, pausing 6h")
            return cached
        m = _parse_baidu_card(page)
        db("INSERT INTO number_marks(key, tag, source, note, fetched) VALUES(?, ?, ?, ?, ?) "
           "ON CONFLICT(key) DO UPDATE SET tag = excluded.tag, source = excluded.source, note = excluded.note, fetched = excluded.fetched",
           (k, m["tag"], m["source"], m["note"], now_iso()))
        if m["tag"]:
            log.info("mark: %s -> %s (%s)", info["display"], m["tag"], m["source"])
        return {"key": k, **m}


def mark_soon(number):
    """Look a new caller/sender up in the background."""
    asyncio.ensure_future(fetch_mark(number))


async def backfill_marks():
    """Slowly fill marks for numbers already in the history."""
    await asyncio.sleep(30)
    nums = [r["number"] for r in db("SELECT number FROM calls UNION SELECT number FROM messages WHERE direction = 'in'")]
    for n in nums:
        info = numinfo.lookup(n or "")
        if markable(info) and not mark_cached(info["key"]):
            await fetch_mark(n)
            await asyncio.sleep(5)


# ------------------------------------------------------------ number info --
_tags = None


def number_tags():
    global _tags
    if _tags is None:
        _tags = {r["key"]: r["tag"] for r in db("SELECT key, tag FROM number_tags")}
    return _tags


def number_brief(number):
    """Location/category for lists, plus the user's own tag and list status."""
    i = numinfo.lookup(number or "")
    b_nums, b_pre, _ = parse_rules(setting("block_rules"))
    a_nums, a_pre, _ = parse_rules(setting("allow_rules"))
    m = mark_cached(i["key"]) if i["key"] else None
    return {**i, "tag": number_tags().get(i["key"], ""),
            "mark": m["tag"] if m else "", "mark_source": m["source"] if m else "", "mark_note": m["note"] if m else "",
            "blocked": bool(number) and number_matches(number, b_nums, b_pre),
            "allowed": bool(number) and number_matches(number, a_nums, a_pre)}


def with_info(rows):
    cache = {}
    for r in rows:
        n = r.get("number", "")
        r["info"] = cache[n] if n in cache else cache.setdefault(n, number_brief(n))
    return rows


@app.get("/api/number")
async def number_detail(request: Request, n: str = "", refresh: bool = False):
    """Everything about one number: what it is, calls with it, its SMS thread."""
    require_auth(request)
    await fetch_mark(n, force=refresh)
    info = number_brief(n)
    k = info["key"]
    same = lambda x: numinfo.key(x) == k
    calls = [dict(r) for r in db("SELECT * FROM calls ORDER BY id DESC LIMIT 1000") if same(r["number"])][:200]
    sms_numbers = [r["number"] for r in db("SELECT number, MAX(id) AS last FROM messages GROUP BY number ORDER BY last DESC") if same(r["number"])]
    sms = db(f"SELECT COUNT(*) AS n, MAX(ts) AS ts FROM messages WHERE number IN ({','.join('?' * len(sms_numbers))})", sms_numbers, one=True) \
        if sms_numbers else {"n": 0, "ts": ""}
    return {"info": info, "calls": calls, "sms_number": sms_numbers[0] if sms_numbers else (n or k),
            "sms_count": sms["n"], "sms_last": sms["ts"] or "",
            "dial": re.sub(r"[^0-9+]", "", n or "") or k}


# ------------------------------------------------------------- web phone --
@app.get("/internal/ws-auth")
async def ws_auth(request: Request):
    """Caddy forward_auth for wss://<host>/ws: only a logged-in browser on our
    own page may open the SIP WebSocket to Asterisk."""
    origin = request.headers.get("origin", "")
    own = urlparse(setting("public_base_url") or PUBLIC_URL)
    if origin != f"{own.scheme}://{own.netloc}" or not session_valid(request.cookies.get("smsgw", "")):
        raise HTTPException(401, "unauthorized")
    return Response(status_code=200)


@app.get("/api/webphone")
async def webphone(request: Request):
    require_auth(request)
    if not WEB_SIP_PASSWORD:
        raise HTTPException(503, "网页电话没有配置")
    host = urlparse(setting("public_base_url") or PUBLIC_URL).netloc
    return {"ws": f"wss://{host}/ws", "uri": f"sip:web@{SIP_DOMAIN}", "user": "web", "password": WEB_SIP_PASSWORD,
            "domain": SIP_DOMAIN, "ice": [{"urls": "stun:stun.l.google.com:19302"}]}


@app.post("/api/number-tag")
async def number_tag(request: Request):
    global _tags
    require_auth(request)
    data = await request.json()
    k = numinfo.key(data.get("number", ""))
    tag = (data.get("tag") or "").strip()[:20]
    if not k:
        raise HTTPException(400, "号码不对")
    if tag:
        db("INSERT INTO number_tags(key, tag, updated) VALUES(?, ?, ?) ON CONFLICT(key) DO UPDATE SET tag = excluded.tag, updated = excluded.updated",
           (k, tag, now_iso()))
    else:
        db("DELETE FROM number_tags WHERE key = ?", (k,))
    _tags = None
    return Response(status_code=204)


@app.post("/api/unblock-number")
async def unblock_number(request: Request):
    require_auth(request)
    number = ((await request.json()).get("number") or "").strip()
    c = canon(number)
    keep = [l for l in setting("block_rules").splitlines()
            if l.split("#", 1)[0].strip() not in (number, c) and canon(l.split("#", 1)[0].strip()) != c]
    set_setting("block_rules", "\n".join(keep))
    db("UPDATE messages SET spam = 0, spam_reason = '' WHERE number = ? AND spam_reason = '黑名单号码'", (number,))
    return Response(status_code=204)


@app.post("/api/not-spam")
async def not_spam(request: Request):
    """Restore a message (and every other spam from the same sender) and
    allowlist the sender so it is never filtered again."""
    require_auth(request)
    mid = int((await request.json()).get("id", 0))
    row = db("SELECT number FROM messages WHERE id = ?", (mid,), one=True)
    if not row:
        raise HTTPException(404, "没有这条短信")
    db("UPDATE messages SET spam = 0, spam_reason = '' WHERE number = ? AND spam = 1", (row["number"],))
    add_rule("allow_rules", canon(row["number"]) or row["number"])
    return Response(status_code=204)


@app.post("/api/block-number")
async def block_number(request: Request):
    require_auth(request)
    number = ((await request.json()).get("number") or "").strip()
    if not re.search(r"\d{3,}", number):
        raise HTTPException(400, "号码不对")
    add_rule("block_rules", canon(number) or number)
    db("UPDATE messages SET spam = 1, spam_reason = '黑名单号码' WHERE number = ? AND direction = 'in'", (number,))
    return Response(status_code=204)


# ---------------------------------------------------------------- delete --
# Deletion is permanent (the web page confirms first).
@app.post("/api/delete-messages")
async def delete_messages(request: Request):
    require_auth(request)
    ids = [int(i) for i in (await request.json()).get("ids", []) if str(i).isdigit()][:500]
    if not ids:
        raise HTTPException(400, "没有选中短信")
    db(f"DELETE FROM messages WHERE id IN ({','.join('?' * len(ids))})", ids)
    log.info("deleted %d message(s)", len(ids))
    return Response(status_code=204)


@app.post("/api/delete-thread")
async def delete_thread(request: Request):
    require_auth(request)
    number = ((await request.json()).get("number") or "").strip()
    if not number:
        raise HTTPException(400, "没有号码")
    n = db("SELECT COUNT(*) AS n FROM messages WHERE number = ?", (number,), one=True)["n"]
    db("DELETE FROM messages WHERE number = ?", (number,))
    log.info("deleted thread (%d message(s))", n)
    return {"deleted": n}


@app.post("/api/clear-spam")
async def clear_spam(request: Request):
    require_auth(request)
    n = db("SELECT COUNT(*) AS n FROM messages WHERE spam = 1", one=True)["n"]
    db("DELETE FROM messages WHERE spam = 1")
    log.info("cleared %d spam message(s)", n)
    return {"deleted": n}


# --------------------------------------------------------------- watchdog --
# Runs inside the app, so it can report everything except the app itself
# being down. Alerts are timeSensitive (they break through Focus); the daily
# summary is a normal notification so silence means "nothing happened", not
# "the gateway is broken".
BACKUP_DIR = "/backup"            # BACKUP_DIR from .env, ideally on another disk
BACKUP_MARK = ".sms-gateway-backup"  # only exists on the real disk, see backup_db()
BACKUP_KEEP = 30
_alerts = {}                      # name -> {"since", "sent", "text"}
_backup_retry = 0.0


def gateway_problems():
    """name -> (what is wrong, seconds it must last before we alert)."""
    found = {}
    m = ami.modem_status
    if not ami.connected:
        found["asterisk"] = ("电话服务（Asterisk）连不上了，收不到短信和电话", 180)
    elif m.get("State", "") in ("", "Not connected"):
        found["modem"] = ("4G 模块失联了，收不到短信和电话", 180)
    elif "Registered" not in m.get("GSMRegistrationStatus", ""):
        found["network"] = ("4G 模块掉网了，收不到短信和电话", 300)
    d = modem_detail()
    if d is None or d["age"] > 600:
        found["modemstat"] = ("读不到 4G 模块的详细状态（超过 10 分钟）", 0)
    else:
        if d.get("volte") is False:
            found["volte"] = ("VoLTE 不可用，打不了也接不了电话（短信不受影响）", 300)
        if d.get("temp") and d["temp"]["c"] >= 75:
            found["temp"] = (f"4G 模块温度 {d['temp']['c']}°C，偏高", 120)
    return found


def backup_db():
    """Online SQLite backup to the healthy HDD; keeps the last BACKUP_KEEP days.
    The marker file guards against the backup disk not being mounted, in which
    case Docker would have created an empty directory on the system disk."""
    if not os.path.exists(os.path.join(BACKUP_DIR, BACKUP_MARK)):
        raise RuntimeError("备份目录里没有 .sms-gateway-backup，备份盘可能没挂载")
    dst = os.path.join(BACKUP_DIR, f"sms-{datetime.now():%Y%m%d}.db")
    src, out = sqlite3.connect(DB_PATH), sqlite3.connect(dst + ".tmp")
    try:
        src.backup(out)
    finally:
        out.close()
        src.close()
    os.replace(dst + ".tmp", dst)
    for old in sorted(glob.glob(os.path.join(BACKUP_DIR, "sms-*.db")))[:-BACKUP_KEEP]:
        os.remove(old)
    return os.path.getsize(dst)


def backup_service():
    last = setting("last_backup")
    if not last:
        return {"id": "backup", "label": "每日备份", "level": "warn", "value": "还没备份过"}
    days = (datetime.now().date() - datetime.strptime(last, "%Y-%m-%d").date()).days
    return {"id": "backup", "label": "每日备份", "level": "ok" if days <= 1 else "warn" if days <= 3 else "bad",
            "value": "今天已备份" if days == 0 else "昨天已备份" if days == 1 else f"{days} 天没备份了"}


def daily_summary_text():
    since = datetime.fromtimestamp(time.time() - 86400, timezone.utc).isoformat(timespec="seconds")
    count = lambda sql: db(sql, (since,), one=True)["n"]
    sms = count("SELECT COUNT(*) AS n FROM messages WHERE direction='in' AND spam=0 AND ts >= ?")
    sms_b = count("SELECT COUNT(*) AS n FROM messages WHERE spam=1 AND ts >= ?")
    calls = count("SELECT COUNT(*) AS n FROM calls WHERE direction='in' AND status!='blocked' AND started >= ?")
    calls_b = count("SELECT COUNT(*) AS n FROM calls WHERE status='blocked' AND started >= ?")
    lines = [f"短信 {sms} 条（另拦截 {sms_b} 条）· 来电 {calls} 通（另拦截 {calls_b} 通）"]
    problems = gateway_problems()
    if problems:
        lines.append("需要注意：" + "；".join(t for t, _ in problems.values()))
    else:
        d = modem_detail() or {}
        cell, temp = d.get("cell") or {}, d.get("temp") or {}
        parts = ["网关正常"]
        if cell.get("rsrp") is not None:
            parts.append(f"信号 {cell['rsrp']} dBm")
        if d.get("volte"):
            parts.append("VoLTE 可用")
        if temp:
            parts.append(f"模块 {temp['c']}°C")
        lines.append("，".join(parts))
    b = backup_service()
    lines.append(b["value"] if b["level"] == "ok" else f"备份：{b['value']}")
    return "\n".join(lines)


async def watch_once():
    global _backup_retry
    await ami.refresh_status()
    now, base = time.time(), setting("public_base_url").rstrip("/")
    found = gateway_problems()
    for name, (text, grace) in found.items():
        a = _alerts.setdefault(name, {"since": now, "sent": False})
        a["text"] = text
        if not a["sent"] and now - a["since"] >= grace:
            a["sent"] = True
            log.warning("alert: %s", text)
            if setting("alerts", "1") == "1":
                await bark_push("短信网关出问题了", text, "alert", level="timeSensitive", url=base or "")
    for name in [n for n in _alerts if n not in found]:
        a = _alerts.pop(name)
        if a["sent"]:
            log.info("recovered: %s", a["text"])
            if setting("alerts", "1") == "1":
                await bark_push("短信网关已恢复", f"已恢复：{a['text']}", "alert", url=base or "")

    today, hour = datetime.now().strftime("%Y-%m-%d"), datetime.now().hour
    if hour >= 4 and setting("last_backup") != today and now >= _backup_retry:
        try:
            size = await asyncio.to_thread(backup_db)
            set_setting("last_backup", today)
            log.info("backup ok (%d bytes)", size)
        except Exception as e:
            _backup_retry = now + 3600
            log.warning("backup failed: %s", e)
            if setting("alerts", "1") == "1" and setting("last_backup_alert") != today:
                set_setting("last_backup_alert", today)
                await bark_push("短信网关备份失败", f"{e}，一小时后重试", "alert", level="timeSensitive")
    if 9 <= hour < 12 and setting("last_summary") != today:
        set_setting("last_summary", today)
        if setting("daily_summary", "1") == "1":
            await bark_push("短信网关 · 每日小结", daily_summary_text(), "summary", url=base or "")


async def watchdog():
    await asyncio.sleep(90)  # let AMI connect and the module poller write first
    while True:
        try:
            await watch_once()
        except Exception:
            log.exception("watchdog")
        await asyncio.sleep(60)
