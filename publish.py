#!/usr/bin/env python3
"""
Публикация дайджеста в Telegram-канал.

Токен и id канала берутся из окружения, а не из кода:
    export TG_BOT_TOKEN=...
    export TG_CHAT_ID=-100...
либо из файла .env рядом со скриптом.

    python3 publish.py --file digest.md
    echo "текст" | python3 publish.py
    python3 publish.py --test
"""

import argparse, json, os, re, sys, time
import urllib.request, urllib.parse, urllib.error

LIMIT = 4096          # жёсткий лимит Telegram на сообщение
SAFE = 3800           # режем с запасом: разметка добавляет длину


def load_env():
    p = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env")
    if os.path.exists(p):
        for line in open(p, encoding="utf-8"):
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                os.environ.setdefault(k.strip(), v.strip())


def api(method, payload, token):
    data = urllib.parse.urlencode(payload).encode()
    req = urllib.request.Request(
        f"https://api.telegram.org/bot{token}/{method}", data=data)
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            return json.load(r)
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8", "ignore")
        try:
            return json.loads(body)
        except json.JSONDecodeError:
            return {"ok": False, "description": f"HTTP {e.code}: {body[:200]}"}


def md_to_html(text):
    """Markdown из дайджеста → HTML, который понимает Telegram.
    Telegram поддерживает только b/i/u/s/a/code/pre/blockquote, без вложенных div."""
    t = text
    # экранируем до вставки тегов
    t = t.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
    # ссылки [текст](url)
    t = re.sub(r"\[([^\]]+)\]\((https?://[^\s)]+)\)", r'<a href="\2">\1</a>', t)
    # заголовки markdown → жирный
    t = re.sub(r"^#{1,6}\s*(.+)$", r"<b>\1</b>", t, flags=re.M)
    # **жирный** и *курсив*
    t = re.sub(r"\*\*([^*\n]+)\*\*", r"<b>\1</b>", t)
    t = re.sub(r"(?<!\*)\*([^*\n]+)\*(?!\*)", r"<i>\1</i>", t)
    t = re.sub(r"`([^`\n]+)`", r"<code>\1</code>", t)
    return t


def split_message(text, limit=SAFE):
    """Режет по границам абзацев, не разрывая тег и не ломая слово."""
    if len(text) <= limit:
        return [text]
    parts, buf = [], ""
    for block in text.split("\n\n"):
        candidate = (buf + "\n\n" + block) if buf else block
        if len(candidate) <= limit:
            buf = candidate
            continue
        if buf:
            parts.append(buf)
            buf = ""
        while len(block) > limit:
            cut = block.rfind("\n", 0, limit)
            if cut < limit // 2:
                cut = block.rfind(" ", 0, limit)
            if cut < limit // 2:
                cut = limit
            parts.append(block[:cut])
            block = block[cut:].lstrip()
        buf = block
    if buf:
        parts.append(buf)
    return parts


def send(text, token, chat, silent=False, preview=False):
    chunks = split_message(md_to_html(text))
    sent = []
    for i, c in enumerate(chunks):
        if len(chunks) > 1:
            c += f"\n\n<i>{i+1}/{len(chunks)}</i>"
        payload = {
            "chat_id": chat, "text": c, "parse_mode": "HTML",
            "disable_web_page_preview": "false" if preview else "true",
            "disable_notification": "true" if silent else "false",
        }
        r = api("sendMessage", payload, token)
        if not r.get("ok"):
            # частая причина — кривой HTML; шлём тем же текстом, но без разметки
            if "parse" in (r.get("description") or "").lower():
                payload.pop("parse_mode")
                payload["text"] = re.sub(r"<[^>]+>", "", c)
                r = api("sendMessage", payload, token)
        if not r.get("ok"):
            print(f"[publish] ошибка на части {i+1}: {r.get('description')}", file=sys.stderr)
            return sent, r
        sent.append(r["result"]["message_id"])
        if i < len(chunks) - 1:
            time.sleep(1.2)          # не упереться в лимит частоты
    return sent, {"ok": True}


def main():
    load_env()
    ap = argparse.ArgumentParser()
    ap.add_argument("--file")
    ap.add_argument("--token", default=os.environ.get("TG_BOT_TOKEN"))
    ap.add_argument("--chat", default=os.environ.get("TG_CHAT_ID"))
    ap.add_argument("--silent", action="store_true", help="без звука уведомления")
    ap.add_argument("--preview", action="store_true", help="показывать превью первой ссылки")
    ap.add_argument("--test", action="store_true")
    a = ap.parse_args()

    if not a.token or not a.chat:
        sys.exit("нет TG_BOT_TOKEN или TG_CHAT_ID — проверь .env")

    if a.test:
        me = api("getMe", {}, a.token)
        if not me.get("ok"):
            sys.exit(f"токен не принят: {me.get('description')}")
        info = api("getChat", {"chat_id": a.chat}, a.token)
        if not info.get("ok"):
            sys.exit(f"канал недоступен: {info.get('description')}")
        print(f"бот: @{me['result']['username']}")
        print(f"канал: {info['result'].get('title')} ({a.chat})")
        text = ("<b>Проверка связи</b>\n\nСборщик подключён к каналу. "
                "Первый дайджест придёт завтра в 09:00.")
        r = api("sendMessage", {"chat_id": a.chat, "text": text, "parse_mode": "HTML"}, a.token)
        print("отправка:", "ок" if r.get("ok") else r.get("description"))
        return

    text = open(a.file, encoding="utf-8").read() if a.file else sys.stdin.read()
    if not text.strip():
        sys.exit("пустой текст")
    ids, r = send(text, a.token, a.chat, a.silent, a.preview)
    if r.get("ok"):
        print(f"[publish] отправлено сообщений: {len(ids)} → {ids}")
    else:
        sys.exit(1)


if __name__ == "__main__":
    main()
