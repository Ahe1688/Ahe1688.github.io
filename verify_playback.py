#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""播放级验证：模拟播放器行为（拉播放列表 → 下载视频切片测速），并清理/排序。

在 filter_sources.py 之后运行。与 HTTP 存活探测不同，本脚本会真的下载
第一个视频切片，从而识别「列表活着但拉不到画面」和「能播但很卡」的源。

处理策略（保守，避免 GitHub 机房视角误删）：
  - trusted 频道（央视/卫视）整条跳过，不删不动；
  - 播放列表 403/401/429/超时  → 保留（可能仅 GitHub 机房受限）；
  - 播放列表正常但拿不到切片（切片 403/404/5xx/DNS/空数据/非 m3u8 内容）
      → 整条剔除（no_stream：任何网络的播放器都拉不到画面）；
  - 切片下载成功 → 记录速度(KB/s)，并按速度把该频道各 URL 从快到慢重排；
  - 全部频道按「trusted 优先 → 速度从快到慢」重排（id 不变，只调顺序）。

用法:
    python3 verify_playback.py <输入.json> <输出.json> [--workers 80]
                               [--limit N] [--csv 报告.csv]
"""
import concurrent.futures as cf
import csv
import json
import re
import sys
import threading
import time
import urllib.error
import urllib.request

UA = "Mozilla/5.0 (Linux; Android 11) exe"   # 与直播 APP 播放器 UA 相近
HOST_LOCKS = {}
_lock = threading.Lock()
HOST_CONCURRENCY = 6      # 同一主机最大并发（1 会串行拖慢，6 兼顾限流风险）
SEG_MAX_BYTES = 512 * 1024
SEG_RE = re.compile(r"^([^#\n][^\n]*\.(?:ts|m4s|mp4|aac))\s*$", re.M)
NESTED_RE = re.compile(r"^([^#\n][^\n]*\.m3u8[^\n]*)\s*$", re.M)


def host_of(u):
    m = re.match(r"https?://([^/:]+)", u)
    return m.group(1) if m else u


def _lock_for(host):
    with _lock:
        if host not in HOST_LOCKS:
            HOST_LOCKS[host] = threading.BoundedSemaphore(HOST_CONCURRENCY)
        return HOST_LOCKS[host]


def fetch(url, timeout, max_bytes=0):
    """返回 (status, body_bytes, speed_kbps)。status: 200/'blocked'/'timeout'/其他错误串"""
    sem = _lock_for(host_of(url))
    if not sem.acquire(timeout=timeout + 4):    # 排队也限时，防止慢主机拖垮全局
        return "timeout", b"", 0.0
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    t0 = time.time()
    try:
        try:
            resp = urllib.request.urlopen(req, timeout=timeout)
        except urllib.error.HTTPError as e:
            return ("blocked" if e.code in (401, 403, 429) else "http%d" % e.code,
                    b"", 0.0)
        except Exception as e:
            r = str(getattr(e, "reason", e))
            if "timed out" in r or "timeout" in r.lower():
                return "timeout", b"", 0.0
            if "getaddrinfo" in r.lower() or "Name or service" in r:
                return "dns", b"", 0.0
            if "refused" in r.lower():
                return "refused", b"", 0.0
            if "reset" in r.lower():
                return "reset", b"", 0.0
            if "CERTIFICATE" in r.upper() or "SSL" in r.upper():
                return "ssl", b"", 0.0
            return "oserr", b"", 0.0

        data, deadline = b"", time.time() + (timeout - 2 if timeout > 4 else 4)
        try:
            while (not max_bytes or len(data) < max_bytes) and time.time() < deadline:
                chunk = resp.read(65536)
                if not chunk:
                    break
                data += chunk
        except Exception:
            dt = time.time() - t0
            sp = (len(data) / 1024.0 / dt) if dt > 0 and data else 0.0
            try:
                resp.close()
            except Exception:
                pass
            return "reset", data, sp
        try:
            resp.close()
        except Exception:
            pass
        dt = time.time() - t0
        sp = (len(data) / 1024.0 / dt) if dt > 0 and data else 0.0
        return resp.status, data, sp
    finally:
        sem.release()


def resolve(url, base):
    if url.startswith("http"):
        return url
    if url.startswith("/"):
        m = re.match(r"(https?://[^/]+)", base)
        return m.group(1) + url if m else url
    return base.rsplit("/", 1)[0] + "/" + url


def test_urls(urls):
    """对一组 URL 做播放级测试，返回 [(url, speed_kbps)]（可播的）。
    首个 URL 成功即停（备线保持原样跟随其后），大幅减少请求量。"""
    first = urls[0]
    st, body, sp = fetch(first, timeout=6)
    if st in ("blocked", "timeout"):    # 列表层受限：无法判断，保守不剔除
        return [(first, 0.0)], True     # (url, 未知速度) +受限标记
    if st == 200 and (b"#EXTM3U" in body[:512] or b"#EXTINF" in body[:512]):
        r = _probe_stream(first, body)
        if r:
            return [r], False
    # 首 URL 不行 → 试第 2 条备线
    for u in urls[1:2]:
        st, body, sp = fetch(u, timeout=6)
        if st in ("blocked", "timeout"):
            return [(u, 0.0)], True
        if st == 200 and (b"#EXTM3U" in body[:512] or b"#EXTINF" in body[:512]):
            r = _probe_stream(u, body)
            if r:
                return [r], False
    return [], False                    # 所有尝试都拿不到画面 → 剔除


def _probe_stream(url, body):
    """从播放列表内容跟进子列表并下载切片，成功返回 (url, speed)。"""
    text = body.decode("utf-8", "replace")
    m = NESTED_RE.search(text)
    if m:                               # 主列表 → 跟进子列表
        sub = resolve(m.group(1).strip(), url)
        st2, body2, sp2 = fetch(sub, timeout=6)
        if st2 != 200 or b"#EXTINF" not in body2[:512]:
            return None
        text, url = body2.decode("utf-8", "replace"), sub
    m = SEG_RE.search(text)
    if not m:                           # 列表里没有任何视频切片
        return None
    st3, sdata, sp3 = fetch(resolve(m.group(1).strip(), url),
                            timeout=7, max_bytes=SEG_MAX_BYTES)
    if st3 == 200 and sdata:
        return (url, round(sp3, 1))
    if st3 == "timeout":
        return (url, 1.0)               # 切片超时：极慢但可能能播，垫底保留
    return None                         # 切片 403/404/5xx/DNS → 不可播


def main():
    a = sys.argv[1:]
    in_path = a[0] if a and not a[0].startswith("--") else "channels.json"
    out_path = a[1] if len(a) > 1 and not a[1].startswith("--") else in_path
    workers = int(a[a.index("--workers") + 1]) if "--workers" in a else 80
    limit = int(a[a.index("--limit") + 1]) if "--limit" in a else 0
    csv_out = a[a.index("--csv") + 1] if "--csv" in a else "playback_report.csv"

    with open(in_path, encoding="utf-8") as f:
        data = json.load(f)
    channels = data.get("channels") or []
    todo = [c for c in channels if not c.get("trusted") and c.get("urls")]
    if limit:
        todo = todo[:limit]
    print(f"[播放级验证] 待测 {len(todo)} 个非 trusted 频道（trusted 跳过），{workers} 并发")

    results = {}
    t0 = time.time()
    with cf.ThreadPoolExecutor(max_workers=workers) as ex:
        futs = {ex.submit(test_urls, c["urls"]): c["id"] for c in todo}
        for i, fu in enumerate(cf.as_completed(futs), 1):
            results[futs[fu]] = fu.result()
            if i % 500 == 0:
                print(f"  进度 {i}/{len(todo)}  用时 {time.time()-t0:.0f}s")

    kept, removed = [], []
    for c in channels:
        if c.get("trusted") or c["id"] not in results:
            kept.append(c)
            continue
        playable, restricted = results[c["id"]]
        if restricted:
            kept.append(c)              # 403/超时：无法判断，原样保留
            continue
        if not playable:
            removed.append(c)
            continue
        url, speed = playable[0]
        rest = [u for u in c["urls"] if u != url]
        c["urls"] = [url] + rest        # 实测最快(可用)的排最前，备线跟随
        c["speed"] = speed
        kept.append(c)

    # 全局排序：trusted 优先，其余按速度从快到慢（id 不变）
    kept.sort(key=lambda c: (0 if c.get("trusted") else 1,
                             -c.get("speed", 0), str(c.get("name", ""))))
    data["channels"] = kept
    data["playback_verified_at"] = int(time.time())

    with open(out_path, "w", encoding="utf-8", newline="\n") as f:
        json.dump(data, f, ensure_ascii=False, indent=1)

    with open(csv_out, "w", newline="", encoding="utf-8-sig") as f:
        w = csv.writer(f)
        w.writerow(["name", "trusted", "speed_kbps", "url"])
        for c in kept:
            if not c.get("trusted") and c.get("speed"):
                w.writerow([c["name"], 0, c["speed"], c["urls"][0]])

    from collections import Counter
    speeds = [c["speed"] for c in kept if not c.get("trusted") and c.get("speed")]
    buckets = Counter("<30 很卡" if s < 30 else "30-150 卡" if s < 150
                      else ">=150 流畅" for s in speeds)
    print(f"\n[结果] 保留 {len(kept)} | 剔除不可播 {len(removed)} | 测速成功 {len(speeds)}")
    print(f"[速度分布] {dict(buckets)}")
    print(f"[写出] {out_path} / {csv_out}")


if __name__ == "__main__":
    main()
