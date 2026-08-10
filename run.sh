#!/usr/bin/env bash
# Бутстрап ежедневного прогона. Запускается из свежего облачного окружения.
#
#   GIT_REPO=https://<токен>@github.com/<user>/digest-bot.git bash run.sh collect
#   ... агент читает out/shortlist_*.json, пишет digest.md ...
#   bash run.sh publish
#
# Состояние живёт в репозитории, поэтому дедуп за 30 дней и карантин
# переживают то, что контейнер каждый раз новый.

set -euo pipefail
STEP="${1:-collect}"
WORK="${WORK:-/home/claude/digest-bot}"
DATE="${DATE:-$(TZ=Europe/Moscow date -d yesterday +%F)}"

setup() {
  if [ ! -d "$WORK/.git" ]; then
    echo "[bootstrap] клонирую репозиторий"
    git clone --depth 1 "$GIT_REPO" "$WORK"
  else
    git -C "$WORK" pull --rebase --quiet || true
  fi
  # архив мог быть распакован во вложенную папку — находим, где реально лежат скрипты
  if [ ! -f "$WORK/harvest.py" ] && [ -f "$WORK/digest-bot/harvest.py" ]; then
    echo "[bootstrap] скрипты во вложенной папке, работаю оттуда"
    WORK="$WORK/digest-bot"
  fi
  cd "$WORK"
  git config user.email "noreply@anthropic.com"
  git config user.name "Claude"
  python3 -c "import yaml" 2>/dev/null || pip install pyyaml --break-system-packages -q
  command -v yt-dlp >/dev/null || pip install yt-dlp --break-system-packages -q
  # секреты не в репозитории, а в переменных окружения задачи
  printf 'TG_BOT_TOKEN=%s\nTG_CHAT_ID=%s\n' "$TG_BOT_TOKEN" "$TG_CHAT_ID" > .env
  chmod 600 .env
  python3 tgstate.py restore || echo "[warn] состояние из канала не поднялось"
}

commit_state() {
  cd "$WORK"
    python3 tgstate.py save || echo "[warn] состояние в канал не ушло"
  # коммитить надо из корня репозитория, а он может быть уровнем выше
  ROOT="$(git rev-parse --show-toplevel 2>/dev/null || echo "$WORK")"
  cd "$ROOT"
  git add -A 2>/dev/null || true
  if ! git diff --cached --quiet; then
    git commit -qm "state $(TZ=Europe/Moscow date +%F): прогон за $DATE"
    git push -q origin HEAD && echo "[bootstrap] состояние сохранено"
  else
    echo "[bootstrap] состояние не изменилось"
  fi
}

case "$STEP" in
  collect)
    setup
    echo "[1/2] ищу новые каналы"
    timeout 600 python3 discovery.py --sources sources.yaml --state state --run || \
      echo "[warn] discovery не доработал, продолжаю без него"
    echo "[2/2] собираю $DATE"
    python3 harvest.py --sources sources.yaml --state state --out out \
                       --date "$DATE" --workers 12
    commit_state
    echo
    echo "ГОТОВО. Шортлист: $WORK/out/shortlist_$DATE.json"
    echo "Полные тексты:   $WORK/out/raw_$DATE.json"
    echo "Ссылки без RSS:  $WORK/out/webindex_$DATE.json"
    echo "Кандидаты в базу: $WORK/state/to_review.json"
    ;;
  publish)
    cd "$WORK"
    F="${2:-digest.md}"
    python3 publish.py --file "$F"
    # сохраняем опубликованное в архив: история выпусков живёт в репозитории
    mkdir -p digests && cp "$F" "digests/$DATE.md"
    commit_state
    ;;
  transcript)
    # $2 = videoId. Двухступенчатая схема: обычная, потом обход бот-чека
    cd "$WORK"
    V="$2"
    yt-dlp --write-auto-sub --write-sub --sub-lang ru,en --skip-download \
           --sub-format vtt -o "/tmp/%(id)s.%(ext)s" \
           "https://www.youtube.com/watch?v=$V" >/dev/null 2>&1 || \
    yt-dlp --extractor-args "youtube:player_client=web_embedded" \
           --ignore-no-formats-error --write-auto-sub --write-sub \
           --sub-lang ru,en --skip-download --sub-format vtt \
           -o "/tmp/%(id)s.%(ext)s" "https://www.youtube.com/watch?v=$V" >/dev/null 2>&1 || true
    python3 - "$V" <<'PY'
import glob, re, sys
v = sys.argv[1]
files = glob.glob(f"/tmp/{v}*.vtt")
if not files:
    print("СУБТИТРОВ НЕТ"); raise SystemExit
seen, out = set(), []
for line in open(files[0], encoding="utf-8", errors="ignore"):
    line = line.strip()
    if not line or "-->" in line or line.startswith(("WEBVTT", "Kind:", "Language:")):
        continue
    line = re.sub(r"<[^>]+>", "", line)
    if line and line not in seen:
        seen.add(line); out.append(line)
print(" ".join(out))
PY
    ;;
  *)
    echo "неизвестный шаг: $STEP (нужно collect | publish | transcript)"; exit 1;;
esac
