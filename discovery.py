#!/usr/bin/env python3
"""
Авто-открытие новых источников. Запускается каждый день перед сбором дайджеста.

Логика: качественные малые каналы всплывают там, где на них ссылаются практики.
Обходим граф упоминаний и репостов вокруг уже доверенных источников,
новичков держим в карантине и повышаем в основную базу только после
наблюдения за реальным контентом.

Использование:
    python3 discovery.py --sources sources.yaml --state state/ --run
    python3 discovery.py --state state/ --report      # что в карантине
"""

import argparse, json, os, random, re, sys, time, html
import urllib.request, urllib.error
from datetime import datetime, timedelta, timezone

MSK = timezone(timedelta(hours=3))
UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/122.0 Safari/537.36"

# служебное, что никогда не является каналом-источником
JUNK = {"s", "share", "joinchat", "iv", "proxy", "addstickers", "setlanguage",
        "telegram", "durov", "toc", "addlist", "c"}

RE_MENTION = re.compile(r'href="https://t\.me/([A-Za-z0-9_]{4,32})(?:/\d+)?"')
RE_FORWARD = re.compile(r'forwarded_from_name"\s+href="https://t\.me/([A-Za-z0-9_]{4,32})')
RE_POSTTEXT = re.compile(
    r'<div class="tgme_widget_message_text[^"]*"[^>]*>(.*?)</div>', re.S)
RE_DATETIME = re.compile(r'datetime="([^"]+)"')
RE_TITLE = re.compile(r'<meta property="og:title" content="([^"]*)"')
RE_DESC = re.compile(r'<meta property="og:description" content="([^"]*)"')
RE_SUBS = re.compile(
    r'counter_value">([\d\s.,KMkМм]+)</span>\s*'
    r'<span class="counter_type">\s*(?:subscribers|подписчик)')
RE_TAG = re.compile(r'<[^>]+>')

# признаки рекламного поста
AD_MARKERS = re.compile(
    r'erid|реклама\.\s*ооо|рекламодател|#промо|партнёрский материал|'
    r'партнерский материал|по промокоду|успей купить|старт потока|'
    r'запись вебинара|регистрация по ссылке', re.I)

# признаки контента с конкретикой — то, ради чего всё затевается
SUBSTANCE = re.compile(
    r'\d[\d\s.,]*\s*(₽|руб|рублей|\$|%|тыс|млн|k\b)|'
    r'\bкейс\b|\bразбор\b|\bтест(ил|ировал)\b|\bэксперимент\b|'
    r'\bCPL\b|\bCPA\b|\bCPM\b|\bCPC\b|\bROI\b|\bROAS\b|\bДРР\b|\bCTR\b|'
    r'\bконверси|\bбюджет\b|\bвыручк|\bокупаем', re.I)


def fetch(url, retries=2, pause=0.8):
    for attempt in range(retries + 1):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": UA})
            with urllib.request.urlopen(req, timeout=25) as r:
                return r.read().decode("utf-8", "ignore")
        except urllib.error.HTTPError as e:
            if e.code == 429:
                time.sleep(5 * (attempt + 1))
                continue
            return ""
        except Exception:
            time.sleep(pause)
    return ""


def parse_subs(raw):
    if not raw:
        return None
    s = raw.replace(" ", "").replace(" ", "").replace(",", "")
    mult = 1
    if s and s[-1] in "Kk":
        mult, s = 1000, s[:-1]
    elif s and s[-1] in "Mм":
        mult, s = 1_000_000, s[:-1]
    try:
        return int(float(s) * mult)
    except ValueError:
        return None


def channel_snapshot(username):
    """Скачивает превью канала и возвращает метрики + тексты постов."""
    h = fetch(f"https://t.me/s/{username}")
    if not h:
        return None
    texts = []
    for block in RE_POSTTEXT.findall(h):
        t = html.unescape(RE_TAG.sub(" ", block.replace("<br/>", "\n")))
        t = re.sub(r"[ \t]+", " ", t).strip()
        if t:
            texts.append(t)
    if not texts:
        return None
    dates = RE_DATETIME.findall(h)
    title = RE_TITLE.search(h)
    desc = RE_DESC.search(h)
    subs = RE_SUBS.search(h)
    return {
        "u": username,
        "title": html.unescape(title.group(1)) if title else "",
        "desc": html.unescape(desc.group(1)) if desc else "",
        "subs": parse_subs(subs.group(1)) if subs else None,
        "posts": len(texts),
        "last_post": dates[-1] if dates else None,
        "avg_len": round(sum(len(t) for t in texts) / len(texts)),
        "ad_share": round(sum(1 for t in texts if AD_MARKERS.search(t)) / len(texts), 2),
        "substance_share": round(sum(1 for t in texts if SUBSTANCE.search(t)) / len(texts), 2),
        "sample": texts[-3:],
        "mentions": [m.lower() for m in RE_MENTION.findall(h)],
        "forwards": [m.lower() for m in RE_FORWARD.findall(h)],
    }


def days_since(iso):
    if not iso:
        return 999
    try:
        d = datetime.fromisoformat(iso.replace("Z", "+00:00"))
    except ValueError:
        return 999
    return (datetime.now(timezone.utc) - d).days


def load_state(path):
    f = os.path.join(path, "discovery.json")
    if os.path.exists(f):
        return json.load(open(f, encoding="utf-8"))
    return {"quarantine": {}, "promoted": [], "rejected": {}, "runs": 0}


def save_state(path, st):
    os.makedirs(path, exist_ok=True)
    json.dump(st, open(os.path.join(path, "discovery.json"), "w", encoding="utf-8"),
              ensure_ascii=False, indent=1)


def known_usernames(sources):
    out = set()
    for k, v in sources.items():
        if k.startswith("tg_") and isinstance(v, list):
            for item in v:
                out.add(item["u"].lower())
    return out


def crawl(sources, state, cfg, today):
    """Один проход: обходим ротируемую выборку сидов, собираем кандидатов."""
    known = known_usernames(sources)
    seeds = sorted(known)
    random.seed(today)                       # детерминированная ротация по дате
    random.shuffle(seeds)
    seeds = seeds[: cfg.get("seeds_per_run", 40)]

    found = {}
    for i, s in enumerate(seeds):
        snap = channel_snapshot(s)
        time.sleep(0.7)
        if not snap:
            continue
        # репост весит больше упоминания: автор сознательно ретранслирует
        for u in snap["forwards"]:
            found[u] = found.get(u, 0) + 3
        for u in snap["mentions"]:
            found[u] = found.get(u, 0) + 1

    lo, hi = cfg.get("prefer_subscribers_range", [200, 30000])
    fresh = []
    for u, weight in sorted(found.items(), key=lambda x: -x[1]):
        if (u in known or u in JUNK or u.endswith("_bot") or u.endswith("chat")
                or u in state["quarantine"] or u in state["rejected"]
                or u in state["promoted"]):
            continue
        fresh.append((u, weight))

    print(f"[crawl] сидов обойдено: {len(seeds)}, новых кандидатов: {len(fresh)}")

    added = 0
    for u, weight in fresh[:60]:             # потолок на прогон, чтобы не долбить t.me
        snap = channel_snapshot(u)
        time.sleep(0.7)
        if not snap:
            state["rejected"][u] = {"why": "превью закрыто", "at": today}
            continue
        if days_since(snap["last_post"]) > 10:
            state["rejected"][u] = {"why": f"молчит {days_since(snap['last_post'])} дн", "at": today}
            continue
        subs = snap["subs"]
        if subs is not None and not (lo <= subs <= hi * 4):
            state["rejected"][u] = {"why": f"{subs} подписчиков вне диапазона", "at": today}
            continue
        snap["weight"] = weight
        snap["first_seen"] = today
        snap["observations"] = 1
        state["quarantine"][u] = snap
        added += 1
    print(f"[crawl] в карантин добавлено: {added}")
    return added


def review_quarantine(state, cfg, today):
    """Решаем судьбу тех, кто отсидел карантин."""
    promo = cfg.get("promote_if", {})
    drop = cfg.get("drop_if", {})
    hold_days = cfg.get("quarantine_days", 7)
    ready, dropped = [], []

    for u, c in list(state["quarantine"].items()):
        age = (datetime.fromisoformat(today) - datetime.fromisoformat(c["first_seen"])).days
        snap = channel_snapshot(u)
        time.sleep(0.7)
        if snap:
            snap.update(weight=c["weight"], first_seen=c["first_seen"],
                        observations=c["observations"] + 1)
            state["quarantine"][u] = c = snap

        silent = days_since(c.get("last_post"))
        if silent > drop.get("silent_days", 21) or c["ad_share"] > drop.get("ad_share_above", 0.5):
            why = f"молчит {silent} дн" if silent > 21 else f"реклама {int(c['ad_share']*100)}%"
            state["rejected"][u] = {"why": why, "at": today}
            del state["quarantine"][u]
            dropped.append((u, why))
            continue

        if age < hold_days:
            continue

        ok = (c["posts"] >= promo.get("min_posts_in_quarantine", 5)
              and c["ad_share"] <= promo.get("max_ad_share", 0.33)
              and c["avg_len"] >= promo.get("min_avg_post_length", 600)
              and c["substance_share"] >= 0.25)
        if ok:
            ready.append(c)          # финальное слово — за моделью, см. README
        else:
            why = (f"постов {c['posts']}, реклама {int(c['ad_share']*100)}%, "
                   f"средняя длина {c['avg_len']}, конкретика {int(c['substance_share']*100)}%")
            state["rejected"][u] = {"why": why, "at": today}
            del state["quarantine"][u]
            dropped.append((u, why))

    return ready, dropped


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--sources", default="sources.yaml")
    ap.add_argument("--state", default="state")
    ap.add_argument("--run", action="store_true")
    ap.add_argument("--report", action="store_true")
    a = ap.parse_args()

    import yaml
    sources = yaml.safe_load(open(a.sources, encoding="utf-8"))
    cfg = sources.get("discovery", {})
    state = load_state(a.state)
    today = datetime.now(MSK).date().isoformat()

    if a.report:
        print(f"В карантине: {len(state['quarantine'])}, "
              f"повышено: {len(state['promoted'])}, отсеяно: {len(state['rejected'])}")
        for u, c in sorted(state["quarantine"].items(), key=lambda x: -x[1]["weight"]):
            print(f"  @{u:28} вес {c['weight']:>3}  подп {str(c['subs']):>7}  "
                  f"длина {c['avg_len']:>5}  реклама {c['ad_share']:.0%}  "
                  f"конкретика {c['substance_share']:.0%}  {c['title'][:40]}")
        return

    if not a.run:
        ap.error("укажи --run или --report")

    crawl(sources, state, cfg, today)
    ready, dropped = review_quarantine(state, cfg, today)
    state["runs"] += 1
    save_state(a.state, state)

    # кандидаты на повышение выгружаются отдельно: их читает модель
    # и выносит финальный вердикт «авторский контент с конкретикой»
    out = os.path.join(a.state, "to_review.json")
    json.dump(ready, open(out, "w", encoding="utf-8"), ensure_ascii=False, indent=1)

    print(f"\nОтсидели карантин и ждут вердикта модели: {len(ready)} → {out}")
    for c in ready:
        print(f"  @{c['u']} — {c['title']} ({c['subs']} подп, "
              f"средний пост {c['avg_len']} знаков, конкретика {c['substance_share']:.0%})")
    if dropped:
        print(f"\nОтсеяно: {len(dropped)}")
        for u, why in dropped[:15]:
            print(f"  @{u} — {why}")


if __name__ == "__main__":
    main()
