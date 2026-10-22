#!/bin/sh
# SSH-тунель NAS ↔ RAG-ПК для приватного RAG (база знань пошти, sevskiy-prod).
# Нічого не відкривається в інтернет: NAS сам підключається до RAG-ПК по SSH і прокидає два порти.
#
#   Minerva → RAG:  <шлюз Docker-мережі Minerva>:18789  →  RAG-ПК 127.0.0.1:8789
#   RAG → Minerva:  RAG-ПК 127.0.0.1:18081              →  NAS 127.0.0.1:8000
#
# Команди (від root, з теки Minerva на NAS):
#   sh rag_assistant/deploy/nas_rag_tunnel.sh setup    — ключ тунелю + рядок для RAG-ПК (один раз)
#   sh rag_assistant/deploy/nas_rag_tunnel.sh start    — запустити у фоні (те саме ставиться в Планувальник DSM)
#   sh rag_assistant/deploy/nas_rag_tunnel.sh status   — стан, перевірка й що вписати в Minerva
#   sh rag_assistant/deploy/nas_rag_tunnel.sh stop | restart
# Докладно: rag_assistant/deploy/README.md
set -u

RAG_HOST="${RAG_HOST:-192.168.2.68}"
RAG_USER="${RAG_USER:-user}"
RAG_PORT="${RAG_PORT:-8789}"                  # приватний RAG на RAG-ПК (слухає лише 127.0.0.1)
NAS_RAG_PORT="${NAS_RAG_PORT:-18789}"         # де Minerva бачить RAG
RAG_MINERVA_PORT="${RAG_MINERVA_PORT:-18081}" # де RAG бачить Minerva (127.0.0.1 на RAG-ПК)
MINERVA_PORT="${MINERVA_PORT:-8000}"          # Minerva на NAS (docker-compose: "8000:8000")
COMPOSE_PROJECT="${COMPOSE_PROJECT:-tabele_mvp}"
# Відбиток SSH-ключа RAG-ПК (ssh-keygen -lf C:\ProgramData\ssh\ssh_host_ed25519_key.pub); порожньо — не перевіряти
RAG_HOST_FP="${RAG_HOST_FP-SHA256:+TcuhMYmy8VGzB3GXm74CoMgbe8YzqsQDJwyAjSCqiM}"
DIR="${TUNNEL_DIR:-/volume1/docker/rag-tunnel}"   # поза репозиторієм: git його не чіпає

KEY="$DIR/id_ed25519"
KNOWN="$DIR/known_hosts"
PIDFILE="$DIR/tunnel.pid"
LOG="$DIR/tunnel.log"
SELF="$(cd "$(dirname "$0")" && pwd)/$(basename "$0")"
export PATH="/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin:$PATH"

log() { echo "$(date '+%Y-%m-%d %H:%M:%S') $*" >> "$LOG"; }

rotate() {
    if [ -f "$LOG" ] && [ "$(wc -c < "$LOG")" -gt 1048576 ]; then mv -f "$LOG" "$LOG.1"; fi
}

web_container() {
    if [ -n "${WEB_CONTAINER:-}" ]; then echo "$WEB_CONTAINER"; return; fi
    docker ps --filter "label=com.docker.compose.project=$COMPOSE_PROJECT" \
        --filter "label=com.docker.compose.service=web" --format '{{.Names}}' 2>/dev/null | head -n 1
}

gateway() {
    C="$(web_container)"
    [ -n "$C" ] || return 0
    docker inspect -f '{{range .NetworkSettings.Networks}}{{.Gateway}} {{end}}' "$C" 2>/dev/null | awk '{print $1}'
}

is_running() { [ -f "$PIDFILE" ] && kill -0 "$(cat "$PIDFILE")" 2>/dev/null; }

cmd_setup() {
    mkdir -p "$DIR" && chmod 700 "$DIR" || exit 1
    if [ ! -f "$KEY" ]; then
        ssh-keygen -q -t ed25519 -N "" -C "minerva-nas-rag-tunnel" -f "$KEY" || exit 1
    fi
    rm -f "$KNOWN.new"
    if command -v ssh-keyscan > /dev/null 2>&1; then
        ssh-keyscan -t ed25519 "$RAG_HOST" > "$KNOWN.new" 2>/dev/null
    else   # без ssh-keyscan: ключ хоста запам'ятовується до входу, сам вхід ще не дозволено
        ssh -i "$KEY" -o BatchMode=yes -o ConnectTimeout=15 -o StrictHostKeyChecking=accept-new \
            -o UserKnownHostsFile="$KNOWN.new" -o HostKeyAlgorithms=ssh-ed25519 "$RAG_USER@$RAG_HOST" true 2>/dev/null
    fi
    if [ ! -s "$KNOWN.new" ]; then
        rm -f "$KNOWN.new"
        echo "❌ RAG-ПК $RAG_HOST не відповідає по SSH (порт 22)."
        exit 1
    fi
    FP="$(ssh-keygen -lf "$KNOWN.new" | awk '{print $2; exit}')"
    if [ -n "$RAG_HOST_FP" ] && [ "$FP" != "$RAG_HOST_FP" ]; then
        rm -f "$KNOWN.new"
        echo "❌ Відбиток SSH-ключа $RAG_HOST ($FP) не збігається з очікуваним ($RAG_HOST_FP)."
        echo "   Це не той ПК або ключ змінився — перевірте на RAG-ПК: ssh-keygen -lf C:\\ProgramData\\ssh\\ssh_host_ed25519_key.pub"
        exit 1
    fi
    mv -f "$KNOWN.new" "$KNOWN"
    chmod 600 "$KEY" "$KNOWN"
    LINE="restrict,port-forwarding,command=\"exit\",permitopen=\"127.0.0.1:$RAG_PORT\",permitlisten=\"127.0.0.1:$RAG_MINERVA_PORT\" $(cat "$KEY.pub")"
    echo "✅ RAG-ПК $RAG_HOST відповідає, відбиток ключа збігається ($FP)."
    echo
    echo "Далі на RAG-ПК: PowerShell «від імені адміністратора», вставити ОДИН рядок:"
    echo
    printf '%s\n' "Add-Content -Encoding ascii -Path C:\\ProgramData\\ssh\\administrators_authorized_keys -Value @('', '$LINE')"
    echo
    echo "Цей ключ дозволяє лише два прокинуті порти — без входу в систему й без команд."
    echo "Потім тут: sh $SELF start"
}

cmd_run() {   # внутрішнє: цикл з'єднання, запускає start
    while :; do
        GW="$(gateway)"
        if [ -z "$GW" ]; then
            log "немає контейнера web проєкту $COMPOSE_PROJECT — чекаю"
            sleep 30
            continue
        fi
        log "з'єднання: Minerva $GW:$NAS_RAG_PORT → RAG, RAG 127.0.0.1:$RAG_MINERVA_PORT → NAS :$MINERVA_PORT"
        ssh -N -i "$KEY" -o UserKnownHostsFile="$KNOWN" -o StrictHostKeyChecking=yes -o BatchMode=yes \
            -o ExitOnForwardFailure=yes -o ServerAliveInterval=30 -o ServerAliveCountMax=3 -o ConnectTimeout=15 \
            -L "$GW:$NAS_RAG_PORT:127.0.0.1:$RAG_PORT" \
            -R "127.0.0.1:$RAG_MINERVA_PORT:127.0.0.1:$MINERVA_PORT" \
            "$RAG_USER@$RAG_HOST" >> "$LOG" 2>&1
        log "тунель закрився (код $?), повтор через 15 с"
        rotate
        sleep 15
    done
}

cmd_start() {
    [ -f "$KEY" ] && [ -f "$KNOWN" ] || { echo "Спершу: sh $SELF setup"; exit 1; }
    if is_running; then
        echo "Тунель уже працює (pid $(cat "$PIDFILE"))."
        return 0
    fi
    nohup sh "$SELF" run > /dev/null 2>&1 &
    echo $! > "$PIDFILE"
    echo "Тунель запущено (pid $!)."
    sleep 5
    cmd_status
}

cmd_stop() {
    if is_running; then
        kill "$(cat "$PIDFILE")" 2>/dev/null
        echo "Тунель зупинено."
    else
        echo "Тунель не працював."
    fi
    pkill -f "127.0.0.1:$RAG_MINERVA_PORT:127.0.0.1:$MINERVA_PORT" 2>/dev/null
    rm -f "$PIDFILE"
}

cmd_status() {
    if is_running; then echo "Тунель: ✅ працює (pid $(cat "$PIDFILE"))"; else echo "Тунель: ⛔ зупинено"; fi
    C="$(web_container)"
    GW="$(gateway)"
    if [ -z "$GW" ]; then
        echo "Контейнер Minerva (web) не знайдено — перевірте COMPOSE_PROJECT=$COMPOSE_PROJECT або задайте WEB_CONTAINER."
        return 1
    fi
    CODE="$(docker exec "$C" python -c "
import urllib.request, urllib.error
try:
    urllib.request.urlopen('http://$GW:$NAS_RAG_PORT/v2/capabilities', timeout=5)
    print(200)
except urllib.error.HTTPError as e:
    print(e.code)
except Exception as e:
    print(type(e).__name__)
" 2>/dev/null)"
    case "$CODE" in
        401) echo "Minerva → RAG: ✅ RAG відповідає (401 без ключа — так і має бути)" ;;
        *)   echo "Minerva → RAG: ❌ немає відповіді (${CODE:-?}) — див. журнал нижче" ;;
    esac
    echo
    echo "У Minerva → «🧠 База знань» → «Підключення RAG»:"
    echo "  Назва цього сервера для RAG: sevskiy-prod"
    echo "  Адреса RAG-ПК: $GW    Порт: $NAS_RAG_PORT    HTTPS: вимкнено"
    if [ -f "$LOG" ]; then
        echo
        echo "Журнал ($LOG):"
        tail -n 5 "$LOG"
    fi
}

case "${1:-}" in
    setup)   cmd_setup ;;
    start)   cmd_start ;;
    stop)    cmd_stop ;;
    restart) cmd_stop; sleep 2; cmd_start ;;
    status)  cmd_status ;;
    run)     cmd_run ;;
    *)       echo "Використання: sh $0 setup|start|stop|restart|status"; exit 2 ;;
esac
