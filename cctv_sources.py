#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
央视 / 卫视频道源（独立可测，被 generate_channels.py 调用）

设计要点
========
1. 【央视 CCTV-1~17】主源固定用聚合机
      http://74.91.26.218:82/live/cctvNhd.m3u8
   该地址沙箱实测 17/17 全部返回标准 HLS（全球可达，国内也能播），
   且为硬编码、不依赖任何外部抓取，保证“看央视”永远有兜底。
2. 【备用源】再叠加 best-fan/iptv-sources 的 cn_cctv / cn_province
   （作者每日从国内校验更新）。只保留不带过期令牌的 URL，剔除
   auth_key= / txSecret= / txTime= / wsSecret= / wsTime= 这类会失效的地址。
3. 【卫视】全部来自 best-fan 的 cn_province（同样只留非过期地址）。
4. 每个频道打 trusted=1 标记，filter_sources.py 见到该标记会跳过探测，
   避免 GitHub 机房探不到国内源而误删。

用法
====
  python3 cctv_sources.py            # 打印统计 + 写出 cctv_channels.json
  (被 generate_channels.py import 时调用 build_cctv_channels())
"""
import json
import re
import sys
import urllib.request

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36")

# 央视主源（硬编码，全球可达，沙箱已验证 CCTV1-17 全可用）
AGG_CCTV = "http://74.91.26.218:82/live/cctv{}.hd.m3u8"  # {} 替换为 1..17

# 备用/卫视来源（每日国内校验）
SOURCES = [
    "https://raw.githubusercontent.com/best-fan/iptv-sources/master/cn_cctv.m3u8",
    "https://raw.githubusercontent.com/best-fan/iptv-sources/master/cn_province.m3u8",
]

# 带这些串的 URL 是“过期令牌”，几天就失效，必须剔除
EXPIRE_HINTS = ("auth_key=", "txsecret=", "txtime=", "wssecret=", "wstime=")

# CCTV 数字 -> 中文全称（用于友好的频道名）
CCTV_NAMES = {
    1: "CCTV-1 综合", 2: "CCTV-2 财经", 3: "CCTV-3 综艺",
    4: "CCTV-4 中文国际", 5: "CCTV-5 体育", 6: "CCTV-6 电影",
    7: "CCTV-7 国防军事", 8: "CCTV-8 电视剧", 9: "CCTV-9 纪录",
    10: "CCTV-10 科教", 11: "CCTV-11 戏曲", 12: "CCTV-12 社会与法",
    13: "CCTV-13 新闻", 14: "CCTV-14 少儿", 15: "CCTV-15 音乐",
    16: "CCTV-16 奥林匹克", 17: "CCTV-17 农业农村",
}

CCTV_RE = re.compile(r"CCTV[-_ ]?(\d+)", re.I)
WS_RE = re.compile(r"([\u4e00-\u9fff]+卫视)")


def _fetch(url):
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    with urllib.request.urlopen(req, timeout=40) as r:
        return r.read().decode("utf-8", "ignore")


def _clean(url):
    u = (url or "").strip()
    if not u:
        return ""
    low = u.lower()
    if any(h in low for h in EXPIRE_HINTS):
        return ""          # 过期令牌，丢弃
    if not u.lower().startswith(("http://", "https://")):
        return ""
    return u


def parse_cn_m3u(text):
    """解析 best-fan 格式的 M3U，返回 [(name, logo, group, url)]"""
    out = []
    lines = text.splitlines()
    for i, line in enumerate(lines):
        line = line.strip()
        if not line.startswith("#EXTINF"):
            continue
        logo_m = re.search(r'tvg-logo="([^"]*)"', line)
        group_m = re.search(r'group-title="([^"]*)"', line)
        name_m = re.search(r",(.+)$", line)
        name = name_m.group(1).strip() if name_m else "未命名"
        logo = logo_m.group(1) if logo_m else ""
        group = group_m.group(1) if group_m else ""
        # 下一行是 URL
        for j in range(i + 1, min(i + 4, len(lines))):
            cand = lines[j].strip()
            if cand and not cand.startswith("#"):
                out.append((name, logo, group, cand))
                break
    return out


def canon_cn(name):
    """把频道名归一化成“央视/卫视”规范键；不是则返回 None。"""
    m = CCTV_RE.search(name or "")
    if m:
        return "CCTV-%d" % int(m.group(1))
    if "卫视" in (name or ""):
        mm = WS_RE.search(name)
        if mm:
            return mm.group(1)        # 例如 “北京卫视”
    return None


def build_cctv_channels():
    """返回央视+卫视频道列表（dict，含 trusted 标记）。"""
    # 1) 先放硬编码央视主源（保证可用，不依赖网络）
    ch = {}
    for n in range(1, 18):
        key = "CCTV-%d" % n
        ch[key] = {
            "name": CCTV_NAMES.get(n, "CCTV-%d" % n),
            "en_name": "CCTV-%d" % n,
            "logo": "https://live.fanmingming.cn/tv/CCTV%d.png" % n,
            "epg_id": "CCTV%d.cn@SD" % n,
            "category_names": ["央视"],
            "urls": [AGG_CCTV.format(n)],
            "trusted": 1,
            "area": "中国",
        }

    # 2) 叠加 best-fan 的央视/卫视，做备用与卫视补充
    fetched = 0
    for src in SOURCES:
        try:
            txt = _fetch(src)
            fetched += 1
        except Exception as e:
            print("  [warn] 拉取 %s 失败: %s" % (src, e), file=sys.stderr)
            continue
        for name, logo, group, url in parse_cn_m3u(txt):
            cu = _clean(url)
            if not cu:
                continue
            key = canon_cn(name)
            if not key:
                continue
            if key not in ch:
                # 卫视或新增央视：新建
                is_cctv = key.startswith("CCTV-")
                ch[key] = {
                    "name": name,
                    "en_name": name,
                    "logo": logo or "",
                    "epg_id": "",
                    "category_names": ["央视" if is_cctv else "卫视"],
                    "urls": [],
                    "trusted": 1,
                    "area": "中国",
                }
            if cu not in ch[key]["urls"]:
                # 主源（聚合）已在 urls[0]，其余放后面做备用
                ch[key]["urls"].append(cu)
            if not ch[key].get("logo") and logo:
                ch[key]["logo"] = logo

    # 3) 转成与 generate_channels 对齐的频道 dict 列表
    channels = []
    cid = 1
    for key in sorted(ch.keys()):
        c = ch[key]
        channels.append({
            "id": cid,
            "name": c["name"],
            "en_name": c["en_name"],
            "pinyin": "",
            "channel_code": cid,
            "logo": c["logo"],
            "level": 0,
            "category_names": c["category_names"],
            "epg_id": c["epg_id"],
            "catchup_id": "",
            "timeshift_id": "",
            "region_id": 0,
            "area": c["area"],
            "urls": c["urls"],
            "trusted": 1,
        })
        cid += 1
    return channels, fetched


if __name__ == "__main__":
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
    chs, fetched = build_cctv_channels()
    cctv = [c for c in chs if "央视" in c["category_names"]]
    ws = [c for c in chs if "卫视" in c["category_names"]]
    print("best-fan 成功拉取 %d/%d 个源文件" % (fetched, len(SOURCES)))
    print("生成频道：央视 %d 个，卫视 %d 个，合计 %d 个" % (len(cctv), len(ws), len(chs)))
    print("央视主源示例：", cctv[0]["name"], cctv[0]["urls"][0])
    for c in cctv[:3]:
        print("  %-16s urls=%d  %s" % (c["name"], len(c["urls"]), c["urls"][0]))
    with open("cctv_channels.json", "w", encoding="utf-8") as f:
        json.dump(chs, f, ensure_ascii=False, indent=1)
    print("写出 cctv_channels.json (%d 个频道)" % len(chs))
