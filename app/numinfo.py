"""Offline number lookup: where a number is from and what kind it is.

Data (see numdb/SOURCES.md): mainland mobile ranges (phone.dat), mainland
landline area codes (areacodes.json), plus hand-written tables below for
Japan, country codes and well-known service numbers.
"""
import json
import os
import re
import struct
from functools import lru_cache

_DIR = os.path.join(os.path.dirname(__file__), "numdb")

with open(os.path.join(_DIR, "phone.dat"), "rb") as _fh:
    _DAT = _fh.read()
_IDX = struct.unpack("<I", _DAT[4:8])[0]
_N = (len(_DAT) - _IDX) // 9
with open(os.path.join(_DIR, "areacodes.json"), encoding="utf-8") as _fh:
    _AREA = json.load(_fh)

CARRIERS = {1: "中国移动", 2: "中国联通", 3: "中国电信", 4: "电信虚拟运营商", 5: "联通虚拟运营商",
            6: "移动虚拟运营商", 7: "中国广电", 8: "广电虚拟运营商"}

# Well-known mainland service numbers (only ones that are certain).
SERVICES = {
    "10086": "中国移动", "10010": "中国联通", "10000": "中国电信", "10099": "中国广电",
    "95588": "工商银行", "95599": "农业银行", "95566": "中国银行", "95533": "建设银行",
    "95559": "交通银行", "95555": "招商银行", "95580": "邮储银行", "95568": "民生银行",
    "95558": "中信银行", "95595": "光大银行", "95577": "华夏银行", "95561": "兴业银行",
    "95528": "浦发银行", "95508": "广发银行", "95511": "平安", "95519": "中国人寿",
    "95518": "中国人保", "95500": "太平洋保险", "95188": "支付宝", "95017": "微信支付",
    "95338": "顺丰速运", "95311": "中通快递", "95543": "申通快递", "95546": "韵达快递",
    "95554": "圆通速递", "11183": "中国邮政", "95583": "中国国航", "95539": "南方航空",
    "95530": "东方航空", "12306": "铁路 12306", "12345": "政务服务热线", "12315": "市场监管投诉",
    "12321": "不良信息举报", "12123": "交管 12123", "12333": "人社服务", "12366": "纳税服务",
    "12110": "短信报警", "110": "报警", "119": "火警", "120": "急救", "122": "交通事故报警",
}

COUNTRIES = {"81": "日本", "852": "中国香港", "853": "中国澳门", "886": "中国台湾", "1": "美国/加拿大",
             "44": "英国", "82": "韩国", "65": "新加坡", "61": "澳大利亚", "49": "德国", "33": "法国",
             "7": "俄罗斯", "66": "泰国", "60": "马来西亚", "84": "越南", "63": "菲律宾", "62": "印度尼西亚",
             "91": "印度", "64": "新西兰", "39": "意大利", "34": "西班牙"}

JP_AREAS = {"3": "东京", "6": "大阪", "52": "名古屋", "11": "札幌", "92": "福冈", "45": "横滨",
            "75": "京都", "78": "神户", "22": "仙台", "82": "广岛", "44": "川崎", "43": "千叶",
            "48": "埼玉", "25": "新潟", "54": "静冈", "95": "长崎", "98": "冲绳"}

# User-facing kinds; the page picks an icon/colour per kind.
MOBILE, LANDLINE, SERVICE, BULK_SMS, HOTLINE, EMERGENCY, FOREIGN, HIDDEN = \
    "mobile", "landline", "service", "bulk", "hotline", "emergency", "foreign", "hidden"


def key(num):
    """Canonical E.164-ish key so 10010 / +8610010 and 0631... / +86631...
    compare equal. Matches how [from-iphone] dials."""
    n = re.sub(r"[^0-9+]", "", num or "")
    if not re.search(r"\d", n):
        return ""
    if n.startswith("+"):
        return n
    if n.startswith("00"):
        return "+" + n[2:]
    if n.startswith("86") and len(n) == 13 and n[2] == "1":
        return "+" + n
    if n.startswith("0"):
        return "+86" + n[1:]
    return "+86" + n


def _mobile(r):
    p = int(r[:7])
    lo, hi = 0, _N - 1
    while lo <= hi:
        mid = (lo + hi) // 2
        pre, rec, typ = struct.unpack("<IIB", _DAT[_IDX + mid * 9:_IDX + mid * 9 + 9])
        if pre == p:
            prov, city, _zip, area = _DAT[rec:_DAT.index(b"\0", rec)].decode().split("|")
            return prov, city, CARRIERS.get(typ, "")
        lo, hi = (mid + 1, hi) if pre < p else (lo, mid - 1)
    return None


def _cn(r):
    if r in SERVICES:
        return {"kind": EMERGENCY if len(r) == 3 else SERVICE, "name": SERVICES[r], "loc": "", "category": "紧急电话" if len(r) == 3 else "服务号码"}
    if len(r) == 11 and r[0] == "1":
        m = _mobile(r)
        if m:
            prov, city, carrier = m
            loc = prov if prov == city else f"{prov} {city}"
            return {"kind": MOBILE, "name": "", "loc": loc, "carrier": carrier,
                    "category": "手机" + ("（虚拟运营商）" if "虚拟" in carrier else "")}
        return {"kind": MOBILE, "name": "", "loc": "", "category": "手机"}
    if r.startswith("106"):
        return {"kind": BULK_SMS, "name": "", "loc": "", "category": "企业短信通道"}
    if r[0] == "1":
        return {"kind": SERVICE, "name": "", "loc": "", "category": "公共服务号码" if r.startswith("12") else "运营商服务号码"}
    if len(r) == 10 and r[:3] in ("400", "800"):
        return {"kind": HOTLINE, "name": "", "loc": "", "category": "企业客服热线"}
    if r[:2] in ("95", "96") and len(r) <= 8:
        return {"kind": SERVICE, "name": "", "loc": "", "category": "全国服务热线" if r[:2] == "95" else "地方服务热线"}
    ac = "0" + (r[:2] if r[0] in "12" else r[:3])
    a = _AREA.get(ac)
    if a and len(r) >= 9:
        loc = a["province"] if a["province"] == a["city"] else f"{a['province']} {a['city']}"
        return {"kind": LANDLINE, "name": "", "loc": loc.replace("省", "").replace("市", "").replace("自治区", ""),
                "category": f"固话（区号 {ac}）"}
    return {"kind": SERVICE, "name": "", "loc": "", "category": "其他号码"}


def _jp(r):
    if r[:2] in ("90", "80", "70"):
        return {"kind": FOREIGN, "name": "", "loc": "日本", "category": "日本手机"}
    if r.startswith("50"):
        return {"kind": FOREIGN, "name": "", "loc": "日本", "category": "日本 IP 电话"}
    if r.startswith("120") or r.startswith("800"):
        return {"kind": HOTLINE, "name": "", "loc": "日本", "category": "日本免费电话"}
    for n in (2, 1):
        if r[:n] in JP_AREAS:
            return {"kind": FOREIGN, "name": "", "loc": f"日本 {JP_AREAS[r[:n]]}", "category": "日本固话"}
    return {"kind": FOREIGN, "name": "", "loc": "日本", "category": "日本固话"}


@lru_cache(maxsize=4096)
def lookup(num):
    """{key, display, kind, name, loc, carrier, category}"""
    k = key(num)
    if not k:
        return {"key": "", "display": "隐藏号码", "kind": HIDDEN, "name": "", "loc": "", "carrier": "", "category": "隐藏号码"}
    if k.startswith("+86"):
        info = _cn(k[3:])
    elif k.startswith("+81"):
        info = _jp(k[3:])
    else:
        cc = next((k[1:1 + n] for n in (3, 2, 1) if k[1:1 + n] in COUNTRIES), "")
        info = {"kind": FOREIGN, "name": "", "loc": COUNTRIES.get(cc, ""), "category": "国际号码"}
    info.setdefault("carrier", "")
    return {"key": k, "display": display(k), **info}


def display(k):
    """Readable form: mainland numbers without +86, others grouped."""
    if k.startswith("+86"):
        r = k[3:]
        if len(r) == 11 and r[0] == "1":
            return f"{r[:3]} {r[3:7]} {r[7:]}"
        if r[0] in "23456789" and len(r) >= 9 and not r.startswith(("400", "800", "95", "96")):
            ac = r[:2] if r[0] == "2" else r[:3]
            return f"0{ac}-{r[len(ac):]}"
        return r
    if k.startswith("+81"):
        r = k[3:]
        if r[:2] in ("90", "80", "70", "50"):
            return f"+81 {r[:2]}-{r[2:6]}-{r[6:]}"
        return f"+81 {r}"
    return k
