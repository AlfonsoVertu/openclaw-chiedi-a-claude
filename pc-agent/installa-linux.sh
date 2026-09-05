#!/usr/bin/env bash
# Installer dell'agent SOWAI per Linux (Raspberry incluso).
#
# COS'E'. Il gemello di installa-windows.bat, ma per Linux headless: invece di
# un'app con finestra, l'agente vive come SERVIZIO systemd che riparte da solo
# al boot e a ogni crash - la forma giusta per una macchina senza schermo come
# il Raspberry. Il codice Python e' lo stesso di Windows (agent.py + comandi.py):
# solo libreria standard, gira uguale su ARM e x86.
#
# COSA FA. Copia l'agente in /opt/sowai-agent, lo accoppia al tenant col codice
# a 6 cifre, scrive un'unita' systemd che lo esegue come l'utente scelto, e la
# avvia. Da quel momento il telefono... pardon, il PC/Pi e' un device del tenant.
#
# USO:
#   sudo ./installa-linux.sh https://tuonome.workingwithweb.eu 123456
#   sudo ./installa-linux.sh https://tuonome.workingwithweb.eu 123456 --shell
#
# --shell accende SOWAI_AGENT_SHELL: l'agente diventa un "desktop commander"
# (esegue comandi, gestisce file e processi, lancia job). E' una scelta esplicita
# di chi installa - senza, l'agente fa solo osservazione e "chiedi a Claude".

set -euo pipefail

BASE="${1:-}"
CODICE="${2:-}"
SHELL_FLAG="${3:-}"

INSTALL_DIR="/opt/sowai-agent"
SERVIZIO="sowai-agent"
SRC_DIR="$(cd "$(dirname "$0")" && pwd)"

# --- chi deve far girare l'agente: l'utente che ha lanciato sudo, non root.
# Cosi' il file col token e lo stato vivono nella sua home, non in quella di root.
RUN_USER="${SUDO_USER:-$USER}"
RUN_HOME="$(getent passwd "$RUN_USER" | cut -d: -f6)"

rosso() { printf '\033[31m%s\033[0m\n' "$*"; }
verde() { printf '\033[32m%s\033[0m\n' "$*"; }

# --- controlli
if [ "$(id -u)" -ne 0 ]; then
    rosso "Lanciami con sudo:  sudo ./installa-linux.sh <URL> <codice> [--shell]"
    exit 1
fi
if ! command -v python3 >/dev/null 2>&1; then
    rosso "Serve python3 (nessun'altra dipendenza)."
    exit 1
fi
if [ -z "$BASE" ] || [ -z "$CODICE" ]; then
    read -r -p "Indirizzo del tuo SOWAI (https://...): " BASE
    read -r -p "Codice di accoppiamento (6 cifre): " CODICE
fi

# --shell e --net in qualunque posizione degli argomenti
PUO_ESEGUIRE=0
PUO_RETE=0
for a in "$@"; do
    [ "$a" = "--shell" ] && PUO_ESEGUIRE=1
    [ "$a" = "--net" ] && PUO_RETE=1
done

echo
echo "Installo l'agent SOWAI:"
echo "  utente servizio : $RUN_USER"
echo "  cartella        : $INSTALL_DIR"
echo "  tenant          : $BASE"
echo "  esecuzione shell: $([ "$PUO_ESEGUIRE" = 1 ] && echo 'SI (desktop commander)' || echo 'no (solo osserva + Claude)')"
echo

# --- copia del codice
mkdir -p "$INSTALL_DIR"
cp "$SRC_DIR/agent.py" "$SRC_DIR/comandi.py" "$INSTALL_DIR/"
chown -R "$RUN_USER":"$RUN_USER" "$INSTALL_DIR"

# --- sudo di rete (solo con --net): i tool che AGISCONO sulla rete (connessione
# isolata, monitor mode) vogliono CAP_NET_ADMIN. Invece di far girare tutto
# l'agent da root, diamo all'utente dell'agent un sudo passwordless MIRATO a
# pochi binari di rete. E' una concessione esplicita di chi installa.
if [ "$PUO_RETE" = 1 ]; then
    BINS=""
    for b in ip nmcli iw rfkill tcpdump timeout; do
        p="$(command -v "$b" 2>/dev/null)"; [ -n "$p" ] && BINS="$BINS, $p"
    done
    BINS="${BINS#, }"
    if [ -n "$BINS" ]; then
        echo "$RUN_USER ALL=(root) NOPASSWD: $BINS" > /etc/sudoers.d/sowai-agent-net
        chmod 440 /etc/sudoers.d/sowai-agent-net
        verde "  sudo di rete abilitato (connect isolato / monitor mode) per: $BINS"
    fi
fi

# --- accoppiamento COME l'utente del servizio (il token finisce nella sua home)
echo "Accoppiamento in corso..."
ENVSH=""
[ "$PUO_ESEGUIRE" = 1 ] && ENVSH="SOWAI_AGENT_SHELL=1"
if ! sudo -u "$RUN_USER" env $ENVSH python3 "$INSTALL_DIR/agent.py" accoppia "$BASE" "$CODICE"; then
    rosso "Accoppiamento fallito. Controlla indirizzo e codice (il codice dura 15 minuti)."
    exit 1
fi

# --- unita' systemd
UNIT="/etc/systemd/system/${SERVIZIO}.service"
cat > "$UNIT" <<UNITEOF
[Unit]
Description=SOWAI device agent
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=$RUN_USER
WorkingDirectory=$INSTALL_DIR
Environment=PYTHONUNBUFFERED=1
$([ "$PUO_ESEGUIRE" = 1 ] && echo "Environment=SOWAI_AGENT_SHELL=1")
ExecStart=/usr/bin/python3 $INSTALL_DIR/agent.py
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
UNITEOF

systemctl daemon-reload
systemctl enable --now "$SERVIZIO"

echo
verde "Fatto. L'agent SOWAI e' attivo e riparte al boot."
echo "  stato:  systemctl status $SERVIZIO"
echo "  log:    journalctl -u $SERVIZIO -f"
echo "  ferma:  sudo systemctl stop $SERVIZIO"
echo "  togli:  sudo systemctl disable --now $SERVIZIO && sudo rm $UNIT $INSTALL_DIR -rf"
