#!/usr/bin/env python3
"""
Состояние сборщика в закреплённом сообщении канала.

Гит-прокси облачной задачи не пускает push, поэтому state/ переезжает туда,
куда доступ гарантированно есть — в сам телеграм-канал. Папка пакуется в
tar.gz, уходит документом, закрепляется. Следующий прогон читает закреп.

    python3 tgstate.py save       # state/ → канал
    python3 tgstate.py restore    # канал → state/
    python3 tgstate.py info       # что сейчас лежит в закрепе

Восстановление никогда не роняет прогон: нет закрепа, нет сети, битый
архив — печатаем причину и работаем с тем, что пришло из репозитория.
"""

import argparse, io, json, os, sys, tarfile, time
import urllib.request, urllib.parse, urllib.error

HERE = os.path.dirname(os.path.abspath(__file__))
STATE = os.path.join(HERE, "state")
ARCNAME = "digest-bot-state.tar.gz"
CAPTION = "служебное: состояние сборщика, не удалять"
# файлы, которые действительно нужны следующему прогону
KEEP = ("seen.json", "wow_pool.json", "discovery.json", "to_review.json")


def load_env():
    p = os.path.join(HERE, ".env")
    if os.path.exists(p):
        for line in open(p, encoding="utf-8"):
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                os.environ.setdefault(k.strip(), v.strip())


def creds():
    load_env()
    t, c = os.environ.get("TG_BOT_TOKEN"), os.environ.get("TG_CHAT_ID")
    if not t or not c:
        raise RuntimeError("нет TG_BOT_TOKEN или TG_CHAT_ID")
    return t, c


def api(method, token, payload=None, timeout=60):
    url = f"https://api.telegram.org/bot{token}/{method}"
    data = urllib.parse.urlencode(payload).encode() if payload else None
    try:
        with urllib.request.urlopen(urllib.request.Request(url, data=data), timeout=timeout) as r:
            return json.load(r)
    except urllib.error.HTTPError as e:
        try:
            return json.loads(e.read().decode("utf-8", "ignore"))
        except Exception:
            return {"ok": False, "description": f"HTTP {e.code}"}
    except Exception as e:
        return {"ok": False, "description": str(e)}


def send_document(token, chat, blob, filename, caption):
    """multipart/form-data руками: тянуть requests ради одного запроса не хочется"""
    b = b"----digestbot%d" % time.time_ns()
    parts = []
    for k, v in (("chat_id", chat), ("caption", caption), ("disable_notification", "true")):
        parts.append(b"--" + b + b"\r\nContent-Disposition: form-data; name=\"%s\"\r\n\r\n%s\r\n"
                     % (k.encode(), str(v).encode()))
    parts.append(b"--" + b + b"\r\nContent-Disposition: form-data; name=\"document\"; "
                 b"filename=\"%s\"\r\nContent-Type: application/gzip\r\n\r\n" % filename.encode()
                 + blob + b"\r\n")
    parts.append(b"--" + b + b"--\r\n")
    body = b"".join(parts)
    req = urllib.request.Request(
        f"https://api.telegram.org/bot{token}/sendDocument", data=body,
        headers={"Content-Type": "multipart/form-data; boundary=" + b.decode()})
    try:
        with urllib.request.urlopen(req, timeout=180) as r:
            return json.load(r)
    except urllib.error.HTTPError as e:
        try:
            return json.loads(e.read().decode("utf-8", "ignore"))
        except Exception:
            return {"ok": False, "description": f"HTTP {e.code}"}
    except Exception as e:
        return {"ok": False, "description": str(e)}


def pinned(token, chat):
    r = api("getChat", token, {"chat_id": chat})
    if not r.get("ok"):
        return None
    pm = (r.get("result") or {}).get("pinned_message")
    if not pm:
        return None
    doc = pm.get("document") or {}
    if doc.get("file_name") != ARCNAME:
        return None
    return {"message_id": pm["message_id"], "file_id": doc["file_id"],
            "size": doc.get("file_size", 0), "date": pm.get("date")}


def pack():
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        for name in KEEP:
            p = os.path.join(STATE, name)
            if os.path.exists(p):
                tar.add(p, arcname=name)
    return buf.getvalue()


def cmd_save():
    token, chat = creds()
    if not os.path.isdir(STATE):
        print("[tgstate] нечего сохранять: папки state нет"); return 1
    blob = pack()
    old = pinned(token, chat)

    r = send_document(token, chat, blob, ARCNAME, CAPTION)
    if not r.get("ok"):
        print("[tgstate] не сохранил:", r.get("description")); return 1
    mid = r["result"]["message_id"]

    p = api("pinChatMessage", token,
            {"chat_id": chat, "message_id": mid, "disable_notification": "true"})
    if not p.get("ok"):
        print("[tgstate] закрепить не вышло:", p.get("description"))
        api("deleteMessage", token, {"chat_id": chat, "message_id": mid})
        return 1

    # старое сначала откалываем, потом сносим: в ленте не должно копиться
    if old:
        api("unpinChatMessage", token, {"chat_id": chat, "message_id": old["message_id"]})
        api("deleteMessage", token, {"chat_id": chat, "message_id": old["message_id"]})

    print(f"[tgstate] состояние сохранено в закреп: {len(blob)/1024:.0f} КБ, "
          f"сообщение {mid}" + (f", старое {old['message_id']} удалено" if old else ""))
    return 0


def cmd_restore():
    try:
        token, chat = creds()
    except RuntimeError as e:
        print("[tgstate] пропускаю восстановление:", e); return 0

    meta = pinned(token, chat)
    if not meta:
        print("[tgstate] в закрепе состояния нет, работаю с тем, что в репозитории"); return 0

    g = api("getFile", token, {"file_id": meta["file_id"]})
    if not g.get("ok"):
        print("[tgstate] getFile не ответил:", g.get("description")); return 0
    path = g["result"]["file_path"]
    try:
        with urllib.request.urlopen(
                f"https://api.telegram.org/file/bot{token}/{path}", timeout=180) as r:
            blob = r.read()
        os.makedirs(STATE, exist_ok=True)
        got = []
        with tarfile.open(fileobj=io.BytesIO(blob), mode="r:gz") as tar:
            for m in tar.getmembers():
                # архив свой, но путь всё равно проверяем
                if not m.isfile() or "/" in m.name or m.name.startswith("."):
                    continue
                if m.name not in KEEP:
                    continue
                f = tar.extractfile(m)
                if not f:
                    continue
                with open(os.path.join(STATE, m.name), "wb") as out:
                    out.write(f.read())
                got.append(m.name)
    except Exception as e:
        print("[tgstate] архив не развернулся, работаю с репозиторием:", e); return 0

    age = ""
    if meta.get("date"):
        h = (time.time() - meta["date"]) / 3600
        age = f", возраст {h:.0f} ч" if h < 96 else f", возраст {h/24:.0f} дн"
    print(f"[tgstate] состояние восстановлено из закрепа: {', '.join(got)}{age}")
    return 0


def cmd_info():
    token, chat = creds()
    m = pinned(token, chat)
    if not m:
        print("[tgstate] закрепа с состоянием нет"); return 1
    when = time.strftime("%Y-%m-%d %H:%M UTC", time.gmtime(m["date"])) if m.get("date") else "?"
    print(f"[tgstate] сообщение {m['message_id']}, {m['size']/1024:.0f} КБ, от {when}")
    return 0


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("cmd", choices=["save", "restore", "info"])
    sys.exit({"save": cmd_save, "restore": cmd_restore, "info": cmd_info}[ap.parse_args().cmd]())
