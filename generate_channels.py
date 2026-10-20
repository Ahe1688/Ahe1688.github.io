#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
超级直播 远端直播源自动生成器
============================
从公开、每日自动维护的 IPTV 源（默认 iptv-org）抓取 M3U，
转换成「超级直播」App 远端 channels.json 格式，写回仓库根目录。

输出格式（与 App 内置 channels_fallback.json 同源，App 直接可读）：
{
  "categories": [ {"id":int,"name":str,"en_name":str,"logo":str,"sort":int} ],
  "channels":   [ {"id":int,"name":str,"en_name":str,"pinyin":str,
                   "channel_code":int,"logo":str,"level":int(0=免费),
                   "category_bits":int(位掩码), "category_ids":[int],
                   "epg_id":str,"catchup_id":str,"timeshift_id":str,
                   "region_id":int,"area":str,"urls":[str]} ],
  "generated_at": <unix秒>
}

用法：
    python3 generate_channels.py [输出路径，默认 ./channels.json]

环境变量 / 可改项：见下方 SOURCES。
"""

import json
import os
import re
import sys
import datetime
import urllib.request

# ---------------------------------------------------------------------------
# 1) 抓取源（均为公开、每日自动维护的 M3U）
#    若某源在 GitHub Actions 环境中不可达，会自动跳过，不影响整体。
#    想增加更多源，往这个列表里加 URL 即可。
# ---------------------------------------------------------------------------
SOURCES = [
    # iptv-org 全球主列表（每日自动验活、清理死链，最稳）
    "https://iptv-org.github.io/iptv/index.m3u",
    # iptv-org 中国专属子集（CCTV / 卫视 / 地方更集中）
    "https://iptv-org.github.io/iptv/countries/cn.m3u",
    # 可选：fanmingming 国内维护源（若你的运行环境能连上就启用）
    # "https://live.fanmingming.cn/tv/m3u/ipv6.m3u",
]

UA = "Mozilla/5.0 (superlive-autogen) IPTV-Collector/1.0"

# ---------------------------------------------------------------------------
# 2) 分类显示名映射（iptv-org 的 group-title 多为英文，这里转成中文展示）
# ---------------------------------------------------------------------------
GROUP_CN = {
    "news": "新闻",
    "sports": "体育",
    "movies": "电影",
    "kids": "少儿",
    "documentary": "纪录片",
    "music": "音乐",
    "general": "综合",
    "entertainment": "娱乐",
    "education": "教育",
    "religious": "宗教",
    "lifestyle": "生活",
    "business": "财经",
    "animation": "动画",
    "culture": "文化",
    "tech": "科技",
    "travel": "旅游",
    "weather": "天气",
    "gaming": "游戏",
    "comedy": "喜剧",
    "cooking": "美食",
    "undefined": "未分类",
    "adult": "成人",
    "shop": "购物",
    "family": "家庭",
    "public": "公共",
    "series": "剧集",
    "classic": "经典",
    "relax": "休闲",
    "outdoor": "户外",
    "auto": "汽车",
    "science": "科学",
    "legislative": "立法",
    "interactive": "互动",
}

# 中文频道按名称归类的规则（命中后追加到对应分类）
CN_RULES = [
    (re.compile(r"CCTV|央视|中国中央电视台", re.I), "央视"),
    (re.compile(r"卫视|Satellite\s*TV", re.I), "卫视"),
    (re.compile(r"纪录|Documentary", re.I), "纪录片"),
    (re.compile(r"体育|Sports", re.I), "体育"),
    (re.compile(r"新闻|News", re.I), "新闻"),
    (re.compile(r"电影|Movies", re.I), "电影"),
    (re.compile(r"少儿|卡通|Kids|Cartoon", re.I), "少儿"),
    (re.compile(r"音乐|Music", re.I), "音乐"),
]


def norm_group(title):
    """把一个 group-title 归一化成一个中文分类名。"""
    t = (title or "").strip()
    if not t:
        return "未分类"
    low = t.lower()
    if low in GROUP_CN:
        return GROUP_CN[low]
    # 常见变体
    for k, v in GROUP_CN.items():
        if k in low:
            return v
    return t  # 兜底：保留原样


def fetch(url):
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    with urllib.request.urlopen(req, timeout=60) as r:
        raw = r.read().decode("utf-8", "ignore")
    return raw


def parse_m3u(text):
    """返回 [(name, logo, groups:list[str], url, tvg_id)]"""
    out = []
    lines = text.splitlines()
    i = 0
    n = len(lines)
    while i < n:
        line = lines[i].strip()
        if line.startswith("#EXTINF"):
            m = re.search(r'group-title="([^"]*)"', line)
            logo_m = re.search(r'tvg-logo="([^"]*)"', line)
            id_m = re.search(r'tvg-id="([^"]*)"', line)
            name_m = re.search(r",(.+)$", line)
            groups = m.group(1).split(";") if m else []
            name = name_m.group(1).strip() if name_m else "未命名"
            # 去掉名称末尾的画质/状态标签，让名字更干净
            name = re.sub(r"\s*[\(\[].*?[\)\]]\s*$", "", name).strip() or name
            logo = logo_m.group(1) if logo_m else ""
            tvg_id = id_m.group(1) if id_m else ""
            # 找紧随其后的 URL
            j = i + 1
            url = ""
            while j < n:
                cand = lines[j].strip()
                if not cand:
                    j += 1
                    continue
                if cand.startswith("#"):
                    j += 1
                    continue
                url = cand
                break
            if url and url.lower().startswith(("http://", "https://", "rtmp://", "rtsp://", "udp://", "rtp://")):
                out.append((name, logo, groups, url, tvg_id))
            i = j + 1
        else:
            i += 1
    return out


def build(entries):
    # category name -> id
    cat_id = {}
    categories = []

    def get_cat(name):
        if name not in cat_id:
            cid = len(categories) + 1
            cat_id[name] = cid
            categories.append({
                "id": cid,
                "name": name,
                "en_name": name,
                "logo": "",
                "sort": cid,
            })
        return cat_id[name]

    channels = []
    seen_urls = set()
    cid_counter = 1

    for name, logo, groups, url, tvg_id in entries:
        if url in seen_urls:
            continue
        seen_urls.add(url)

        # 分类集合：来自 group-title + 中文名称规则
        cat_names = set()
        for g in groups:
            cat_names.add(norm_group(g))
        for rx, cn in CN_RULES:
            if rx.search(name):
                cat_names.add(cn)

        cids = sorted(get_cat(c) for c in cat_names)
        bits = 0
        for c in cids:
            bits |= (1 << (c - 1))

        # 判断是否偏中国（用于 area 标注，便于 App 可能的地区筛选）
        area = "中国" if any(k in name for k in ("CCTV", "央视", "卫视")) or "中国" in name else ""

        channels.append({
            "id": cid_counter,
            "name": name,
            "en_name": name,
            "pinyin": "",
            "channel_code": cid_counter,
            "logo": logo,
            "level": 0,                 # 全部免费，无需登录
            "category_bits": bits,
            "category_ids": cids,
            "epg_id": tvg_id,          # 节目单匹配键（与 EPG 源的频道 ID 对齐才生效）
            "catchup_id": "",
            "timeshift_id": "",
            "region_id": 0,
            "area": area,
            "urls": [url],
        })
        cid_counter += 1

    # 分类按 id 排序输出
    categories.sort(key=lambda c: c["id"])
    return {"categories": categories, "channels": channels}


def main():
    out_path = sys.argv[1] if len(sys.argv) > 1 else "channels.json"
    all_entries = []
    for url in SOURCES:
        try:
            print(f"[抓取] {url}")
            txt = fetch(url)
            ent = parse_m3u(txt)
            print(f"        -> {len(ent)} 条")
            all_entries.extend(ent)
        except Exception as e:
            print(f"        !! 失败，跳过: {e}")

    print(f"[合计] 原始条目 {len(all_entries)}")

    print(f"[合计] 原始条目 {len(all_entries)}")
    data = build(all_entries)
    data["generated_at"] = int(datetime.datetime.now().timestamp())

    with open(out_path, "w", encoding="utf-8", newline="\n") as f:
        json.dump(data, f, ensure_ascii=False, indent=1)
    print(f"[写出] {out_path} | 频道 {len(data['channels'])} | 分类 {len(data['categories'])}")


if __name__ == "__main__":
    main()
