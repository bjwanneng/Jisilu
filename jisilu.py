# -*- coding: utf-8 -*-
"""集思录登录 + ETF 数据抓取

登录流程（逆向自 www.jisilu.cn 登录页 JS）:
  1. GET /account/login/ 取 kbzw__Session cookie
  2. POST /webapi/account/login_process/
     - aes=1
     - user_name / password 均为 jslencode() 加密后的 hex
     - jslencode = AES-128-ECB(key='397151C04723421F', PKCS7) -> hex
成功后会话 cookie 落在 session 里，用它请求 /data/etf/etf_list/ 等接口拿全量数据。
"""
import sys
import json
import time
import requests
from Crypto.Cipher import AES
from Crypto.Util.Padding import pad

BASE = "https://www.jisilu.cn"
KEY = b"397151C04723421F"
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36")

# 与登录账号分离存放，避免误提交/误分享
CRED_FILE = "jisilu_cred.json"


def jslencode(text: str) -> str:
    cipher = AES.new(KEY, AES.MODE_ECB)
    ct = cipher.encrypt(pad(text.encode("utf-8"), AES.block_size))
    return ct.hex()


def load_credentials():
    try:
        with open(CRED_FILE, encoding="utf-8") as f:
            c = json.load(f)
            return c["user"], c["password"]
    except (OSError, KeyError, ValueError):
        return None, None


def login(user: str, password: str) -> requests.Session:
    s = requests.Session()
    s.headers.update({"User-Agent": UA, "Referer": f"{BASE}/account/login/"})
    s.get(f"{BASE}/account/login/", timeout=30)

    data = {
        "aes": 1,
        "user_name": jslencode(user),
        "password": jslencode(password),
        "auto_login": 1,
        "return_url": "/",
    }
    r = s.post(f"{BASE}/webapi/account/login_process/", data=data, timeout=30)
    r.raise_for_status()
    result = r.json()
    if result.get("code") != 200:
        raise RuntimeError(f"登录失败: {result}")
    return s


def get_session() -> requests.Session:
    """带缓存的登录会话：cookie 失效前复用磁盘上的会话。

    会话校验用 etf_list 行数（未登录被截到 20 行），不要用
    /webapi/account/user/ —— 该接口在本站不存在（404），
    曾因误判失效反复重登触发"登录次数过多"限制。
    """
    import os
    cache = "jisilu_session.json"
    s = requests.Session()
    s.headers.update({"User-Agent": UA, "Referer": f"{BASE}/data/etf/"})
    if os.path.exists(cache):
        with open(cache, encoding="utf-8") as f:
            cookies = json.load(f)
        s.cookies.update(cookies)
        try:
            d = fetch(s, "/data/etf/etf_list/")
            if len(d.get("rows", [])) > 20:
                return s
        except requests.RequestException:
            raise  # 网络故障不能当成登录失效，避免反复撞登录限制
    user, password = load_credentials()
    if not user:
        raise RuntimeError(f"请先创建 {CRED_FILE}: {{\"user\": \"手机号\", \"password\": \"密码\"}}")
    s = login(user, password)
    with open(cache, "w", encoding="utf-8") as f:
        json.dump(s.cookies.get_dict(), f)
    return s


def fetch(s: requests.Session, path: str) -> dict:
    r = s.get(f"{BASE}{path}", params={"___jsl": int(time.time() * 1000)}, timeout=30)
    r.raise_for_status()
    return r.json()


if __name__ == "__main__":
    import argparse
    ap = argparse.ArgumentParser(description="集思录数据抓取")
    ap.add_argument("--user")
    ap.add_argument("--password")
    ap.add_argument("--api", default="/data/etf/etf_list/",
                    help="接口路径，如 /data/etf/etf_list/ /data/etf/gold_list/ /data/etf/money_list/")
    ap.add_argument("-o", "--out", default=None, help="结果保存为 JSON 文件")
    args = ap.parse_args()

    if args.user and args.password:
        with open(CRED_FILE, "w", encoding="utf-8") as f:
            json.dump({"user": args.user, "password": args.password}, f)

    s = get_session()
    data = fetch(s, args.api)
    rows = data.get("rows", [])
    print(f"接口 {args.api} -> {len(rows)} 行 (total={data.get('total')})")
    if args.out:
        with open(args.out, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False)
        print(f"已保存到 {args.out}")
    else:
        print(json.dumps(rows[:3], ensure_ascii=False, indent=2))
