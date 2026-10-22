#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
从 channels.json 剔除明显失效的直播源，输出干净的 channels.json。

探测逻辑复用 check_sources.py 的判定 + 「按主机限流」策略（避免同域名
并发被打爆导致误判），但采用【保守剔除】原则：

【只剔除在任何网络位置都必定失效的源】（位置无关，安全）：
    http404 / http410 / http5xx / DNS失败 / 连接被拒 / 连接重置 /
    SSL错误 / 返回 HTML 错误页
【保留】（这些是否真能播取决于播放器 Header 与你的网络位置）：
    ok（可直接播）
    blocked  (http401/403/407/429 —— 防盗链，真实播放器可能带对 Header 而成功)
    suspect  (200 但内容无法识别，可能是不支持 Range 的服务器)
    timeout  (超时，可能是服务器慢，不一定是死)
    skip     (rtmp 等非 HTTP 协议，本脚本不测)
    unknown  (无法识别的 200 响应)

加 --strict 可把 blocked / timeout / unknown 也一并剔除，只保留验证可播的。

用法：
  # 方式 A：现场探测后剔除（适合 GitHub Action 每日自动跑）
  python3 filter_sources.py channels.json [输出路径] [--workers 100] [--timeout 7]

  # 方式 B：用已有的探测结果 CSV 剔除（适合本地快速过滤，免去重新探测 17 分钟）
  python3 filter_sources.py channels.json [输出路径] --csv source_check_result.csv

  输入/输出为同一路径时原地覆盖（先写临时文件，成功后才替换，原子安全）。
"""
import argparse
import csv
import json
import os
import socket
import ssl
import sys
import time
import urllib.error
import urllib.request
from collections import Counter
from concurrent.futures import ThreadPoolExecutor, as_completed
from urllib.parse import urlparse

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36")
_CTX = ssl.create_default_context()
_CTX.check_hostname = False
_CTX.verify_mode = ssl.CERT_NONE

# 必定失效的 reason（与 check_sources.py 的 reason 命名一致）
REMOVE_REASONS = {
    "http404", "http410",
    "dns", "refused", "reset", "ssl", "html_page",
}
REMOVE_PREFIXES = ("http5", "oserr")  # http500~599、oserr0~...
STRICT_EXTRA = {"timeout", "blocked", "unknown"}


def classify(code, ctype, data):
    if code in (200, 206):
        if b"#EXTM3U" in data:
            return "ok", "hls"
        if data[:1] == b"\x47":
            return "ok", "ts"
        if b"<MPD" in data or b"<mpd" in data:
            return "ok", "mpd"
        if any(k in ctype for k in ("mpegurl", "mp2t", "video/", "octet-stream")):
            return "ok", "ctype"
        low = data[:300].lower()
        if "text/html" in ctype or low.startswith(b"<!doctype") or b"<html" in low:
            return "dead", "html_page"
        if not data:
            return "suspect", "empty"
        return "suspect", "unknown"
    if code in (401, 403, 407):
        return "blocked", "http%d" % code
    if code == 429:
        return "blocked", "http429"
    if code in (404, 410):
        return "dead", "http%d" % code
    if 500 <= code < 600:
        return "dead", "http%d" % code
    return "dead", "http%d" % code


def _fetch(url, timeout, use_range, referer=""):
    headers = {"User-Agent": UA, "Accept": "*/*", "Connection": "close"}
    if use_range:
        headers["Range"] = "bytes=0-4095"
    if referer:
        headers["Referer"] = referer
    req = urllib.request.Request(url, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=timeout, context=_CTX) as r:
            code = r.getcode()
            ctype = (r.headers.get("Content-Type") or "").lower()
            try:
                data = r.read(4096)
            except Exception:
                data = b""
        return classify(code, ctype, data), code
    except urllib.error.HTTPError as e:
        ctype = (e.headers.get("Content-Type") or "").lower() if e.headers else ""
        try:
            data = e.read(1024)
        except Exception:
            data = b""
        return classify(e.code, ctype, data), e.code
    except urllib.error.URLError as e:
        r = getattr(e, "reason", None)
        if isinstance(r, socket.timeout):
            return ("dead", "timeout"), None
        if isinstance(r, ConnectionRefusedError):
            return ("dead", "refused"), None
        if isinstance(r, ConnectionResetError):
            return ("dead", "reset"), None
        if isinstance(r, socket.gaierror):
            return ("dead", "dns"), None
        if isinstance(r, ssl.SSLError):
            return ("dead", "ssl"), None
        if isinstance(r, OSError):
            return ("dead", "oserr%d" % (r.errno or 0)), None
        return ("dead", "urlerr_%s" % type(r).__name__), None
    except Exception as e:
        name = type(e).__name__
        low = str(e).lower()
        if "timed out" in low or "timeout" in name.lower():
            return ("dead", "timeout"), None
        if "name or service" in low or "getaddrinfo" in low or "nodename" in low:
            return ("dead", "dns"), None
        if "refused" in low:
            return ("dead", "refused"), None
        if "reset" in low:
            return ("dead", "reset"), None
        if "certificate" in low or "ssl" in low:
            return ("dead", "ssl"), None
        return ("dead", name), None


def probe(url, timeout):
    if not url.lower().startswith(("http://", "https://")):
        return "skip", "non_http"
    res, code = _fetch(url, timeout, True)
    if code == 400:  # 不接受 Range，去掉再试
        res2, _ = _fetch(url, timeout, False)
        if res2[0] in ("ok", "suspect"):
            return res2[0], res2[1] + "+norange"
    if code in (401, 403):  # 防盗链，带同源 Referer 再试
        try:
            referer = url.split("/")[0] + "//" + url.split("/")[2] + "/"
        except Exception:
            referer = ""
        if referer:
            res3, _ = _fetch(url, timeout, True, referer)
            if res3[0] == "ok":
                return "ok", res3[1] + "+referer"
    return res


def should_remove(status, reason, strict):
    if status != "dead":
        return False
    if reason in REMOVE_REASONS:
        return True
    if any(reason.startswith(p) for p in REMOVE_PREFIXES):
        return True
    if strict and reason in STRICT_EXTRA:
        return True
    return False


def main():
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
    ap = argparse.ArgumentParser()
    ap.add_argument("src")
    ap.add_argument("dst", nargs="?", default="")
    ap.add_argument("--csv", default="", help="用已有探测结果 CSV（含 status,reason 列）剔除，跳过现场探测")
    ap.add_argument("--workers", type=int, default=100)
    ap.add_argument("--timeout", type=float, default=7.0)
    ap.add_argument("--strict", action="store_true", help="同时剔除 blocked/timeout/unknown，只保留验证可播")
    args = ap.parse_args()

    dst = args.dst or args.src
    with open(args.src, encoding="utf-8") as f:
        doc = json.load(f)
    channels = doc.get("channels", [])

    # 探测或读 CSV，得到 url -> (status, reason)
    url_status = {}
    if args.csv:
        print("[模式] 使用已有探测结果 %s" % args.csv, flush=True)
        with open(args.csv, encoding="utf-8-sig", newline="") as f:
            for row in csv.DictReader(f):
                url_status[row["url"]] = (row.get("status", ""), row.get("reason", ""))
        # 同时按 id 兜底（防止 url 大小写/空格差异）
        id_status = {}
        with open(args.csv, encoding="utf-8-sig", newline="") as f:
            for row in csv.DictReader(f):
                if row.get("id"):
                    id_status[row["id"]] = (row.get("status", ""), row.get("reason", ""))
    else:
        print("[模式] 现场探测 %d 个频道" % len(channels), flush=True)
        tasks = []
        for c in channels:
            for u in (c.get("urls") or []):
                if u:
                    tasks.append((c, u))
        groups = {}
        for c, u in tasks:
            try:
                host = urlparse(u).netloc.lower()
            except Exception:
                host = "?"
            groups.setdefault(host, []).append((c, u))
        chunks = []
        for host, items in groups.items():
            n = len(items)
            parts = 1 if n <= 15 else min(6, (n + 14) // 15)
            size = (n + parts - 1) // parts
            for i in range(0, n, size):
                chunks.append(items[i:i + size])

        def work_group(chunk):
            out = []
            for c, u in chunk:
                try:
                    st, rs = probe(u, args.timeout)
                except Exception:
                    st, rs = "dead", "exec_error"
                out.append((u, st, rs))
            return out

        done = 0
        t0 = time.time()
        with ThreadPoolExecutor(max_workers=args.workers) as ex:
            futs = [ex.submit(work_group, ch) for ch in chunks]
            for fut in as_completed(futs):
                for u, st, rs in fut.result():
                    url_status[u] = (st, rs)
                    done += 1
                if done % 1000 < len(fut.result()) or done == len(tasks):
                    el = time.time() - t0
                    print("  %d/%d  %.0fs" % (done, len(tasks), el), flush=True)

    # 逐频道判定：只要任意一个 url 不算“应剔除”，就保留该频道
    kept, removed = [], []
    removed_rows = []
    for c in channels:
        urls = [u for u in (c.get("urls") or []) if u]
        if not urls:
            kept.append(c)
            continue
        if c.get("trusted"):   # 可信央视/卫视源：不在 GitHub 机房探测，避免误删
            kept.append(c)
            continue
        # 优先按 url 匹配；CSV 模式下也尝试按 id 匹配
        first = urls[0]
        st, rs = url_status.get(first, ("", ""))
        if not st and args.csv and str(c.get("id")) in id_status:
            st, rs = id_status[str(c.get("id"))]
        # 决定：本频道是否可被剔除（全部 url 都该删才删）
        drop = True
        for u in urls:
            s, r = url_status.get(u, ("", ""))
            if s and not should_remove(s, r, args.strict):
                drop = False
                break
            if not s:  # 没探测到，保守保留
                drop = False
                break
        if drop:
            removed.append(c)
            removed_rows.append({"name": c.get("name", ""), "url": first,
                                 "status": st, "reason": rs})
        else:
            kept.append(c)

    doc["channels"] = kept
    doc["filtered_at"] = int(time.time())
    doc["filter_policy"] = ("strict" if args.strict else "conservative")

    # 原子写入
    tmp = dst + ".tmp"
    with open(tmp, "w", encoding="utf-8", newline="\n") as f:
        json.dump(doc, f, ensure_ascii=False, indent=1)
    os.replace(tmp, dst)

    # 报告
    print("\n==== 剔除报告 ====")
    print("原始频道 %d  →  保留 %d  →  剔除 %d (%.1f%%)"
          % (len(channels), len(kept), len(removed),
             100.0 * len(removed) / max(len(channels), 1)))
    rc = Counter(r["reason"] for r in removed_rows)
    print("剔除原因分布:")
    for k, v in rc.most_common(15):
        print("  %-12s %5d" % (k, v))
    # 分类保留率
    cats = {c.get("id"): c.get("name", "") for c in doc.get("categories", []) if c.get("id")}
    per = {}
    for c in kept:
        bits = c.get("category_bits") or 0
        label = "(未分类)"
        for cid in sorted(cats):
            if bits & (1 << (cid - 1)):
                label = cats[cid]
                break
        per.setdefault(label, [0, 0])
        per[label][0] += 1
    for c in removed:
        bits = c.get("category_bits") or 0
        label = "(未分类)"
        for cid in sorted(cats):
            if bits & (1 << (cid - 1)):
                label = cats[cid]
                break
        per.setdefault(label, [0, 0])
        per[label][1] += 1
    rows = [(k, v[0], v[1]) for k, v in per.items() if (v[0] + v[1]) >= 50]
    rows.sort(key=lambda x: -(x[1] + x[2]))
    if rows:
        print("---- 各分类 保留/剔除(>=50) ----")
        for k, keep_n, rem_n in rows[:30]:
            print("  %-22s 保留 %5d / 剔除 %4d" % (k[:22], keep_n, rem_n))

    # 写剔除明细 CSV（同名放在 dst 同目录）
    rem_csv = os.path.join(os.path.dirname(os.path.abspath(dst)) or ".",
                           "removed_sources.csv")
    with open(rem_csv, "w", newline="", encoding="utf-8-sig") as f:
        w = csv.DictWriter(f, fieldnames=["name", "url", "status", "reason"])
        w.writeheader()
        w.writerows(removed_rows)
    print("\n剔除明细 -> %s" % rem_csv)
    print("写出 -> %s (%.2f MB)" % (dst, os.path.getsize(dst) / 1048576.0))


if __name__ == "__main__":
    main()
