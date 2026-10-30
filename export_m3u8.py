#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""把过滤后的 channels.json 导出为标准 M3U8 播放列表。

在 GitHub Action 中于 filter_sources.py 之后运行，保证 .m3u8 与
channels.json 内容一致（同样的频道、同样的 URL 顺序）。

用法:
    python3 export_m3u8.py [channels.json 路径] [输出 .m3u8 路径]

输出规则:
  - 每个频道的每个 URL 输出一条 #EXTINF（播放器按顺序尝试）；
  - trusted 频道（央视/卫视）排在最前，其余按分类 id、频道名排序；
  - group-title 取频道所属分类名（多个分类取第一个）；
  - tvg-id / tvg-logo 透传，供带 EPG 的播放器使用。
"""
import json
import sys


def esc(s):
    """转义 EXTINF 属性值中的双引号。"""
    return (s or "").replace('"', "'")


def main():
    json_path = sys.argv[1] if len(sys.argv) > 1 else "channels.json"
    m3u8_path = sys.argv[2] if len(sys.argv) > 2 else "live.m3u8"

    with open(json_path, encoding="utf-8") as f:
        data = json.load(f)

    channels = data.get("channels") or []
    cat_name = {c["id"]: c.get("name", "") for c in (data.get("categories") or [])}

    def sort_key(c):
        return (
            0 if c.get("trusted") else 1,          # 央视/卫视在前
            min((cat_name.get(i, "z") or "z"
                 for i in (c.get("category_ids") or [99])),
                default="z"),                      # 分类名
            c.get("name", ""),
        )

    channels.sort(key=sort_key)

    lines = ["#EXTM3U"]
    n_entries = 0
    for ch in channels:
        name = ch.get("name", "").strip()
        if not name:
            continue
        cats = [cat_name.get(i, "") for i in (ch.get("category_ids") or [])]
        group = next((x for x in cats if x), "未分类")
        logo = esc(ch.get("logo", ""))
        tvg_id = esc(ch.get("epg_id", ""))
        for u in (ch.get("urls") or []):
            if not u:
                continue
            lines.append(
                f'#EXTINF:-1 tvg-id="{tvg_id}" tvg-logo="{logo}" '
                f'group-title="{esc(group)}",{name}'
            )
            lines.append(u)
            n_entries += 1

    with open(m3u8_path, "w", encoding="utf-8", newline="\n") as f:
        f.write("\n".join(lines) + "\n")

    print(f"[导出] {m3u8_path} | 频道 {len(channels)} | 播放条目 {n_entries}")


if __name__ == "__main__":
    main()
