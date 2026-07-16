#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
小红书 一体化采集管线
    关键词搜索  ->  每篇作品:  原图(XHS-Downloader 下高清)  +  一级评论(Playwright 拦截)

两件事在一次登录里完成:
    * Playwright 驱动真浏览器: 扫码登录 -> 搜索 -> 滚动收集笔记 -> 抓一级评论
    * 登录后把浏览器 cookie 直接喂给 XHS-Downloader, 由它下载高清原图(轮播图下全)
两边都以 note_id 为主键, 结束时生成 manifest 把 图片文件夹 <-> 评论 对齐。

运行(必须用 XHS-Downloader 的 uv 环境, 里面有 XHS 和 playwright):
    uv run --project /Users/kapozux/Documents/XHS-Downloader \
        python /Users/kapozux/Documents/CODEelse/xhs_pipeline.py

首次运行会弹浏览器 -> 扫码登录 -> 回终端按回车。登录态存在 xhs_user_data/, 之后免扫。

产出(都在 xhs_dataset/ 下):
    images/<note_id>/*.png     每篇作品的高清原图 (给后续 LLM 读图)
    comments.csv               一级评论: note_id, comment_text, like_count, label(留空供标注)
    notes.csv                  笔记清单: note_id, title, author, type, note_url
    manifest.csv               对齐表: note_id, image_count, comment_count, image_dir
"""

import asyncio
import csv
import json
import os
import random
import re
import sys
from urllib.parse import urlparse, parse_qs

# --- 接入 XHS-Downloader 源码 (二次开发) ---
XHS_DOWNLOADER_DIR = "/Users/kapozux/Documents/XHS-Downloader"
sys.path.insert(0, XHS_DOWNLOADER_DIR)
from source import XHS  # noqa: E402

from playwright.async_api import async_playwright  # noqa: E402

# ============================================================
# 配置区
# ============================================================

# 一组关键词: 分别搜、去重合并, 每个词只取最相关的前一批(避开各词的"飘尾")。
# 想调研什么就改这里 —— 现在聚焦 CS 强校美本 bg/申请。
KEYWORDS = [
    "uiuc 美本 bg",
    "uiuc cs 录取",
    "ucsd cs 美本",
    "cmu cs 本科",          # 锚定"本科", 避免漏进美研/PhD
    "gatech cs 美本",
    "美本 cs 选校",
    "美本 cs bg",
    "美本 cs 录取",
    "美本 计算机 本科",
    "uw cs 美本",
]
MAX_NOTES = 100                 # 合并去重后最多抓多少篇作品
MAX_COMMENTS_PER_NOTE = 800     # 每篇一级评论上限(设大=尽量抓全, 到"THE END"自动停)

# —— 外部驱动(Verbatim 调用时用环境变量覆盖，不填就用上面的默认)——
#   XHS_KEYWORDS  多个关键词用换行或 || 分隔
#   XHS_MAX_NOTES / XHS_MAX_COMMENTS  数量上限
_env_kw = os.environ.get("XHS_KEYWORDS", "").strip()
if _env_kw:
    KEYWORDS = [k.strip() for k in re.split(r"\n|\|\|", _env_kw) if k.strip()]
if os.environ.get("XHS_MAX_NOTES"):
    try:
        MAX_NOTES = int(os.environ["XHS_MAX_NOTES"])
    except ValueError:
        pass
if os.environ.get("XHS_MAX_COMMENTS"):
    try:
        MAX_COMMENTS_PER_NOTE = int(os.environ["XHS_MAX_COMMENTS"])
    except ValueError:
        pass

DATASET_DIR = "/Users/kapozux/Documents/CODEelse/xhs_dataset"      # 数据集根目录
USER_DATA_DIR = "/Users/kapozux/Documents/CODEelse/xhs_user_data"  # 登录态(别删)

IMAGE_FORMAT = "PNG"      # 原图格式: PNG(无损, 适合 LLM 读图) / JPEG / WEBP / HEIC / AUTO
VIDEO_DOWNLOAD = True     # 视频作品是否也下(命中视频笔记时). 只要图文可设 False
USE_BROWSER_COOKIE = True # 把浏览器登录 cookie 传给下载器换取最高画质; 若下载异常可设 False
HEADLESS = False          # 必须 False(要扫码, 且有头更不易被风控)

# 延迟(秒), 放慢节奏降低风控概率
NOTE_DELAY = (6.0, 11.0)        # 每篇之间(放慢, 防风控)
SCROLL_DELAY = (1.5, 2.5)       # 评论翻页滚动之间
COOLDOWN_EVERY = 20             # 每处理 N 篇, 长歇一次
COOLDOWN = (45.0, 90.0)         # 长歇时长(秒), 给风控降温

SEARCH_URL = "https://www.xiaohongshu.com/search_result?keyword={kw}&source=web_search_result_notes"

# ============================================================
# 全局收集器 (被响应监听器填充)
# ============================================================

search_notes = {}     # note_id -> {"id","xsec_token","title","author","kw"}
note_comments = {}    # note_id -> [ {text, like} ]
current_kw = ""       # 当前正在搜的关键词(给新收集到的笔记打来源标签)

_EMOTE_RE = re.compile(r"\[[^\[\]]{1,15}\]")


def is_meaningless(text: str) -> bool:
    """纯表情 / 空 评论过滤(和 B站脚本保持一致)。"""
    if not text:
        return True
    stripped = _EMOTE_RE.sub("", text).strip()
    stripped = re.sub(r"[\s​‌‍﻿]+", "", stripped)
    return stripped == ""


# ============================================================
# 接口响应监听: 搜索结果 + 一级评论
# ============================================================

async def on_response(response):
    url = response.url
    try:
        if "/search/notes" in url:            # so.xiaohongshu.com/api/sns/web/v2/search/notes
            data = await response.json()
            for it in (data.get("data") or {}).get("items") or []:
                if it.get("model_type") not in ("note", None):
                    continue
                nid = it.get("id")
                if not nid:
                    continue
                nc = it.get("note_card") or {}
                token = it.get("xsec_token") or ""
                cur = search_notes.get(nid)
                if cur is None:
                    search_notes[nid] = {
                        "id": nid,
                        "xsec_token": token,
                        "title": (nc.get("display_title") or "").strip(),
                        "author": ((nc.get("user") or {}).get("nickname") or "").strip(),
                        "kw": current_kw,
                    }
                elif token and not cur["xsec_token"]:
                    cur["xsec_token"] = token

        elif "/comment/page" in url:          # /api/sns/web/vN/comment/page (一级评论)
            nid = (parse_qs(urlparse(url).query).get("note_id") or [""])[0]
            if not nid:
                return
            data = await response.json()
            bucket = note_comments.setdefault(nid, [])
            for c in (data.get("data") or {}).get("comments") or []:
                text = (c.get("content") or "").strip()
                if is_meaningless(text):
                    continue
                bucket.append({"text": text, "like": c.get("like_count") or "0"})
    except Exception:
        pass  # 非 JSON / body 已释放, 忽略


# ============================================================
# 浏览器动作
# ============================================================

async def load_comments(page, nid, cap, max_rounds=120):
    """滚动评论容器 .note-scroller 触发翻页, 抓到 cap 条 / '- THE END -' / 无新增为止。"""
    stale, last = 0, len(note_comments.get(nid, []))
    for _ in range(max_rounds):
        if len(note_comments.get(nid, [])) >= cap:
            break
        await page.evaluate(
            """() => {
                const its = document.querySelectorAll('.parent-comment, .comment-item');
                if (its.length) its[its.length - 1].scrollIntoView({block: 'end', behavior: 'instant'});
                const sc = document.querySelector('.note-scroller');
                if (sc) sc.scrollTop = sc.scrollHeight;
            }"""
        )
        await asyncio.sleep(random.uniform(*SCROLL_DELAY))
        ended = await page.evaluate(
            "() => { const e = document.querySelector('.end-container,.no-more,.comment-end');"
            " return !!(e && /THE END|没有更多|到底了/.test(e.textContent || '')); }"
        )
        cur = len(note_comments.get(nid, []))
        if cur <= last:
            stale += 1
            if ended or stale >= 5:
                break
        else:
            stale = 0
        last = cur


def select_notes(per_kw):
    """每个关键词只留最相关的前 per_kw 篇(搜索结果靠前=更相关), 保证学校多样性;
    不足 MAX_NOTES 再从剩余里补齐。"""
    usable = [n for n in search_notes.values() if n["xsec_token"]]
    picked, ids, cnt = [], set(), {}
    for n in usable:                       # 第一轮: 每词头部, 保多样性
        k = n.get("kw", "")
        if cnt.get(k, 0) >= per_kw:
            continue
        cnt[k] = cnt.get(k, 0) + 1
        picked.append(n)
        ids.add(n["id"])
    if len(picked) < MAX_NOTES:            # 第二轮: 没凑够就补
        for n in usable:
            if n["id"] not in ids:
                picked.append(n)
                ids.add(n["id"])
                if len(picked) >= MAX_NOTES:
                    break
    return picked[:MAX_NOTES]


async def collect_notes_from_dom(page):
    """DOM 兜底: 抓带 xsec_token 的笔记卡片链接(/search_result/{id}?xsec_token=...)。"""
    try:
        hrefs = await page.eval_on_selector_all(
            "a[href*='xsec_token']",
            "els => els.map(e => e.getAttribute('href'))",
        )
    except Exception:
        hrefs = []
    for href in hrefs:
        if not href:
            continue
        m = re.search(r"/(?:explore|discovery/item|search_result)/([0-9a-zA-Z]+)", href)
        if not m:
            continue
        nid = m.group(1)
        token = (parse_qs(urlparse(href).query).get("xsec_token") or [""])[0]
        if nid not in search_notes:
            search_notes[nid] = {"id": nid, "xsec_token": token, "title": "", "author": "", "kw": current_kw}
        elif token and not search_notes[nid]["xsec_token"]:
            search_notes[nid]["xsec_token"] = token


async def collect_search_notes(page, target, max_rounds=40):
    """边滚动边从 DOM 收集笔记, 直到够数或到底。"""
    stale, last = 0, 0
    for _ in range(max_rounds):
        await collect_notes_from_dom(page)
        if len(search_notes) >= target:
            break
        await page.mouse.wheel(0, 3000)
        await asyncio.sleep(random.uniform(*SCROLL_DELAY))
        cur = len(search_notes)
        if cur <= last:
            stale += 1
            if stale >= 4:
                break
        else:
            stale = 0
        last = cur
    await collect_notes_from_dom(page)


async def goto_retry(page, url, tries=3):
    """goto 带重试(小红书偶发 goto 超时)。"""
    for i in range(tries):
        try:
            await page.goto(url, wait_until="domcontentloaded", timeout=60000)
            return True
        except Exception as e:
            print(f"    goto 重试 {i + 1}/{tries}: {e}")
            await asyncio.sleep(3)
    return False


async def ensure_login(page):
    await page.goto("https://www.xiaohongshu.com", wait_until="domcontentloaded")
    await asyncio.sleep(3)
    try:
        need_login = await page.locator("text=登录").first.is_visible(timeout=3000)
    except Exception:
        need_login = False
    if need_login:
        print("\n[登录] 请在弹出的浏览器里扫码登录小红书。")
        loop = asyncio.get_event_loop()
        await loop.run_in_executor(None, input, "      登录完成后回到这里按【回车】继续...")
    else:
        print("[登录] 已是登录态(复用本地登录)。")


# ============================================================
# 主流程
# ============================================================

async def main():
    os.makedirs(DATASET_DIR, exist_ok=True)

    async with async_playwright() as p:
        context = await p.chromium.launch_persistent_context(
            USER_DATA_DIR,
            headless=HEADLESS,
            viewport={"width": 1280, "height": 900},
            args=["--disable-blink-features=AutomationControlled"],
        )
        page = context.pages[0] if context.pages else await context.new_page()
        page.on("response", lambda r: asyncio.create_task(on_response(r)))

        await ensure_login(page)

        # 取浏览器 cookie 传给下载器(换最高画质)
        cookie_str = ""
        if USE_BROWSER_COOKIE:
            cks = await context.cookies("https://www.xiaohongshu.com")
            cookie_str = "; ".join(f"{c['name']}={c['value']}" for c in cks)
            print(f"[cookie] 已从浏览器取得 {len(cks)} 项 cookie 传给下载器")

        # 1. 遍历全部关键词各搜一屏(保证每所学校都覆盖), 再每词只留最相关的头部, 去重合并
        global current_kw
        per_kw = max(6, (MAX_NOTES + len(KEYWORDS) - 1) // len(KEYWORDS))
        print(f"\n[搜索] {len(KEYWORDS)} 个关键词各搜一屏, 每词留前 ~{per_kw} 篇, 合并目标 {MAX_NOTES} 篇")
        for kw in KEYWORDS:
            current_kw = kw
            before = len(search_notes)
            await goto_retry(page, SEARCH_URL.format(kw=kw))
            await asyncio.sleep(3)
            await collect_search_notes(page, before + per_kw)   # 第一屏即够, 基本不滚
            print(f"  「{kw}」新增 {len(search_notes) - before} 篇 (累计 {len(search_notes)})")
            await asyncio.sleep(random.uniform(*NOTE_DELAY))
        notes = select_notes(per_kw)
        print(f"[搜索] 合并去重后 {len(notes)} 篇(带 token, 覆盖 {len(set(n['kw'] for n in notes))} 个关键词)")

        note_rows, processed = [], []

        # 2. XHS-Downloader 实例(带登录 cookie), 逐篇下高清图
        #    图片落在 xhs_dataset/notes/<note_id>/, 稍后把该篇评论+元信息也写进同一文件夹
        async with XHS(
            work_path=DATASET_DIR,
            folder_name="notes",
            name_format="作品ID",        # 文件夹名 = note_id, 便于对齐
            folder_mode=True,            # 每篇一个文件夹
            author_archive=False,
            image_format=IMAGE_FORMAT,
            image_download=True,
            video_download=VIDEO_DOWNLOAD,
            live_download=False,
            download_record=True,        # 记录已下 ID, 重复跑自动跳过
            record_data=False,           # 作品数据我们自己写 CSV
            cookie=cookie_str,
            language="zh_CN",
        ) as xhs:
            for i, n in enumerate(notes, 1):
                nid, token = n["id"], n["xsec_token"]
                print(f"\n[{i}/{len(notes)}] {nid}  {n['title'][:20]}")
                note_url = f"https://www.xiaohongshu.com/explore/{nid}?xsec_token={token}&xsec_source=pc_search"
                ntype = ""
                try:
                    # 2a. 进详情页, 滚评论区 -> 抓全一级评论(靠拦截)
                    await goto_retry(page, note_url)
                    await asyncio.sleep(3)
                    await load_comments(page, nid, MAX_COMMENTS_PER_NOTE)
                    # 2b. 下高清原图(下全)
                    info = await xhs.extract(note_url, True)
                    if isinstance(info, list) and info:
                        info = info[0]
                    info = info if isinstance(info, dict) else {}
                    ntype = info.get("作品类型", "")
                    title = info.get("作品标题", "") or n["title"]
                    author = info.get("作者昵称", "") or n["author"]
                    cmts = note_comments.get(nid, [])[:MAX_COMMENTS_PER_NOTE]
                    kw = n.get("kw", "")
                    print(f"      类型 {ntype or '?'} | 一级评论 {len(cmts)} 条 | 来源词「{kw}」")
                    # 2c. 把该篇评论 + 元信息写进同一文件夹, 图评一眼对应
                    write_note_folder(nid, title, author, ntype, kw, note_url, cmts)
                    note_rows.append((nid, title, author, ntype, kw, note_url))
                    processed.append(nid)
                except Exception as e:
                    print(f"      [!] 处理失败: {e}")

                if i < len(notes):
                    await asyncio.sleep(random.uniform(*NOTE_DELAY))

        await context.close()

    write_aggregates(note_rows, processed)


_IMG_EXT = (".png", ".jpg", ".jpeg", ".webp", ".heic", ".gif")


def note_dir_of(nid):
    return os.path.join(DATASET_DIR, "notes", nid)


def count_images(nid):
    d = note_dir_of(nid)
    if not os.path.isdir(d):
        return 0
    return sum(1 for x in os.listdir(d) if x.lower().endswith(_IMG_EXT))


def write_note_folder(nid, title, author, ntype, kw, note_url, cmts):
    """把单篇的 评论 + 元信息 写进它自己的文件夹(和图片同一个目录)。"""
    d = note_dir_of(nid)
    os.makedirs(d, exist_ok=True)
    # 该篇评论
    with open(os.path.join(d, "comments.csv"), "w", newline="", encoding="utf-8-sig") as f:
        w = csv.writer(f)
        w.writerow(["comment_text", "like_count", "label"])
        for c in cmts:
            w.writerow([c["text"], c["like"], ""])
    # 该篇元信息
    meta = {
        "note_id": nid, "title": title, "author": author, "type": ntype,
        "source_keyword": kw, "note_url": note_url, "comment_count": len(cmts),
    }
    with open(os.path.join(d, "meta.json"), "w", encoding="utf-8") as f:
        json.dump(meta, f, ensure_ascii=False, indent=2)


def write_aggregates(note_rows, processed):
    """顶层汇总: notes.csv / all_comments.csv(接标注) / manifest.csv(对齐总览)。"""
    with open(os.path.join(DATASET_DIR, "notes.csv"), "w", newline="", encoding="utf-8-sig") as f:
        w = csv.writer(f)
        w.writerow(["note_id", "title", "author", "type", "source_keyword",
                    "comment_count", "image_count", "note_url"])
        for (nid, title, author, ntype, kw, note_url) in note_rows:
            w.writerow([nid, title, author, ntype, kw,
                        len(note_comments.get(nid, [])), count_images(nid), note_url])

    total_c = 0
    with open(os.path.join(DATASET_DIR, "all_comments.csv"), "w", newline="", encoding="utf-8-sig") as f:
        w = csv.writer(f)
        w.writerow(["note_id", "comment_text", "like_count", "label"])
        for nid in processed:
            for c in note_comments.get(nid, [])[:MAX_COMMENTS_PER_NOTE]:
                w.writerow([nid, c["text"], c["like"], ""])
                total_c += 1

    total_img = 0
    with open(os.path.join(DATASET_DIR, "manifest.csv"), "w", newline="", encoding="utf-8-sig") as f:
        w = csv.writer(f)
        w.writerow(["note_id", "image_count", "comment_count", "folder"])
        for nid in processed:
            ic = count_images(nid)
            total_img += ic
            w.writerow([nid, ic, len(note_comments.get(nid, [])), note_dir_of(nid)])

    print(f"\n完成! 作品 {len(processed)} 篇 | 图片 {total_img} 张 | 一级评论 {total_c} 条")
    print(f"数据集目录: {DATASET_DIR}")
    print("  notes/<note_id>/       每篇: 原图 + comments.csv + meta.json (图评对应)")
    print("  all_comments.csv       全部评论(带 note_id, 接 BERT/SVM 标注)")
    print("  notes.csv / manifest.csv   总览与对齐表")


if __name__ == "__main__":
    asyncio.run(main())
