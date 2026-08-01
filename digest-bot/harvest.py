#!/usr/bin/env python3
"""
Сборщик. Тянет вчерашний день из всех источников, чистит, дедуплицирует,
прогоняет через дешёвый предфильтр и отдаёт компактный JSON,
который дальше читает модель.

Модель НЕ должна видеть сырьё: 400+ источников дают тысячи записей,
из них до неё доходит несколько сотен строк заголовков.

    python3 harvest.py --sources sources.yaml --state state/ --out out/
    python3 harvest.py --date 2026-07-30          # конкретный день
    python3 harvest.py --only tg,rss              # частичный прогон
"""

import argparse, concurrent.futures as cf, hashlib, html, json, os, re, sys, time
import urllib.request, urllib.error
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone

MSK = timezone(timedelta(hours=3))
UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/122.0 Safari/537.36"

RE_TAG = re.compile(r"<[^>]+>")
RE_WS = re.compile(r"[ \t ]+")
RE_POST = re.compile(
    r'<div class="tgme_widget_message_wrap.*?data-post="([^"]+)".*?'
    r'(?:<time datetime="([^"]+)")?.*?'
    r'<div class="tgme_widget_message_text[^"]*"[^>]*>(.*?)</div>', re.S)
RE_MSG_BLOCK = re.compile(r'<div class="tgme_widget_message_wrap.*?(?=<div class="tgme_widget_message_wrap|$)', re.S)
RE_DATA_POST = re.compile(r'data-post="([^"]+)"')
RE_TIME = re.compile(r'datetime="([^"]+)"')
RE_TEXT = re.compile(r'<div class="tgme_widget_message_text[^"]*"[^>]*>(.*?)</div>\s*(?:<div class="tgme_widget_message_(?:footer|reply_markup)|</div>)', re.S)
RE_VIEWS = re.compile(r'tgme_widget_message_views">([\d.,KMkм ]+)<')

# --- предфильтр -----------------------------------------------------
# то, что вылетает сразу и никогда не доходит до модели
NOISE = re.compile(
    r"erid[:\s]|реклама\.\s*ооо|рекламодател|#промо|по промокоду|"
    r"вакансия|ищем в команду|резюме|мы ищем|открыт набор|"
    r"розыгрыш|конкурс среди подписчиков|разыгрываем|"
    r"успей купить|последние места|старт потока|осталось \d+ мест|"
    r"регистрация на вебинар|запись вебинара|приходи на эфир|"
    r"10 промптов|топ[- ]\d+ нейросет|подборка нейросет|"
    r"с днём рождения|с праздником|доброе утро", re.I)

# то, что поднимает запись в приоритет — фактура
SIGNAL = re.compile(
    r"\d[\d\s.,]*\s*(?:₽|руб|рублей|\$|%|тыс|млн|млрд)|"
    r"\bкейс\b|\bразбор\b|\bэксперимент\b|тестировал|потратил|заработал|"
    r"\bCPL\b|\bCPA\b|\bCPM\b|\bCPC\b|\bROI\b|\bROAS\b|\bДРР\b|\bCTR\b|\bLTV\b|"
    r"конверси|окупаем|выручк|в \d+[,.]?\d* раза|вырос(?:ло|ли)? на|"
    r"case study|revenue|I spent|I tested|benchmark|результат", re.I)

# признаки нужной темы по рубрикам
TOPIC = {
    "perf": re.compile(
        r"директ|яндекс\.?директ|рся|vk ads|vk рекламе|telegram ads|"
        r"маркетплейс|wildberries|озон|ozon|авито|контекст|таргет|"
        r"seo|geo|аукцион|ставк|креатив|лендинг|конверси|трафик|арбитраж|"
        r"google ads|meta ads|ppc|search console", re.I),
    "ai": re.compile(
        r"агент|multi-?agent|мультиагент|оркестр|mcp|скилл|skill|"
        r"n8n|make\.com|пайплайн|workflow|rag|харнесс|harness|"
        r"claude code|codex|cursor|субагент|sub-?agent|"
        r"автоматизирова|инструмент(?:ы|ов)? модел|tool call", re.I),
    "money": re.compile(
        r"заработал|выручка|mrr|arr|подписк|монетизац|клиент(?:ов)? на|"
        r"продал|окупил|доход|\$\d|получил \d+.*(?:₽|руб)|"
        r"услуг(?:а|у|и) на базе|ии[- ]агентств|запустил продукт", re.I),
}

# корпоративная отчётность и прогнозы рынка — фон, а не кейс
MACRO = re.compile(
    r"выручка (?:компании|группы|холдинга)|квартальн|отчётност|отчетност|"
    r"инвестиц(?:ии|ионный) раунд|привлек(?:ла|ли) \$|оценка компании|"
    r"по прогноз|аналитики ожидают|рынок вырастет|доля рынка соста|"
    r"акции (?:выросли|упали)|капитализац|\bIPO\b|"
    r"заключил[аи] (?:договор|сделк|контракт)|расходы .{0,30}превысил|"
    r"удвоил(?:ось|ся|ась)|выросл[оиа] на \d+% за (?:год|два)|"
    r"earnings|quarterly results|raised \$\d+[MB]|valuation", re.I)

# признак личного опыта — перевешивает макро-штраф
PERSONAL = re.compile(
    r"\bя\b|\bмы\b|\bмой\b|\bнаш|сделал|запустил|собрал|протестировал|"
    r"у меня|у нас|в моём|в нашем|\bI \b|\bwe \b|my |our ", re.I)

# академические источники
ACADEMIC = re.compile(r"arXiv|Daily Papers|Hugging Face", re.I)

# что делает академическую статью полезной: прикладная агентная тема
APPLIED = re.compile(
    r"\bagent(?:s|ic)?\b|multi-?agent|orchestrat|tool[- ]use|tool call|"
    r"\bRAG\b|marketing|advertis|harness|agent memory|agent eval", re.I)


# ВАУ-сигналы: то, ради чего дайджест вообще читают.
# Кратные изменения, аномалии, контринтуитивные выводы, провалы и странные ходы.
WOW_MULT = re.compile(
    r"в \d+([,.]\d+)? раз|[xх]\s?\d+|на \d{3,}\s?%|"
    r"с нуля до|с 0 до|до \d+ млн|\d+ млн (?:просмотр|показ|рублей|₽)|"
    r"\d+\s?(?:млрд|миллиард)|в \d+ раза дешевле|вдвое|втрое|"
    r"\d+x\b|grew \d+%|\d{3,}% (?:growth|increase|roi)", re.I)

WOW_ANOMALY = re.compile(
    r"неожиданн|оказалось(?: наоборот| что)?|вопреки|парадокс|"
    r"не сработал|провал|факап|облажал|сломал|уронил|слил бюджет|"
    r"миф[а-я]*\b|все делают неправильно|перестало работать|"
    r"впервые|никто (?:не делает|так не)|странн|абсурд|дичь|"
    r"counterintuitive|surprising|turns out|failed|backfired", re.I)

WOW_UNUSUAL = re.compile(
    r"нестандартн|необычн|хайп|виральн|завирусил|мем\b|"
    r"партизанск|гериллья|коллаборац|спецпроект|"
    r"guerrilla|stunt|viral|weird|bizarre", re.I)

# «напиши текст нейросетью» — то, что заказчик просил не приносить
AI_TOO_SIMPLE = re.compile(
    r"написа(?:ть|л) текст (?:с помощью|через)|сгенерирова(?:ть|л) картинк|"
    r"промпт для (?:чат|chat)?gpt|нейросеть напишет|"
    r"как писать промпты|шаблон промпта", re.I)


def fetch(url, timeout=25, retries=2):
    for a in range(retries + 1):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": UA})
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return r.read()
        except urllib.error.HTTPError as e:
            if e.code == 429:
                time.sleep(4 * (a + 1)); continue
            return b""
        except Exception:
            time.sleep(0.8)
    return b""


def clean(s):
    s = html.unescape(RE_TAG.sub(" ", (s or "").replace("<br/>", "\n").replace("<br>", "\n")))
    s = RE_WS.sub(" ", s)
    return re.sub(r"\n{3,}", "\n\n", s).strip()


def in_window(iso, day):
    if not iso:
        return False
    try:
        d = datetime.fromisoformat(iso.replace("Z", "+00:00")).astimezone(MSK)
    except ValueError:
        return False
    return d.date() == day


# ---------- TELEGRAM ------------------------------------------------
def harvest_telegram(ch, day):
    """Читает канал за конкретные сутки, при необходимости листает назад."""
    out, before, guard = [], None, 0
    while guard < 5:
        guard += 1
        url = f"https://t.me/s/{ch['u']}" + (f"?before={before}" if before else "")
        raw = fetch(url)
        if not raw:
            break
        h = raw.decode("utf-8", "ignore")
        blocks = RE_MSG_BLOCK.findall(h)
        if not blocks:
            break
        oldest = None
        got_older = False
        for b in blocks:
            pid = RE_DATA_POST.search(b)
            ts = RE_TIME.findall(b)
            tx = RE_TEXT.search(b)
            if not (pid and ts):
                continue
            num = int(pid.group(1).split("/")[-1])
            oldest = num if oldest is None else min(oldest, num)
            when = ts[-1]
            try:
                d = datetime.fromisoformat(when.replace("Z", "+00:00")).astimezone(MSK).date()
            except ValueError:
                continue
            if d < day:
                got_older = True
                continue
            if d != day:
                continue
            text = clean(tx.group(1)) if tx else ""
            if len(text) < 120:          # односложные реплики не несут кейса
                continue
            v = RE_VIEWS.search(b)
            out.append({
                "src": "tg", "src_name": ch.get("n", ch["u"]),
                "channel": ch["u"], "v": ch.get("v", 3), "f": ch.get("f", 3),
                "url": f"https://t.me/{pid.group(1)}",
                "ts": when, "title": text[:140], "text": text[:4000],
                "views": v.group(1).strip() if v else None,
            })
        if got_older or oldest is None:
            break
        before = oldest
        time.sleep(0.5)
    return out


# ---------- RSS -----------------------------------------------------
def harvest_rss(feed, day):
    raw = fetch(feed["url"])
    if not raw:
        return []
    try:
        root = ET.fromstring(raw)
    except ET.ParseError:
        return []
    ns = {"a": "http://www.w3.org/2005/Atom",
          "c": "http://purl.org/rss/1.0/modules/content/",
          "dc": "http://purl.org/dc/elements/1.1/"}
    items = root.findall(".//item") or root.findall(".//a:entry", ns)
    out = []
    for it in items:
        def g(*tags):
            for t in tags:
                e = it.find(t, ns) if ":" in t else it.find(t)
                if e is not None:
                    return (e.text or "").strip() or (e.get("href") or "")
            return ""
        title = clean(g("title", "a:title"))
        link = g("link", "a:link")
        if not link:
            e = it.find("a:link", ns)
            link = e.get("href") if e is not None else ""
        pub = g("pubDate", "published", "a:published", "a:updated", "dc:date")
        ts = parse_date(pub)
        if not in_window(ts, day):
            continue
        body = clean(g("description", "c:encoded", "a:summary", "a:content"))
        out.append({
            "src": "rss", "src_name": feed["n"], "v": feed.get("v", 3), "f": 2,
            "url": link, "ts": ts, "title": title, "text": body[:3000],
        })
    return out


def parse_date(s):
    if not s:
        return None
    s = s.strip()
    for f in ("%a, %d %b %Y %H:%M:%S %z", "%a, %d %b %Y %H:%M:%S %Z",
              "%Y-%m-%dT%H:%M:%S%z", "%Y-%m-%dT%H:%M:%SZ", "%Y-%m-%d %H:%M:%S"):
        try:
            d = datetime.strptime(s, f)
            if d.tzinfo is None:
                d = d.replace(tzinfo=timezone.utc)
            return d.isoformat()
        except ValueError:
            continue
    try:
        return datetime.fromisoformat(s.replace("Z", "+00:00")).isoformat()
    except ValueError:
        return None


# ---------- YOUTUBE -------------------------------------------------
def harvest_youtube(ch, day):
    raw = fetch(f"https://www.youtube.com/feeds/videos.xml?channel_id={ch['id']}")
    if not raw:
        return []
    try:
        root = ET.fromstring(raw)
    except ET.ParseError:
        return []
    ns = {"a": "http://www.w3.org/2005/Atom",
          "yt": "http://www.youtube.com/xml/schemas/2015",
          "m": "http://search.yahoo.com/mrss/"}
    out = []
    for e in root.findall("a:entry", ns):
        vid = e.findtext("yt:videoId", "", ns)
        pub = e.findtext("a:published", "", ns)
        if not in_window(parse_date(pub), day):
            continue
        title = clean(e.findtext("a:title", "", ns))
        group = e.find("m:group", ns)
        desc = clean(group.findtext("m:description", "", ns)) if group is not None else ""
        views = None
        if group is not None:
            st = group.find("m:community/m:statistics", ns)
            if st is not None:
                views = st.get("views")
        out.append({
            "src": "yt", "src_name": ch.get("n", ch["h"]), "v": ch.get("v", 3), "f": 3,
            "url": f"https://www.youtube.com/watch?v={vid}", "video_id": vid,
            "ts": parse_date(pub), "title": title, "text": desc[:1200],
            "views": views, "maybe_shorts": ch.get("shorts", False),
        })
    return out


# ---------- ПАРСЕРЫ HTML --------------------------------------------
def harvest_parser(site, day):
    """Сайты без RSS. Отдаёт только ссылки — дату и текст добирает модель
    при глубоком чтении, иначе пришлось бы качать каждую статью."""
    raw = fetch(site["url"])
    if not raw:
        return []
    h = raw.decode("utf-8", "ignore")
    sel = site.get("sel", "")
    m = re.match(r'a\[href\^="([^"]+)"\]', sel)
    pat = re.escape(m.group(1)) + r'[^"#?]*' if m else sel
    links, seen = [], set()
    for u in re.findall(r'href="(' + pat + r')"', h):
        u = u if u.startswith("http") else site["url"].split("/", 3)[0] + "//" + \
            site["url"].split("/")[2] + u
        if u in seen or u.rstrip("/") == site["url"].rstrip("/"):
            continue
        seen.add(u)
        links.append(u)
    return [{"src": "web", "src_name": site["n"], "v": site.get("v", 3), "f": 2,
             "url": u, "ts": None, "title": "", "text": "", "needs_fetch": True}
            for u in links[:25]]


# ---------- ДЕДУП И СКОРИНГ -----------------------------------------
def norm_key(rec):
    base = (rec.get("url") or "").split("?")[0].rstrip("/")
    t = re.sub(r"[^\wа-яё]+", "", (rec.get("title") or "").lower())[:80]
    return hashlib.md5((base + "|" + t).encode()).hexdigest()


def score(rec, cfg):
    blob = (rec.get("title", "") + " " + rec.get("text", ""))[:2500]
    if NOISE.search(blob):
        return 0, [], "noise", 0
    tags = [k for k, rx in TOPIC.items() if rx.search(blob)]
    if not tags:
        return 0, [], "noise", 0
    # чистая академия без прикладной части заказчику не нужна
    if ACADEMIC.search(rec.get("src_name", "")) and not APPLIED.search(blob):
        return 0, [], "noise", 0
    s = rec.get("v", 3) * 10
    if MACRO.search(blob) and not PERSONAL.search(blob):
        s -= 35          # отчётность корпораций и прогнозы рынка — это фон, а не кейс
    if rec.get("f", 0) >= 4:
        s *= cfg.get("small_author_bonus", 1.4)      # приоритет малым авторским
    hits = len(SIGNAL.findall(blob))
    s += min(hits, 8) * 6
    if "ai" in tags and AI_TOO_SIMPLE.search(blob):
        s -= 40                                       # «напиши текст нейросетью» — не нужно
    if rec["src"] == "tg" and len(rec.get("text", "")) > 900:
        s += 10                                       # длинный пост чаще всего разбор
    if rec["src"] == "yt" and rec.get("maybe_shorts"):
        s += 5                                        # вертикалки дешёвы в обработке

    # ВАУ-фактор считаем отдельно: он поднимает запись независимо от рубрики
    wow = 0
    if WOW_MULT.search(blob):
        wow += 2
    if WOW_ANOMALY.search(blob):
        wow += 2
    if WOW_UNUSUAL.search(blob):
        wow += 1
    s += wow * 12

    # разбор из личной практики или новость — модели это надо знать заранее
    kind = "case" if (PERSONAL.search(blob) and hits >= 2) else "news"
    if kind == "news" and wow < 2:
        s -= 15          # новость без вау-фактора действительно фон
    return round(s), tags, kind, min(wow, 5)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--sources", default="sources.yaml")
    ap.add_argument("--state", default="state")
    ap.add_argument("--out", default="out")
    ap.add_argument("--date", help="YYYY-MM-DD, по умолчанию вчера")
    ap.add_argument("--only", default="tg,rss,yt,web")
    ap.add_argument("--workers", type=int, default=8)
    a = ap.parse_args()

    import yaml
    src = yaml.safe_load(open(a.sources, encoding="utf-8"))
    cfg = src.get("meta", {})
    day = (datetime.fromisoformat(a.date).date() if a.date
           else (datetime.now(MSK) - timedelta(days=1)).date())
    only = set(a.only.split(","))
    os.makedirs(a.out, exist_ok=True)
    os.makedirs(a.state, exist_ok=True)

    jobs = []
    if "tg" in only:
        for k, v in src.items():
            if k.startswith("tg_"):
                jobs += [(harvest_telegram, c) for c in v if c.get("v", 3) >= cfg.get("min_value", 3)]
    slow_jobs = []
    if "rss" in only:
        for k, v in src.items():
            if k.startswith("rss_"):
                for f in v:
                    (slow_jobs if f.get("slow") else jobs).append((harvest_rss, f))
    if "yt" in only:
        for k, v in src.items():
            if k.startswith("yt_"):
                jobs += [(harvest_youtube, c) for c in v]
    if "web" in only:
        jobs += [(harvest_parser, s) for s in src.get("parsers", [])]

    print(f"[harvest] день {day}, источников: {len(jobs)}")
    recs, t0 = [], time.time()
    with cf.ThreadPoolExecutor(max_workers=a.workers) as ex:
        futs = {ex.submit(fn, item, day): item for fn, item in jobs}
        done = 0
        for fu in cf.as_completed(futs):
            done += 1
            if done % 50 == 0:
                print(f"  ...{done}/{len(jobs)}, найдено {len(recs)}")
            try:
                recs += fu.result() or []
            except Exception as e:
                print(f"  ! {futs[fu].get('n') or futs[fu].get('u')}: {e}", file=sys.stderr)

    # медленная полоса: reddit и подобные режут частоту, идём по одному
    if slow_jobs:
        print(f"[harvest] медленная полоса: {len(slow_jobs)} источников")
        for fn, item in slow_jobs:
            try:
                recs += fn(item, day) or []
            except Exception as e:
                print(f"  ! {item.get('n')}: {e}", file=sys.stderr)
            time.sleep(4)

    # дедуп внутри дня
    seen, uniq = set(), []
    for r in recs:
        k = norm_key(r)
        if k in seen:
            continue
        seen.add(k)
        r["key"] = k
        uniq.append(r)

    # дедуп против истории за 30 дней
    hist_path = os.path.join(a.state, "seen.json")
    hist = json.load(open(hist_path, encoding="utf-8")) if os.path.exists(hist_path) else {}
    cutoff = (datetime.now(MSK) - timedelta(days=30)).date().isoformat()
    hist = {k: d for k, d in hist.items() if d >= cutoff}
    fresh = [r for r in uniq if r["key"] not in hist]

    # скоринг
    scored = []
    for r in fresh:
        s, tags, kind, wow = score(r, cfg)
        if s <= 0 and not r.get("needs_fetch"):
            continue
        r["score"], r["tags"], r["kind"], r["wow"] = s, tags, kind, wow
        scored.append(r)
    scored.sort(key=lambda x: -x["score"])

    for r in scored:
        hist[r["key"]] = day.isoformat()
    json.dump(hist, open(hist_path, "w", encoding="utf-8"), ensure_ascii=False)

    # ссылки с сайтов-без-RSS идут отдельным индексом: у них нет ни даты,
    # ни текста, и в общем списке они вытеснили бы содержательные записи
    web_index = [r for r in scored if r.get("needs_fetch")]
    scored = [r for r in scored if not r.get("needs_fetch")]

    # потолок на источник: одна многотысячная лента не должна вытеснить всех
    per_src, capped = {}, []
    for r in scored:
        n = r["src_name"]
        if per_src.get(n, 0) >= 5:
            continue
        per_src[n] = per_src.get(n, 0) + 1
        capped.append(r)

    # для модели — только то, что нужно для ранжирования
    shortlist = [{
        "i": i, "src": r["src"], "from": r["src_name"], "score": r["score"],
        "tags": r["tags"], "kind": r["kind"], "wow": r["wow"], "url": r["url"],
        "title": r["title"][:200],
        "snippet": r.get("text", "")[:600],
        "views": r.get("views"),
    } for i, r in enumerate(capped[:200])]

    # Кандидаты в блок «радар»: всё с заметным вау-фактором, отсортировано.
    # Модель берёт отсюда то, что не пошло в глубокий разбор.
    radar = sorted(
        ({"src": r["src"], "from": r["src_name"], "wow": r["wow"], "tags": r["tags"],
          "url": r["url"], "title": r["title"][:160]}
         for r in capped if r["wow"] >= 2),
        key=lambda x: -x["wow"])[:40]

    stamp = day.isoformat()
    json.dump(scored, open(f"{a.out}/raw_{stamp}.json", "w", encoding="utf-8"),
              ensure_ascii=False, indent=1)
    json.dump(shortlist, open(f"{a.out}/shortlist_{stamp}.json", "w", encoding="utf-8"),
              ensure_ascii=False, indent=1)
    json.dump(web_index, open(f"{a.out}/webindex_{stamp}.json", "w", encoding="utf-8"),
              ensure_ascii=False, indent=1)
    json.dump(radar, open(f"{a.out}/radar_{stamp}.json", "w", encoding="utf-8"),
              ensure_ascii=False, indent=1)

    # ВАУ-КОПИЛКА. В отдельно взятых сутках сильных кейсов мало, поэтому
    # всё с wow>=3 складывается в пул и живёт там две недели. Выпуск берёт
    # оттуда, если своих не хватило. Использованное помечает агент.
    pool_path = os.path.join(a.state, "wow_pool.json")
    pool = json.load(open(pool_path, encoding="utf-8")) if os.path.exists(pool_path) else []
    have = {x["url"] for x in pool}
    for r in scored:
        if r["wow"] >= 3 and r["url"] not in have and not r.get("used"):
            pool.append({"url": r["url"], "from": r["src_name"], "wow": r["wow"],
                         "tags": r["tags"], "title": r["title"][:200],
                         "snippet": r.get("text", "")[:500],
                         "added": stamp, "used": False})
    keep = (datetime.now(MSK) - timedelta(days=14)).date().isoformat()
    pool = [x for x in pool if x["added"] >= keep and not x.get("used")]
    pool.sort(key=lambda x: (-x["wow"], x["added"]))
    json.dump(pool, open(pool_path, "w", encoding="utf-8"), ensure_ascii=False, indent=1)

    by_src = {}
    for r in scored:
        by_src[r["src"]] = by_src.get(r["src"], 0) + 1
    print(f"\n[harvest] за {time.time()-t0:.0f}с: сырьё {len(recs)} → уникальных {len(uniq)} "
          f"→ новых {len(fresh)} → прошло фильтр {len(scored)}")
    print(f"[harvest] по источникам: {by_src}")
    print(f"[harvest] модели уйдёт: {len(shortlist)} записей "
          f"(кейсов {sum(1 for x in shortlist if x['kind']=='case')}) "
          f"→ {a.out}/shortlist_{stamp}.json")
    print(f"[harvest] вау-кейсов (wow>=2): "
          f"{sum(1 for x in shortlist if x['wow']>=2)} в шортлисте, "
          f"{len(radar)} кандидатов в радар")
    print(f"[harvest] вау-копилка: {len(pool)} неиспользованных за 14 дней "
          f"→ {a.state}/wow_pool.json")
    print(f"[harvest] индекс сайтов без RSS: {len(web_index)} ссылок для глубокого чтения")


if __name__ == "__main__":
    main()
