# -*- coding: utf-8 -*-
"""Le azioni dell'agent PC: il "desktop commander" del computer.

Fa del PC una superficie pilotabile dal tenant, come Desktop Commander fa per
Claude in locale - ma attraverso sowai/fitness, con l'isolamento per token e il
freno observe/actuate del tenant sopra ogni azione che agisce.

DUE MODI DI ESEGUIRE UN COMANDO, ed e' la parte che conta:

  - `esegui`      bloccante, per cose brevi. Aspetti, ricevi stdout. Ma se il
                  comando dura piu' dell'attesa del tenant (pochi secondi), il
                  comando viene TAGLIATO. Va bene per `dir`, non per un'
                  installazione.
  - `esegui_job`  lancia in sottofondo e torna SUBITO con un id. Il comando
                  continua a girare. Poi `stato_job` dice se e' finito e
                  `output_job` restituisce cio' che ha prodotto finora.

E' lo stesso pattern degli strumenti SSH del gestionale: lancio il job, poi
verifico lo stato. E' l'unico modo di far girare qualcosa di lungo su un
canale che non puo' restare in attesa - un'installazione, un backup, un
build.

SICUREZZA. Tutto cio' che agisce - eseguire, scrivere un file, fermare un
processo - passa per il freno del tenant (mode=actuate) prima ancora di
arrivare qui. Questo file esegue solo cio' che il server gli manda, e il
server non manda azioni attuanti a un device in sola osservazione. In piu':
niente di tutto questo esiste se `SOWAI_AGENT_SHELL` non e' acceso - un PC che
esegue comandi da remoto e' una scelta esplicita di chi installa.
"""
import base64
import os
import platform
import re
import shutil
import subprocess
import tempfile
import threading
import time
import urllib.request
import uuid

_POSIX = os.name == "posix"

# I job in corso: id -> dizionario con processo, output accumulato, stato.
_JOB = {}
_JOB_LOCK = threading.Lock()
MAX_JOB = 20            # oltre, si buttano i piu' vecchi finiti
MAX_OUTPUT = 200000     # per job: oltre, si tiene la coda


# ------------------------------------------------------------- terminale

def esegui(args):
    """Comando bloccante, per cose brevi. Torna stdout/stderr/codice."""
    comando = args.get("comando")
    if not comando:
        raise ValueError("serve 'comando'")
    r = subprocess.run(comando, capture_output=True, text=True, timeout=int(args.get("timeout", 120)),
                       shell=True, encoding="utf-8", errors="replace",
                       cwd=args.get("cwd") or os.path.expanduser("~"))
    return {"stdout": (r.stdout or "")[:MAX_OUTPUT],
            "stderr": (r.stderr or "")[:20000],
            "codice": r.returncode}


def _pulisci_job():
    """Toglie i job finiti piu' vecchi se sono troppi. Un desktop commander
    lanciato a lungo accumulerebbe processi zombie di stato."""
    with _JOB_LOCK:
        if len(_JOB) <= MAX_JOB:
            return
        finiti = sorted((j for j in _JOB.values() if j["stato"] != "in_corso"),
                        key=lambda j: j["fine"] or 0)
        for j in finiti[:len(_JOB) - MAX_JOB]:
            _JOB.pop(j["id"], None)


def esegui_job(args):
    """Lancia in sottofondo. Torna subito con l'id. Il comando continua."""
    comando = args.get("comando")
    if not comando:
        raise ValueError("serve 'comando'")
    jid = uuid.uuid4().hex[:12]
    job = {"id": jid, "comando": comando, "stato": "in_corso",
           "output": "", "codice": None, "inizio": time.time(), "fine": None}

    def lavora():
        try:
            p = subprocess.Popen(comando, shell=True, stdout=subprocess.PIPE,
                                 stderr=subprocess.STDOUT, text=True,
                                 encoding="utf-8", errors="replace", bufsize=1,
                                 cwd=args.get("cwd") or os.path.expanduser("~"))
            job["_pid"] = p.pid
            for riga in p.stdout:
                with _JOB_LOCK:
                    job["output"] += riga
                    if len(job["output"]) > MAX_OUTPUT:
                        job["output"] = job["output"][-MAX_OUTPUT:]
            p.wait()
            job["codice"] = p.returncode
            job["stato"] = "finito" if p.returncode == 0 else "errore"
        except Exception as e:
            job["output"] += "\n[agent] %s" % str(e)[:200]
            job["stato"] = "errore"
        finally:
            job["fine"] = time.time()

    with _JOB_LOCK:
        _JOB[jid] = job
    threading.Thread(target=lavora, daemon=True).start()
    _pulisci_job()
    return {"job_id": jid, "stato": "in_corso"}


def stato_job(args):
    jid = args.get("job_id")
    with _JOB_LOCK:
        j = _JOB.get(jid)
    if not j:
        raise ValueError("job non trovato: %s" % jid)
    return {"job_id": jid, "stato": j["stato"], "codice": j["codice"],
            "secondi": round((j["fine"] or time.time()) - j["inizio"], 1),
            "byte_output": len(j["output"])}


def output_job(args):
    """L'output del job da un certo punto in poi, per seguirlo senza rileggere
    tutto. `da` e' l'offset in byte restituito la volta prima."""
    jid = args.get("job_id")
    da = int(args.get("da", 0))
    with _JOB_LOCK:
        j = _JOB.get(jid)
    if not j:
        raise ValueError("job non trovato: %s" % jid)
    testo = j["output"][da:]
    return {"job_id": jid, "stato": j["stato"], "output": testo,
            "prossimo_da": da + len(testo)}


def ferma_job(args):
    jid = args.get("job_id")
    with _JOB_LOCK:
        j = _JOB.get(jid)
    if not j:
        raise ValueError("job non trovato: %s" % jid)
    pid = j.get("_pid")
    if pid:
        try:
            if os.name == "nt":
                subprocess.run(["taskkill", "/F", "/T", "/PID", str(pid)], capture_output=True)
            else:
                os.kill(pid, 9)
        except Exception:
            pass
    j["stato"] = "fermato"
    return {"job_id": jid, "stato": "fermato"}


# ------------------------------------------------------------- file

def leggi_file(args):
    p = args.get("percorso")
    if not p:
        raise ValueError("serve 'percorso'")
    with open(os.path.expanduser(p), encoding="utf-8", errors="replace") as f:
        contenuto = f.read(int(args.get("max_byte", 100000)))
    return {"percorso": p, "contenuto": contenuto}


def scrivi_file(args):
    p = args.get("percorso")
    if not p or "contenuto" not in args:
        raise ValueError("servono 'percorso' e 'contenuto'")
    p = os.path.expanduser(p)
    os.makedirs(os.path.dirname(p) or ".", exist_ok=True)
    with open(p, "w", encoding="utf-8") as f:
        f.write(args["contenuto"])
    return {"percorso": p, "byte": len(args["contenuto"])}


def lista_cartella(args):
    p = os.path.expanduser(args.get("percorso") or os.path.expanduser("~"))
    voci = []
    for nome in sorted(os.listdir(p))[:int(args.get("max", 300))]:
        pieno = os.path.join(p, nome)
        try:
            voci.append({"nome": nome, "cartella": os.path.isdir(pieno),
                         "byte": os.path.getsize(pieno) if os.path.isfile(pieno) else None})
        except Exception:
            voci.append({"nome": nome})
    return {"percorso": p, "voci": voci}


# ------------------------------------------------------------- processi

def lista_processi(args):
    filtro = (args.get("filtro") or "").lower()
    fuori = []
    if os.name == "nt":
        r = subprocess.run(["tasklist", "/fo", "csv", "/nh"], capture_output=True, text=True, timeout=30)
        for riga in (r.stdout or "").splitlines():
            campi = [c.strip('"') for c in riga.split('","')]
            if len(campi) >= 2 and (not filtro or filtro in campi[0].lower()):
                fuori.append({"nome": campi[0].strip('"'), "pid": campi[1]})
    else:
        r = subprocess.run(["ps", "-eo", "pid,comm"], capture_output=True, text=True, timeout=30)
        for riga in (r.stdout or "").splitlines()[1:]:
            parti = riga.split(None, 1)
            if len(parti) == 2 and (not filtro or filtro in parti[1].lower()):
                fuori.append({"pid": parti[0], "nome": parti[1]})
    return {"processi": fuori[:int(args.get("max", 200))]}


def ferma_processo(args):
    pid = args.get("pid")
    if not pid:
        raise ValueError("serve 'pid'")
    if os.name == "nt":
        subprocess.run(["taskkill", "/F", "/PID", str(pid)], capture_output=True)
    else:
        os.kill(int(pid), 9)
    return {"pid": pid, "fermato": True}


# ------------------------------------------------------------- stato

def stato_pc(args=None):
    return {"sistema": platform.platform(), "hostname": platform.node(),
            "processore": platform.processor(),
            "utente": os.environ.get("USERNAME") or os.environ.get("USER"),
            "home": os.path.expanduser("~")}


# ============================================================================
# RETE & SCOPERTA DISPOSITIVI
#
# Fa del device un nodo che scopre e aggancia camere e domotica - anche su reti
# diverse da quella dove vive (vedi il caso della camera raggiunta dal Pi su
# un'altra wifi). Stesso codice Windows+Linux, con un branch per i comandi che
# cambiano (nmcli vs netsh, ip vs netsh). Ogni tool degrada con un messaggio
# chiaro se lo strumento che gli serve non c'e' (ffmpeg per lo snapshot, il
# servizio wifi, ecc.) - cosi' non promette cio' che non puo' fare.
# ============================================================================

def wifi_scan(args=None):
    """Le reti wifi intorno: SSID, canale, segnale, sicurezza."""
    args = args or {}
    reti = []
    if _POSIX:
        if not shutil.which("nmcli"):
            raise RuntimeError("nmcli non disponibile su questo host")
        r = subprocess.run(["nmcli", "-t", "-f", "SSID,CHAN,SIGNAL,SECURITY",
                            "dev", "wifi", "list", "--rescan", "yes"],
                           capture_output=True, text=True, timeout=40)
        for riga in (r.stdout or "").splitlines():
            # terse: campi separati da ':' non-escaped
            parti = [p.replace("\\:", ":") for p in re.split(r"(?<!\\):", riga)]
            if len(parti) >= 4 and parti[0]:
                reti.append({"ssid": parti[0], "canale": parti[1],
                             "segnale": parti[2], "sicurezza": parti[3]})
    else:
        r = subprocess.run(["netsh", "wlan", "show", "networks", "mode=bssid"],
                           capture_output=True, text=True, timeout=40)
        cur = None
        for riga in (r.stdout or "").splitlines():
            s = riga.strip()
            m = re.match(r"SSID\s+\d+\s*:\s*(.*)", s)
            if m:
                if cur:
                    reti.append(cur)
                cur = {"ssid": m.group(1).strip(), "canale": None,
                       "segnale": None, "sicurezza": None}
            elif cur:
                low = s.lower()
                if low.startswith("authentication"):
                    cur["sicurezza"] = s.split(":", 1)[1].strip()
                elif low.startswith("signal"):
                    cur["segnale"] = s.split(":", 1)[1].strip()
                elif low.startswith("channel"):
                    cur["canale"] = s.split(":", 1)[1].strip()
        if cur:
            reti.append(cur)
    return {"reti": reti}


def _ip_iface(iface):
    """L'IPv4 di un'interfaccia, per dedurne la subnet."""
    if _POSIX:
        r = subprocess.run(["ip", "-4", "-o", "addr", "show", "dev", iface],
                           capture_output=True, text=True, timeout=10)
    else:
        r = subprocess.run(["netsh", "interface", "ip", "show", "addresses", iface],
                           capture_output=True, text=True, timeout=10)
    m = re.search(r"(\d+\.\d+\.\d+\.\d+)", r.stdout or "")
    return m.group(1) if m else None


def rete_scansiona(args):
    """Sweep ping+ARP di una subnet -> host vivi (ip, mac). Bindabile a
    un'interfaccia: obbligatorio quando due reti condividono la stessa subnet."""
    args = args or {}
    iface = args.get("iface")
    subnet = args.get("subnet")
    srcip = _ip_iface(iface) if (iface and not subnet) else None
    if srcip and not subnet:
        subnet = srcip.rsplit(".", 1)[0]
    if not subnet:
        raise ValueError("serve 'iface' oppure 'subnet' (es. '192.168.1')")

    vivi = {}
    lock = threading.Lock()
    sem = threading.Semaphore(50)   # non 254 ping insieme

    def pinga(ip):
        with sem:
            if _POSIX:
                cmd = ["ping", "-c", "1", "-W", "1"] + (["-I", iface] if iface else []) + [ip]
            else:
                cmd = ["ping", "-n", "1", "-w", "1000"] + (["-S", srcip] if srcip else []) + [ip]
            try:
                ok = subprocess.run(cmd, capture_output=True, timeout=4).returncode == 0
            except Exception:
                ok = False
            if ok:
                with lock:
                    vivi[ip] = None

    th = []
    for n in range(1, 255):
        t = threading.Thread(target=pinga, args=("%s.%d" % (subnet, n),))
        t.start()
        th.append(t)
    for t in th:
        t.join(timeout=8)

    # MAC dalla tabella ARP
    try:
        arp = subprocess.run(["arp", "-a"], capture_output=True, text=True, timeout=10).stdout or ""
        for ip in list(vivi):
            blocco = re.search(re.escape(ip) + r"[^\n]*", arp)
            if blocco:
                mac = re.search(r"([0-9a-fA-F]{2}[:-]){5}[0-9a-fA-F]{2}", blocco.group(0))
                if mac:
                    vivi[ip] = mac.group(0).lower().replace("-", ":")
    except Exception:
        pass

    host = [{"ip": ip, "mac": vivi[ip]}
            for ip in sorted(vivi, key=lambda x: int(x.split(".")[-1]))]
    return {"subnet": subnet + ".0/24", "vivi": len(host), "host": host}


_SOAP_DEVINFO = (
    '<?xml version="1.0" encoding="UTF-8"?>'
    '<s:Envelope xmlns:s="http://www.w3.org/2003/05/soap-envelope">'
    '<s:Body xmlns:tds="http://www.onvif.org/ver10/device/wsdl">'
    '<tds:GetDeviceInformation/></s:Body></s:Envelope>'
)


def _onvif_post(url, body, timeout=6):
    req = urllib.request.Request(
        url, data=body.encode("utf-8"), method="POST",
        headers={"Content-Type": "application/soap+xml; charset=utf-8",
                 "User-Agent": "SOWAI-Agent/1.0"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read().decode("utf-8", "replace")


def onvif_info(args):
    """GetDeviceInformation su un IP: marca, modello, firmware, seriale.
    Prova le porte ONVIF comuni; molte camere rispondono senza auth SOAP."""
    ip = (args or {}).get("ip")
    if not ip:
        raise ValueError("serve 'ip'")
    porte = args.get("porte") or [80, 8080, 2020, 8000]
    ultimo = None
    for p in porte:
        try:
            xml = _onvif_post("http://%s:%s/onvif/device_service" % (ip, p), _SOAP_DEVINFO)

            def g(tag):
                m = re.search(r"<[^>]*%s[^>]*>([^<]*)</[^>]*%s>" % (tag, tag), xml)
                return m.group(1) if m else None

            info = {"Manufacturer": g("Manufacturer"), "Model": g("Model"),
                    "FirmwareVersion": g("FirmwareVersion"), "SerialNumber": g("SerialNumber")}
            if any(info.values()):
                return {"ip": ip, "porta": p, "onvif": True, **info}
        except Exception as e:
            ultimo = str(e)[:120]
    return {"ip": ip, "onvif": False,
            "nota": "nessuna risposta ONVIF sulle porte %s" % porte, "errore": ultimo}


def onvif_trova(args):
    """Trova le camere ONVIF su una subnet (o su una lista di IP)."""
    args = args or {}
    ips = args.get("ips")
    if not ips:
        ips = [h["ip"] for h in rete_scansiona(
            {"subnet": args.get("subnet"), "iface": args.get("iface")})["host"]]
    trovate = [i for i in (onvif_info({"ip": ip}) for ip in ips) if i.get("onvif")]
    return {"camere": trovate, "controllati": len(ips)}


def camera_snapshot(args):
    """Cattura un frame da uno stream RTSP con ffmpeg -> immagine JPEG base64.
    Accetta 'rtsp_url' oppure 'ip'(+path/user/pass)."""
    args = args or {}
    url = args.get("rtsp_url")
    if not url:
        ip = args.get("ip")
        if not ip:
            raise ValueError("serve 'rtsp_url' oppure 'ip'")
        path = args.get("path", "/11")
        u, pw = args.get("user"), (args.get("pass") or args.get("password"))
        auth = ("%s:%s@" % (u, pw)) if u else ""
        url = "rtsp://%s%s%s" % (auth, ip, path if path.startswith("/") else "/" + path)
    ff = shutil.which("ffmpeg")
    if not ff:
        raise RuntimeError("ffmpeg non installato: 'camera_snapshot' non disponibile qui")
    out = os.path.join(tempfile.gettempdir(), "snap_%s.jpg" % uuid.uuid4().hex[:8])
    try:
        r = subprocess.run([ff, "-y", "-rtsp_transport", "tcp", "-i", url,
                            "-frames:v", "1", "-q:v", "3", out],
                           capture_output=True, timeout=int(args.get("timeout", 25)))
        if not os.path.exists(out) or os.path.getsize(out) == 0:
            coda = (r.stderr or b"").decode("utf-8", "replace")[-400:]
            raise RuntimeError(coda or "ffmpeg non ha prodotto un frame")
        with open(out, "rb") as f:
            data = f.read()
        return {"mime": "image/jpeg", "byte": len(data),
                "immagine_base64": base64.b64encode(data).decode()}
    finally:
        try:
            os.remove(out)
        except Exception:
            pass


def _priv(cmd):
    """Antepone 'sudo -n' se non siamo root. I comandi di rete che agiscono
    (ip rule, nmcli con up, iw set monitor) vogliono CAP_NET_ADMIN; l'agent gira
    come utente normale, quindi passano per sudo passwordless - che l'installer
    configura solo con --net (una scelta esplicita di chi installa). Se non c'e',
    il comando fallisce con un errore chiaro invece di chiedere una password che
    nessuno puo' digitare."""
    if _POSIX and hasattr(os, "geteuid") and os.geteuid() != 0:
        return ["sudo", "-n"] + cmd
    return cmd


def wifi_segnale(args=None):
    """Qualita' del link wifi attivo: RSSI, rate, SSID. Sola lettura."""
    args = args or {}
    iface = args.get("iface") or ("wlan0" if _POSIX else None)
    if _POSIX:
        info = {}
        if shutil.which("iw") and iface:
            t = subprocess.run(["iw", "dev", iface, "link"],
                               capture_output=True, text=True, timeout=10).stdout or ""
            m = re.search(r"SSID:\s*(.+)", t); info["ssid"] = m.group(1).strip() if m else None
            m = re.search(r"signal:\s*(-?\d+)\s*dBm", t); info["rssi_dbm"] = int(m.group(1)) if m else None
            m = re.search(r"tx bitrate:\s*([\d.]+)\s*MBit", t); info["tx_mbps"] = float(m.group(1)) if m else None
            m = re.search(r"freq:\s*(\d+)", t); info["freq_mhz"] = int(m.group(1)) if m else None
        if not any(info.values()) and shutil.which("nmcli"):
            r = subprocess.run(["nmcli", "-t", "-f", "ACTIVE,SSID,SIGNAL,RATE", "dev", "wifi"],
                               capture_output=True, text=True, timeout=10)
            for riga in (r.stdout or "").splitlines():
                p = [x.replace("\\:", ":") for x in re.split(r"(?<!\\):", riga)]
                if p and p[0] == "yes":
                    info = {"ssid": p[1], "segnale_pct": p[2], "rate": p[3]}
                    break
        if not any(info.values()):
            raise RuntimeError("nessun link wifi attivo su %s (o iw/nmcli assenti)" % iface)
        info["iface"] = iface
        return info
    t = subprocess.run(["netsh", "wlan", "show", "interfaces"],
                       capture_output=True, text=True, timeout=10).stdout or ""
    out = {}
    for k, lab in [("SSID", "ssid"), ("Signal", "segnale"), ("Radio type", "radio"),
                   ("Channel", "canale"), ("Receive rate", "rx_mbps"), ("Transmit rate", "tx_mbps")]:
        m = re.search(r"^\s*%s\s*:\s*(.+)$" % re.escape(k), t, re.M)
        if m:
            out[lab] = m.group(1).strip()
    if not out:
        raise RuntimeError("nessuna interfaccia wifi attiva")
    return out


def ping_qualita(args):
    """Misura stabilita' di un host: disponibilita', loss, latenza, jitter.
    Campioni SEQUENZIALI (il jitter e' la differenza fra latenze consecutive).
    Bindabile a un'interfaccia. Per campagne lunghe usare esegui_job."""
    args = args or {}
    ip = args.get("ip")
    if not ip:
        raise ValueError("serve 'ip'")
    n = int(args.get("conteggio", 15))
    iface = args.get("iface")
    lat = []
    ok = 0
    for _ in range(n):
        if _POSIX:
            cmd = ["ping", "-c", "1", "-W", "1"] + (["-I", iface] if iface else []) + [ip]
        else:
            cmd = ["ping", "-n", "1", "-w", "1000", ip]
        try:
            r = subprocess.run(cmd, capture_output=True, text=True, timeout=4)
            if r.returncode == 0:
                ok += 1
                m = re.search(r"time[=<]([\d.]+)\s*ms", r.stdout)
                if m:
                    lat.append(float(m.group(1)))
        except Exception:
            pass
    persi = n - ok
    avg = sum(lat) / len(lat) if lat else None
    jit = None
    if len(lat) >= 2:
        diffs = [abs(lat[i] - lat[i - 1]) for i in range(1, len(lat))]
        jit = round(sum(diffs) / len(diffs), 1)
    return {"ip": ip, "campioni": n, "ok": ok, "persi": persi,
            "disponibilita_pct": round(ok * 100.0 / n, 1),
            "loss_pct": round(persi * 100.0 / n, 1),
            "latenza_media_ms": round(avg, 1) if avg is not None else None,
            "latenza_max_ms": round(max(lat), 1) if lat else None,
            "jitter_ms": jit}


def wifi_connetti_isolato(args):
    """Si connette a una rete wifi SENZA toccare la rotta principale: profilo
    never-default + policy routing su tabella separata, e ROLLBACK automatico se
    la tabella main cambia anche di una riga. E' cosi' che il device raggiunge
    una seconda rete (es. la wifi di una camera) senza mai tagliare il proprio
    canale di controllo (SSH/polling verso il tenant). Solo Linux."""
    if not _POSIX:
        raise RuntimeError("connessione isolata: solo Linux (ip rule/policy routing)")
    args = args or {}
    ssid = args.get("ssid")
    if not ssid:
        raise ValueError("serve 'ssid'")
    pw = args.get("password") or args.get("pass")
    iface = args.get("iface", "wlan0")
    nome = args.get("con_name", "sowai-iso")
    tab = str(args.get("table", 100))

    def run(cmd, t=45):
        return subprocess.run(_priv(cmd), capture_output=True, text=True, timeout=t)

    def main_txt():
        return subprocess.run(["ip", "route", "show", "table", "main"],
                              capture_output=True, text=True, timeout=10).stdout or ""

    prima = main_txt()
    run(["nmcli", "con", "delete", nome])   # idempotente
    add = ["nmcli", "con", "add", "type", "wifi", "ifname", iface, "con-name", nome,
           "ssid", ssid, "ipv4.never-default", "yes", "ipv4.route-table", tab,
           "connection.autoconnect", "no", "ipv6.method", "disabled"]
    if pw:
        add += ["wifi-sec.key-mgmt", "wpa-psk", "wifi-sec.psk", pw]
    r = run(add)
    if r.returncode != 0:
        raise RuntimeError("add profilo fallito: %s" % ((r.stderr or r.stdout) or "")[:300])
    r = run(["nmcli", "con", "up", nome], t=50)
    if r.returncode != 0:
        run(["nmcli", "con", "delete", nome])
        raise RuntimeError("connessione fallita: %s" % ((r.stderr or r.stdout) or "")[:300])
    time.sleep(2)
    ipr = subprocess.run(["ip", "-4", "-o", "addr", "show", "dev", iface],
                         capture_output=True, text=True, timeout=10).stdout or ""
    m = re.search(r"inet (\d+\.\d+\.\d+\.\d+)", ipr)
    ip = m.group(1) if m else None
    if not ip:
        run(["nmcli", "con", "down", nome]); run(["nmcli", "con", "delete", nome])
        raise RuntimeError("nessun IP dal DHCP della rete")
    run(["ip", "rule", "add", "from", ip, "table", tab])
    if main_txt().strip() != prima.strip():
        run(["ip", "rule", "del", "from", ip, "table", tab])
        run(["nmcli", "con", "down", nome]); run(["nmcli", "con", "delete", nome])
        raise RuntimeError("ROLLBACK: la tabella main e' cambiata - connessione annullata per sicurezza")
    return {"ok": True, "con_name": nome, "iface": iface, "ip": ip, "table": int(tab),
            "nota": "rete isolata: never-default + policy routing, canale di controllo intatto"}


def wifi_disconnetti(args):
    """Smonta la connessione isolata e la sua regola di routing."""
    if not _POSIX:
        raise RuntimeError("solo Linux")
    args = args or {}
    nome = args.get("con_name", "sowai-iso")

    def run(cmd):
        return subprocess.run(_priv(cmd), capture_output=True, text=True, timeout=30)

    ip = args.get("ip")
    if ip:
        run(["ip", "rule", "del", "from", ip, "table", str(args.get("table", 100))])
    run(["nmcli", "con", "down", nome])
    r = run(["nmcli", "con", "delete", nome])
    return {"ok": True, "con_name": nome, "rimosso": r.returncode == 0}


def monitor_deauth(args):
    """Mette un'interfaccia LIBERA (un dongle, non quella connessa) in monitor
    mode e conta i frame di deauth/disassoc: distingue un link che cade per
    segnale/alimentazione da uno colpito da deauth. Solo Linux, serve tcpdump."""
    if not _POSIX:
        raise RuntimeError("monitor mode: solo Linux")
    args = args or {}
    iface = args.get("iface", "wlan1")
    secondi = int(args.get("secondi", 20))
    if not shutil.which("tcpdump") or not shutil.which("iw"):
        raise RuntimeError("servono tcpdump e iw: 'monitor_deauth' non disponibile qui")

    def run(cmd, t=30):
        return subprocess.run(_priv(cmd), capture_output=True, text=True, timeout=t)

    run(["ip", "link", "set", iface, "down"])
    r = run(["iw", iface, "set", "monitor", "control"])
    run(["ip", "link", "set", iface, "up"])
    if r.returncode != 0:
        raise RuntimeError("impossibile mettere %s in monitor mode: %s" % (iface, (r.stderr or "")[:200]))
    try:
        cmd = _priv(["timeout", str(secondi), "tcpdump", "-i", iface, "-e", "-s", "256",
                     "type mgt subtype deauth or type mgt subtype disassoc"])
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=secondi + 15)
        righe = [l for l in (r.stdout or "").splitlines()
                 if "eauth" in l or "isassoc" in l.lower()]
        return {"iface": iface, "secondi": secondi,
                "frame_deauth_disassoc": len(righe), "campione": righe[:8]}
    finally:
        run(["ip", "link", "set", iface, "down"])
        run(["iw", iface, "set", "type", "managed"])
        run(["ip", "link", "set", iface, "up"])


# ------------------------------------------------------------- bluetooth
# Scoperta e dialogo BLE via bluetoothctl (BlueZ, standard su Raspberry Pi OS).
# Stesso contratto dell'agent Android: bt_scan / bt_accendi / bt_servizi /
# bt_leggi / bt_scrivi. Sul Pi non serve sudo: l'utente sta nel gruppo
# 'bluetooth'. Il freno observe/actuate lato server distingue le sola-lettura
# (scan, servizi, leggi) dalle attuative (accendi, scrivi), come per il resto.

_MAC_RE = re.compile(r"([0-9A-F]{2}(?::[0-9A-F]{2}){5})", re.I)
_UUID_RE = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}", re.I)


def _bt_disponibile():
    return _POSIX and shutil.which("bluetoothctl") is not None


def _btctl(righe, timeout=25):
    """Esegue una sequenza di comandi bluetoothctl in una sola sessione: glieli
    diamo su stdin uno per riga e chiudiamo con 'quit'. Torna l'output grezzo."""
    if not _bt_disponibile():
        raise RuntimeError("bluetoothctl non disponibile (solo Linux con BlueZ)")
    testo = "\n".join(list(righe) + ["quit"]) + "\n"
    r = subprocess.run(["bluetoothctl"], input=testo, capture_output=True,
                       text=True, timeout=timeout)
    return ((r.stdout or "") + (r.stderr or ""))[:MAX_OUTPUT]


def bt_accendi(args=None):
    """Accende l'adattatore Bluetooth. Attuativa."""
    out = _btctl(["power on"])
    return {"acceso": "succeeded" in out.lower(), "output": out[-400:]}


def bt_scan(args=None):
    """Scopre i dispositivi BLE nei dintorni. Sola lettura. args: {secondi=8}"""
    secondi = int((args or {}).get("secondi", 8))
    if not _bt_disponibile():
        raise RuntimeError("bluetoothctl non disponibile (solo Linux con BlueZ)")
    # --timeout fa terminare 'scan on' da solo; poi elenchiamo cosa si e' visto.
    subprocess.run(["bluetoothctl", "--timeout", str(secondi), "scan", "on"],
                   capture_output=True, text=True, timeout=secondi + 15)
    lista = _btctl(["devices"])
    dispositivi = []
    for l in lista.splitlines():
        m = _MAC_RE.search(l)
        if m and "Device" in l:
            nome = l.split(m.group(1), 1)[-1].strip()
            dispositivi.append({"mac": m.group(1), "nome": nome or None})
    return {"secondi": secondi, "trovati": len(dispositivi), "dispositivi": dispositivi}


def bt_servizi(args):
    """Servizi/caratteristiche GATT di un dispositivo. Sola lettura. args: {mac}"""
    mac = (args or {}).get("mac")
    if not mac:
        raise ValueError("serve 'mac'")
    out = _btctl(["power on", "connect %s" % mac, "menu gatt",
                  "list-attributes %s" % mac, "back", "disconnect %s" % mac], timeout=35)
    attributi = []
    for m in re.finditer(r"(Primary Service|Characteristic|Descriptor)[^\n]*?(" + _UUID_RE.pattern + ")",
                         out, re.I):
        attributi.append({"tipo": m.group(1), "uuid": m.group(2)})
    # dedup mantenendo l'ordine
    visti, uniq = set(), []
    for a in attributi:
        if a["uuid"] not in visti:
            visti.add(a["uuid"]); uniq.append(a)
    return {"mac": mac, "attributi": uniq,
            "output": out[-800:] if not uniq else None}


def bt_leggi(args):
    """Legge una caratteristica GATT. Sola lettura. args: {mac, uuid}"""
    mac = (args or {}).get("mac"); uuid = (args or {}).get("uuid")
    if not mac or not uuid:
        raise ValueError("servono 'mac' e 'uuid'")
    out = _btctl(["power on", "connect %s" % mac, "menu gatt",
                  "select-attribute %s" % uuid, "read", "back",
                  "disconnect %s" % mac], timeout=35)
    coda = out.split("read", 1)[-1]
    byte = re.findall(r"(?<![0-9a-fx])([0-9a-f]{2})\b", coda, re.I)
    return {"mac": mac, "uuid": uuid,
            "byte_hex": " ".join(byte[:64]) if byte else None, "output": out[-500:]}


def bt_scrivi(args):
    """Scrive su una caratteristica GATT. Attuativa. args: {mac, uuid, hex}"""
    mac = (args or {}).get("mac"); uuid = (args or {}).get("uuid")
    dati = (args or {}).get("hex", "")
    if not mac or not uuid or not dati:
        raise ValueError("servono 'mac', 'uuid', 'hex'")
    byte = " ".join(b if b.startswith("0x") else "0x" + b
                    for b in str(dati).replace(",", " ").split())
    out = _btctl(["power on", "connect %s" % mac, "menu gatt",
                  "select-attribute %s" % uuid, 'write "%s"' % byte, "back",
                  "disconnect %s" % mac], timeout=35)
    return {"mac": mac, "uuid": uuid,
            "scritto": "success" in out.lower(), "output": out[-400:]}


# ------------------------------------------------------------- registro

# Quali azioni questo modulo offre. L'agent le espone in capabilities: cosi' il
# tenant sa cosa puo' chiedere, e ne offre solo quelle vere.
AZIONI = {
    "stato_pc": stato_pc,
    "esegui": esegui,
    "esegui_job": esegui_job,
    "stato_job": stato_job,
    "output_job": output_job,
    "ferma_job": ferma_job,
    "leggi_file": leggi_file,
    "scrivi_file": scrivi_file,
    "lista_cartella": lista_cartella,
    "lista_processi": lista_processi,
    "ferma_processo": ferma_processo,
    # rete & scoperta dispositivi
    "wifi_scan": wifi_scan,
    "rete_scansiona": rete_scansiona,
    "onvif_trova": onvif_trova,
    "onvif_info": onvif_info,
    "camera_snapshot": camera_snapshot,
    # connessione isolata + diagnostica + monitor mode
    "wifi_segnale": wifi_segnale,
    "ping_qualita": ping_qualita,
    "wifi_connetti_isolato": wifi_connetti_isolato,
    "wifi_disconnetti": wifi_disconnetti,
    "monitor_deauth": monitor_deauth,
    # bluetooth / IoT (BlueZ, solo Linux)
    "bt_accendi": bt_accendi,
    "bt_scan": bt_scan,
    "bt_servizi": bt_servizi,
    "bt_leggi": bt_leggi,
    "bt_scrivi": bt_scrivi,
}
