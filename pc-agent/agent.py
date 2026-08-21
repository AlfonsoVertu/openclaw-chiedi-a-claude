# -*- coding: utf-8 -*-
"""Agent PC per SOWAI/fitness - Windows e Linux, stesso codice.

COS'E'. La terza gamba del tavolo, accanto all'estensione del browser e
all'app del telefono. Fa del PC un device del tenant: si accoppia, riceve gli
ordini interrogando il server (pair/heartbeat/poll/result), li esegue, e
restituisce. Identico contratto delle altre due periferiche.

PERCHE' PYTHON E NON UN BINARIO. Deve girare uguale su Windows e Linux senza
ricompilare, e senza dipendenze oltre la libreria standard: un agent che un
tenant installa sul PC della cassa non puo' pretendere che quel PC abbia un
ambiente di sviluppo. urllib e json bastano.

LA CAPACITA' CHE CONTA E' 'claude'. Se su questo PC c'e' Claude Code
installato e loggato, l'agent lo dichiara, e il tenant eredita "chiedi a
Claude" che gira QUI, con l'abbonamento di QUESTO PC. E' il modo in cui ogni
tenant ha il suo Claude senza toccare quello di nessun altro: non un servizio
centrale, ma una capacita' del device.

SICUREZZA. Il token e' l'unica chiave: si scambia una volta con il codice di
accoppiamento e poi vive solo qui, in un file leggibile solo dall'utente. Le
azioni che AGISCONO (eseguire un comando, scrivere un file) le autorizza il
tenant col campo mode del device - questo agent le esegue solo se il server
gliele manda, e il server non le manda a un device in sola osservazione.
"""
import json
import os
import platform
import shutil
import subprocess
import sys
import time
import urllib.error
import urllib.request

# --------------------------------------------------------------- impostazioni

VERSIONE = "1.0.0"
CARTELLA = os.path.join(
    os.environ.get("APPDATA") or os.path.expanduser("~/.config"),
    "sowai-pc-agent")
STATO = os.path.join(CARTELLA, "stato.json")

PAUSA_ATTIVA = 3        # secondi fra un giro e l'altro quando c'e' movimento
PAUSA_RIPOSO = 20       # quando e' un po' che non arriva niente
GIRI_PRIMA_DI_RALLENTARE = 5
BATTITO_OGNI = 8        # giri fra un heartbeat e l'altro

# Se l'agent puo' eseguire comandi di shell. Predefinito NO: un PC che esegue
# comandi arbitrari da remoto e' pericoloso, e la maggior parte del valore
# (chiedi_a_claude) non ne ha bisogno. Si accende con una variabile, che e'
# una scelta di chi installa, non un'eredita' silenziosa.
PUO_ESEGUIRE = os.environ.get("SOWAI_AGENT_SHELL", "") not in ("", "0", "no")


# ------------------------------------------------------------------- stato

def carica_stato():
    try:
        with open(STATO, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def salva_stato(d):
    os.makedirs(CARTELLA, exist_ok=True)
    tmp = STATO + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(d, f)
    os.replace(tmp, STATO)
    # su Linux, permessi stretti: il token e' qui dentro
    if os.name == "posix":
        try:
            os.chmod(STATO, 0o600)
        except Exception:
            pass


# ------------------------------------------------------------- rete (contratto)

def _post(base, rotta, corpo, attesa=30):
    dati = json.dumps(corpo).encode("utf-8")
    req = urllib.request.Request(
        base.rstrip("/") + "/odoo-gpt/pos-agent" + rotta,
        data=dati, method="POST",
        headers={"Content-Type": "application/json", "Accept": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=attesa) as r:
            return json.loads(r.read().decode("utf-8") or "{}")
    except urllib.error.HTTPError as e:
        try:
            return json.loads(e.read().decode("utf-8"))
        except Exception:
            return {"ok": False, "error": "HTTP %s" % e.code}
    except Exception as e:
        # la rete che cade non e' un errore da urlare: capita.
        return {"ok": False, "error": str(e)[:200]}


def accoppia(base, codice, capacita):
    r = _post(base, "/pair", {"pairing_code": codice.strip(), "capabilities": capacita})
    if r.get("ok"):
        salva_stato({"base": base.rstrip("/"),
                     "device_id": r["device_id"], "token": r["token"]})
    return r


def battito(st, capacita):
    return _post(st["base"], "/heartbeat",
                 {"device_id": st["device_id"], "token": st["token"],
                  "capabilities": capacita})


def ritira(st):
    r = _post(st["base"], "/poll", {"device_id": st["device_id"], "token": st["token"]})
    return r.get("commands") or []


def rispondi(st, corr_id, stato, risultato, errore, durata_ms):
    corpo = {"device_id": st["device_id"], "token": st["token"],
             "corr_id": corr_id, "status": stato, "duration_ms": durata_ms}
    if risultato is not None:
        corpo["result"] = risultato
    if errore is not None:
        corpo["error"] = errore
    return _post(st["base"], "/result", corpo)


# ------------------------------------------------------------- cosa sa fare

def claude_eseguibile():
    """Il percorso di Claude Code, o None se non c'e'.

    Su Windows 'claude' e' claude.cmd: which senza estensione non lo trova,
    e diremmo di non avere Claude quando invece c'e'. shutil.which conosce
    PATHEXT e risolve. E' lo stesso inciampo del server MCP.
    """
    return (os.environ.get("CLAUDE_ESEGUIBILE")
            or shutil.which("claude") or shutil.which("claude.cmd"))


def claude_loggato():
    """Claude c'e' ED e' autenticato? Solo allora la capacita' 'claude' vale.

    Un Claude installato ma non loggato promette qualcosa che non puo'
    mantenere: alla prima chiamata chiederebbe il login e si pianterebbe.
    """
    exe = claude_eseguibile()
    if not exe:
        return False
    try:
        # -p con un compito banale: se non e' loggato, esce con errore.
        r = subprocess.run([exe, "-p", "rispondi solo: ok"],
                           capture_output=True, text=True, timeout=60,
                           encoding="utf-8", errors="replace")
        return r.returncode == 0 and bool((r.stdout or "").strip())
    except Exception:
        return False


def capacita():
    """Cio' che questo PC dichiara di saper fare. Si CHIEDE al sistema, non si
    scrive: se Claude viene disinstallato o il login scade, la capacita'
    sparisce da sola al battito successivo, e il tenant smette di offrirla."""
    c = {
        "stato_pc": True,
        "sistema": "%s %s" % (platform.system(), platform.release()),
        "hostname": platform.node(),
        "claude": claude_loggato(),
    }
    if PUO_ESEGUIRE:
        c["esegui"] = True
    return c


def esegui_comando(azione, args):
    """Instrada un comando. Ogni azione ha un nome; niente e' una riga di
    shell libera, tranne 'esegui' che e' spento di default."""
    if azione in ("stato_pc", "ping"):
        return {"pong": True, "sistema": platform.platform(),
                "hostname": platform.node(),
                "claude": claude_loggato()}

    if azione in ("claude", "chiedi_a_claude"):
        compito = (args.get("compito") or args.get("params", {}).get("compito") or "").strip()
        if not compito:
            raise ValueError("serve 'compito'")
        exe = claude_eseguibile()
        if not exe:
            raise RuntimeError("Claude Code non e' installato su questo PC")
        cmd = [exe, "-p", compito]
        if args.get("puo_agire"):
            cmd.append("--dangerously-skip-permissions")
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=600,
                           encoding="utf-8", errors="replace", cwd=os.path.expanduser("~"))
        if r.returncode == 0 and (r.stdout or "").strip():
            return {"risposta": r.stdout.strip()}
        raise RuntimeError((r.stderr or "Claude uscito con codice %s" % r.returncode)[:500])

    if azione == "esegui":
        if not PUO_ESEGUIRE:
            raise PermissionError("l'esecuzione di comandi e' disattivata su questo agent")
        comando = args.get("comando") or args.get("params", {}).get("comando")
        if not comando:
            raise ValueError("serve 'comando'")
        r = subprocess.run(comando, capture_output=True, text=True, timeout=300,
                           shell=True, encoding="utf-8", errors="replace")
        return {"stdout": (r.stdout or "")[:8000], "stderr": (r.stderr or "")[:2000],
                "codice": r.returncode}

    raise ValueError("azione sconosciuta: %s" % azione)


# ------------------------------------------------------------- il ciclo

def ciclo():
    st = carica_stato()
    if not (st.get("base") and st.get("device_id") and st.get("token")):
        print("Agent non accoppiato. Lancia:  python agent.py accoppia <URL> <codice>")
        return
    print("Agent PC attivo. device_id=%s  server=%s" % (st["device_id"], st["base"]))
    print("Capacita': %s" % json.dumps(capacita(), ensure_ascii=False))

    giro = giri_vuoti = 0
    while True:
        try:
            if giro % BATTITO_OGNI == 0:
                battito(st, capacita())
            comandi = ritira(st)
            if not comandi:
                giri_vuoti += 1
            else:
                giri_vuoti = 0
                for c in comandi:
                    esegui_uno(st, c)
            giro += 1
            time.sleep(PAUSA_RIPOSO if giri_vuoti >= GIRI_PRIMA_DI_RALLENTARE else PAUSA_ATTIVA)
        except KeyboardInterrupt:
            print("\nfermato.")
            return
        except Exception as e:
            # qualunque cosa vada storta, si aspetta e si riprova: un agent che
            # muore al primo intoppo e' inutile.
            print("giro fallito, riprovo: %s" % str(e)[:150])
            time.sleep(PAUSA_RIPOSO)


def esegui_uno(st, cmd):
    corr = cmd.get("corr_id")
    azione = cmd.get("action")
    args = cmd.get("args") or {}
    if not corr or not azione:
        return
    inizio = time.time()
    try:
        ris = esegui_comando(azione, args)
        rispondi(st, corr, "done", ris, None, int((time.time() - inizio) * 1000))
        print("  [ok] %s" % azione)
    except Exception as e:
        rispondi(st, corr, "error", None, str(e)[:400], int((time.time() - inizio) * 1000))
        print("  [errore] %s: %s" % (azione, str(e)[:80]))


# ------------------------------------------------------------- avvio

def main():
    if len(sys.argv) >= 4 and sys.argv[1] == "accoppia":
        base, codice = sys.argv[2], sys.argv[3]
        print("Accoppiamento a %s ..." % base)
        r = accoppia(base, codice, capacita())
        if r.get("ok"):
            print("Accoppiato. device_id=%s. Avvia l'agent con:  python agent.py" % r["device_id"])
        else:
            print("Non riuscito: %s" % (r.get("error") or r))
            sys.exit(1)
        return
    if len(sys.argv) >= 2 and sys.argv[1] == "capacita":
        print(json.dumps(capacita(), ensure_ascii=False, indent=2))
        return
    ciclo()


if __name__ == "__main__":
    main()
