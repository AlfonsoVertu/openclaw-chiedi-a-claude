# -*- coding: utf-8 -*-
"""Un server MCP che mette Claude Code a disposizione del modello locale.

PERCHE' MCP E NON UN PLUGIN. Il plugin esisteva e funzionava a meta': `init`
veniva chiamato, `registerTool` pure, nessun errore da nessuna parte - ma
nella richiesta al modello lo strumento non c'era. Verificato con un passante
che registrava il traffico: 31 strumenti in arrivo, nessuno dei plugin. Via
MCP arrivano.

A COSA SERVE. Un modello piccolo risponde a tutto, anche quando non sa. Qui
gliel'abbiamo visto fare: 17x23 -> 405, e 8291x4457 -> 36.803.167 (giusto
36.952.987), detti con la stessa sicurezza. Questo server gli da' un posto
dove chiedere, e un modo per seguire quello che succede mentre succede.

COME ARRIVANO GLI AGGIORNAMENTI. MCP non ha un canale per spingere notifiche
che il chiamante mostri: il client di OpenClaw gestisce solo `initialized` e
`cancelled` - verificato nel suo codice. Quindi non si spinge: si accumula.
Ogni chat gira in sottofondo, gli eventi si depositano, e `claude_novita` li
consegna da un certo punto in poi. Chi chiede vede il lavoro in corso senza
restare fermo ad aspettare la fine.

LA SICUREZZA, CHE QUI E' IL PUNTO. Chi decide di chiamare questi strumenti e'
il modello piccolo, cioe' quello che sbaglia; e quello che gli arriva puo'
venire da un messaggio scritto da chiunque. Quindi:

  - Il modello non passa MAI un indirizzo, un comando o un percorso. Passa un
    NOME, e il nome deve stare in un elenco che curiamo noi. Un URL scelto dal
    modello sarebbe la porta d'ingresso: una pagina web che dice "collegati a
    questo server" diventerebbe un ordine eseguito.
  - L'elenco dei server collegabili si legge dalla configurazione di OpenClaw:
    sono i server che l'utente ha gia' collegato a mano. Nient'altro.
  - Questo server toglie se stesso dall'elenco. Claude che si collega a
    "chiedi a Claude" e' un anello che gira su se stesso.
  - `--strict-mcp-config`: Claude vede solo cio' che gli passiamo, non quello
    che si trova in giro per il disco.
  - Predefinito in SOLA LETTURA. Claude ragiona e risponde, non tocca niente.
    Per farlo agire serve CLAUDE_PUO_AGIRE=1, che e' una scelta di chi
    configura la macchina, non un'impostazione ereditata senza accorgersene.
  - Niente shell, mai: il compito arriva da un modello e non deve finire in
    una riga di comando interpretata.
  - Tetti su tutto: chat contemporanee, eventi tenuti, byte restituiti,
    lunghezza del compito. Un ciclo impazzito costa memoria, non la macchina.
  - Gli identificativi di chat li generiamo noi e li ricontrolliamo prima di
    metterli in una riga di comando.

Uso in OpenClaw:
    openclaw mcp add claude-locale --command python --arg /percorso/questo.py
"""
import json
import os
import re
import shutil
import subprocess
import sys
import threading
import time
import uuid

# ---------------------------------------------------------------- impostazioni

ATTESA = int(os.environ.get('CLAUDE_ATTESA', '600'))
PUO_AGIRE = os.environ.get('CLAUDE_PUO_AGIRE', '') not in ('', '0', 'no', 'false')
CARTELLA = os.environ.get('CLAUDE_CARTELLA') or os.path.expanduser('~')
CONF_OPENCLAW = os.environ.get(
    'OPENCLAW_CONF',
    os.path.join(os.path.expanduser('~'), '.openclaw', 'openclaw.json'))
STATO = os.environ.get(
    'CLAUDE_STATO',
    os.path.join(os.path.expanduser('~'), '.openclaw', 'mcp', 'chat-claude'))

MAX_CHAT_VIVE = int(os.environ.get('CLAUDE_MAX_CHAT', '4'))
MAX_EVENTI = 400            # per chat: oltre, si buttano i piu' vecchi
MAX_BYTE_RISPOSTA = 24000   # quanto si restituisce al modello in una volta
MAX_COMPITO = 8000

# Il nome di questo stesso server dentro OpenClaw: va escluso dall'elenco dei
# collegabili, altrimenti Claude puo' collegarsi a se' stesso.
IO_STESSO = os.environ.get('CLAUDE_NOME_SERVER', 'claude-locale')

NOME_VALIDO = re.compile(r'^[A-Za-z0-9._-]{1,64}$')
CHAT_VALIDA = re.compile(r'^[0-9a-f]{8}$')

_chiave = threading.Lock()
_chat = {}   # id -> dizionario


# ------------------------------------------------------------------- utilita'

def eseguibile():
    """Il percorso vero di Claude Code.

    Su Windows `claude` non e' un programma: e' `claude.cmd`, uno scrittino
    che ne avvia un altro. `subprocess` senza shell cerca un eseguibile, non
    lo trova, e restituisce "non e' nel PATH" anche quando c'e'. E' un errore
    insidioso: il modello riceve il messaggio d'errore, calcola da solo, e
    attribuisce la risposta a Claude. E' successo davvero. `shutil.which`
    conosce PATHEXT e risolve senza aprire una shell.
    """
    return (os.environ.get('CLAUDE_ESEGUIBILE')
            or shutil.which('claude') or shutil.which('claude.cmd') or 'claude')


def cartella_stato():
    try:
        os.makedirs(STATO, exist_ok=True)
    except Exception:
        pass
    return STATO


def server_disponibili():
    """I server MCP che l'utente ha gia' collegato in OpenClaw.

    E' questo l'elenco chiuso da cui il modello puo' scegliere. Non accetta
    indirizzi: solo nomi che stanno qui dentro.
    """
    try:
        with open(CONF_OPENCLAW, encoding='utf-8') as f:
            conf = json.load(f)
    except Exception:
        return {}
    grezzi = ((conf.get('mcp') or {}).get('servers') or {})
    fuori = {}
    for nome, s in grezzi.items():
        if nome == IO_STESSO or not NOME_VALIDO.match(nome or ''):
            continue
        if s.get('enabled') is False:
            continue
        fuori[nome] = s
    return fuori


def _voce_per_claude(s):
    """Traduce una voce di OpenClaw nella forma che vuole --mcp-config."""
    if s.get('url'):
        v = {'type': s.get('type') or 'sse', 'url': s['url']}
        if s.get('headers'):
            v['headers'] = s['headers']
        return v
    if s.get('command'):
        v = {'type': 'stdio', 'command': s['command'],
             'args': list(s.get('args') or [])}
        if s.get('env'):
            v['env'] = s['env']
        return v
    return None


def scrivi_config_mcp(chat_id, nomi):
    """Prepara il file che elenca a Claude i server da usare, e solo quelli.

    Contiene i segreti dei server (per esempio il token di Home Assistant),
    quindi sta nella cartella di stato e non altrove. Su Windows i permessi
    per utente sono quello che sono: la difesa vera e' che il file esiste solo
    finche' serve e che non lo scrive nessun input del modello.
    """
    disponibili = server_disponibili()
    dentro = {}
    for n in nomi:
        s = disponibili.get(n)
        if not s:
            continue
        v = _voce_per_claude(s)
        if v:
            dentro[n] = v
    if not dentro:
        return None, []
    p = os.path.join(cartella_stato(), 'mcp-%s.json' % chat_id)
    with open(p, 'w', encoding='utf-8') as f:
        json.dump({'mcpServers': dentro}, f)
    try:
        os.chmod(p, 0o600)
    except Exception:
        pass
    return p, sorted(dentro)


# ------------------------------------------------------------------- le chat

def _nuova_chat(mcp):
    return {
        'id': uuid.uuid4().hex[:8],
        'sessione': None,        # l'id di sessione di Claude, per riprendere
        'mcp': list(mcp),
        'eventi': [],
        'persi': 0,              # eventi buttati per far posto
        'stato': 'ferma',
        'aperta': time.time(),
        'ultimo': time.time(),
        'compiti': 0,
    }


def _annota(c, tipo, testo):
    with _chiave:
        c['eventi'].append({'n': len(c['eventi']) + c['persi'],
                            'quando': time.strftime('%H:%M:%S'),
                            'tipo': tipo, 'testo': testo})
        if len(c['eventi']) > MAX_EVENTI:
            tagliati = len(c['eventi']) - MAX_EVENTI
            c['eventi'] = c['eventi'][tagliati:]
            c['persi'] += tagliati
        c['ultimo'] = time.time()


def _leggi_evento(c, riga):
    """Traduce una riga di stream-json in una nota leggibile.

    Interessa il lavoro in corso - quale strumento sta usando, cosa dice - non
    il protocollo. Il resto si scarta: chi legge e' un modello piccolo, e ogni
    riga inutile e' contesto rubato a quelle utili.
    """
    try:
        e = json.loads(riga)
    except Exception:
        return
    t = e.get('type')
    if t == 'system':
        if e.get('session_id'):
            c['sessione'] = e['session_id']
        if e.get('subtype') == 'init':
            attrezzi = e.get('mcp_servers') or []
            if attrezzi:
                _annota(c, 'mcp', 'server collegati: ' + ', '.join(
                    str(s.get('name', s)) if isinstance(s, dict) else str(s)
                    for s in attrezzi))
        return
    if t == 'assistant':
        for b in ((e.get('message') or {}).get('content') or []):
            if b.get('type') == 'text' and (b.get('text') or '').strip():
                _annota(c, 'dice', b['text'].strip()[:2000])
            elif b.get('type') == 'tool_use':
                _annota(c, 'usa', str(b.get('name')))
        return
    if t == 'result':
        c['sessione'] = e.get('session_id') or c['sessione']
        testo = (e.get('result') or '').strip()
        if testo:
            _annota(c, 'esito', testo[:MAX_BYTE_RISPOSTA])
        if e.get('is_error'):
            _annota(c, 'errore', str(e.get('subtype') or 'errore'))


def _lavora(c, compito):
    """Fa girare un turno di Claude in sottofondo, annotando via via."""
    conf, collegati = scrivi_config_mcp(c['id'], c['mcp'])
    cmd = [eseguibile(), '-p', compito,
           '--output-format', 'stream-json', '--verbose']
    if c['sessione']:
        cmd += ['--resume', c['sessione']]
    if conf:
        cmd += ['--mcp-config', conf, '--strict-mcp-config']
        # Il permesso va dato, altrimenti Claude vede gli strumenti e non puo'
        # toccarli: senza interfaccia non c'e' nessuno a cui chiedere conferma,
        # e la risposta diventa "concedi il permesso e riprovo". Lo diamo
        # SOLO ai server che abbiamo appena collegato noi, per nome. Restano
        # fuori file, comandi e tutto il resto: e' la differenza fra "Claude
        # puo' usare la casa" e "Claude puo' fare qualunque cosa".
        for n in collegati:
            cmd += ['--allowedTools', 'mcp__%s' % n]
        _annota(c, 'mcp', 'in uso: ' + ', '.join(collegati))
    if PUO_AGIRE:
        cmd.append('--dangerously-skip-permissions')

    c['compiti'] += 1
    _annota(c, 'chiesto', compito[:1000])
    try:
        p = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                             cwd=CARTELLA, shell=False, text=True,
                             encoding='utf-8', errors='replace', bufsize=1)
    except FileNotFoundError:
        _annota(c, 'errore', "Claude Code non e' installato o non e' nel PATH.")
        c['stato'] = 'ferma'
        return
    except Exception as e:
        _annota(c, 'errore', 'avvio fallito: %s' % str(e)[:200])
        c['stato'] = 'ferma'
        return

    scadenza = time.time() + ATTESA
    try:
        for riga in p.stdout:
            _leggi_evento(c, riga)
            if time.time() > scadenza:
                p.kill()
                _annota(c, 'errore', 'fermato dopo %d secondi.' % ATTESA)
                break
    except Exception as e:
        _annota(c, 'errore', 'lettura interrotta: %s' % str(e)[:200])
    finally:
        try:
            p.wait(timeout=10)
        except Exception:
            try:
                p.kill()
            except Exception:
                pass
        err = ''
        try:
            err = (p.stderr.read() or '').strip()
        except Exception:
            pass
        if p.returncode not in (0, None) and err:
            _annota(c, 'errore', err[:600])
        if conf:
            try:
                os.remove(conf)
            except Exception:
                pass
        c['stato'] = 'ferma'


def _attendi_un_po(c, secondi):
    """Aspetta un attimo che arrivi qualcosa, ma non l'intera lavorazione.

    Le domande brevi finiscono in pochi secondi e conviene rispondere subito;
    quelle lunghe si seguono con claude_novita. Cosi' va bene in tutti e due i
    casi senza doverlo decidere prima.
    """
    fine = time.time() + secondi
    while time.time() < fine and c['stato'] == 'lavora':
        time.sleep(0.25)


# ---------------------------------------------------------------- i risultati

def _eventi_da(c, da):
    with _chiave:
        return [e for e in c['eventi'] if e['n'] >= da]


def _righe(eventi):
    etichette = {'chiesto': 'chiesto', 'dice': 'Claude', 'usa': 'usa',
                 'esito': 'esito', 'errore': 'ERRORE', 'mcp': 'mcp'}
    fuori, totale = [], 0
    for e in eventi:
        r = '[%s] %-7s %s' % (e['quando'], etichette.get(e['tipo'], e['tipo']),
                              e['testo'])
        totale += len(r)
        if totale > MAX_BYTE_RISPOSTA:
            fuori.append('... (il resto con claude_novita, da=%d)' % e['n'])
            break
        fuori.append(r)
    return fuori


# --------------------------------------------------------------- gli strumenti

STRUMENTI = [{
    'name': 'claude_chiedi',
    'description': (
        "Delega a Claude cio' che va azzeccato: un calcolo, la sintassi esatta "
        "di qualcosa, leggere o scrivere codice, un ragionamento lungo. "
        "USALO invece di tirare a indovinare. Se non passi 'chat' continua "
        "l'ultima chat aperta, cosi' Claude ricorda quello che vi siete gia' "
        "detti; metti nuova=true per ricominciare da capo. Torna subito con "
        "quello che c'e' finora: se il lavoro e' ancora in corso, prosegui con "
        "claude_novita."),
    'inputSchema': {'type': 'object', 'properties': {
        'compito': {'type': 'string', 'description': "La richiesta completa, con tutto il contesto: Claude non vede la tua conversazione."},
        'chat': {'type': 'string', 'description': "Id di una chat esistente (da claude_chat_elenco)."},
        'nuova': {'type': 'boolean', 'description': "true per aprire una chat nuova invece di continuare."},
        'attendi': {'type': 'number', 'description': "Secondi da aspettare prima di rispondere (0-120, predefinito 25)."},
    }, 'required': ['compito']},
}, {
    'name': 'claude_chat_elenco',
    'description': ("Le chat di Claude: quali stanno lavorando adesso, quando "
                    "hanno parlato l'ultima volta, quali server MCP hanno in "
                    "mano. Serve per sapere dove mandare la prossima richiesta."),
    'inputSchema': {'type': 'object', 'properties': {}},
}, {
    'name': 'claude_novita',
    'description': ("Cosa e' successo in una chat da un certo punto in poi: "
                    "cosa dice Claude, quali strumenti sta usando, come e' "
                    "finita. Richiamalo con il 'da' che ti restituisce per "
                    "seguire il lavoro senza rileggere tutto."),
    'inputSchema': {'type': 'object', 'properties': {
        'chat': {'type': 'string', 'description': "Id della chat."},
        'da': {'type': 'number', 'description': "Numero del primo evento da mostrare (predefinito 0)."},
    }, 'required': ['chat']},
}, {
    'name': 'claude_mcp_elenco',
    'description': ("I server MCP collegabili a Claude - sono quelli gia' "
                    "configurati in OpenClaw - e quali sono in mano a ogni "
                    "chat. Da qui prendi i nomi per claude_mcp_collega."),
    'inputSchema': {'type': 'object', 'properties': {}},
}, {
    'name': 'claude_mcp_collega',
    'description': ("Mette un server MCP in mano a Claude, per nome. Vale dal "
                    "prossimo claude_chiedi in quella chat. Si possono usare "
                    "solo i nomi che compaiono in claude_mcp_elenco: indirizzi "
                    "e comandi non si passano da qui."),
    'inputSchema': {'type': 'object', 'properties': {
        'nome': {'type': 'string', 'description': "Nome del server, da claude_mcp_elenco."},
        'chat': {'type': 'string', 'description': "Chat a cui collegarlo (predefinito: l'ultima)."},
    }, 'required': ['nome']},
}, {
    'name': 'claude_mcp_scollega',
    'description': "Toglie un server MCP dalle mani di Claude in una chat.",
    'inputSchema': {'type': 'object', 'properties': {
        'nome': {'type': 'string'},
        'chat': {'type': 'string'},
    }, 'required': ['nome']},
}]


def _testo(t, errore=False):
    r = {'content': [{'type': 'text', 'text': t}]}
    if errore:
        r['isError'] = True
    return r


def _trova_chat(chat):
    """La chat richiesta, l'ultima usata se non ne chiede una, False se l'id
    e' malformato - che e' un caso diverso da "non c'e'"."""
    if chat is None:
        vive = sorted(_chat.values(), key=lambda c: c['ultimo'], reverse=True)
        return vive[0] if vive else None
    if not CHAT_VALIDA.match(str(chat)):
        return False
    return _chat.get(chat)


def esegui(nome, arg):
    arg = arg or {}

    if nome == 'claude_chat_elenco':
        if not _chat:
            return _testo("Nessuna chat aperta. Ne apre una claude_chiedi.")
        righe = []
        for c in sorted(_chat.values(), key=lambda c: c['ultimo'], reverse=True):
            righe.append('%s  %-7s  %d compiti  ultimo %ds fa  mcp: %s'
                         % (c['id'], c['stato'], c['compiti'],
                            int(time.time() - c['ultimo']),
                            ', '.join(c['mcp']) or '-'))
        return _testo('\n'.join(righe))

    if nome == 'claude_mcp_elenco':
        disp = server_disponibili()
        righe = ['Collegabili (configurati in OpenClaw):']
        if disp:
            for n, s in sorted(disp.items()):
                righe.append('  %-16s %s' % (n, 'remoto' if s.get('url') else 'locale'))
        else:
            righe.append('  nessuno')
        righe.append('In mano alle chat:')
        if _chat:
            for c in _chat.values():
                righe.append('  %s -> %s' % (c['id'], ', '.join(c['mcp']) or '-'))
        else:
            righe.append('  nessuna chat aperta')
        return _testo('\n'.join(righe))

    if nome in ('claude_mcp_collega', 'claude_mcp_scollega'):
        n = str(arg.get('nome') or '').strip()
        if not NOME_VALIDO.match(n):
            return _testo("Nome non valido. Prendilo da claude_mcp_elenco.", True)
        c = _trova_chat(arg.get('chat'))
        if c is False:
            return _testo("Id di chat non valido.", True)
        if c is None:
            if len(_chat) >= MAX_CHAT_VIVE:
                return _testo("Troppe chat aperte.", True)
            c = _nuova_chat([])
            _chat[c['id']] = c
        if nome == 'claude_mcp_collega':
            if n not in server_disponibili():
                return _testo(
                    "'%s' non e' fra i server collegabili. Sono questi: %s. "
                    "Per aggiungerne uno va configurato in OpenClaw."
                    % (n, ', '.join(sorted(server_disponibili())) or 'nessuno'), True)
            if n not in c['mcp']:
                c['mcp'].append(n)
            return _testo("Collegato '%s' alla chat %s. Vale dal prossimo "
                          "claude_chiedi." % (n, c['id']))
        if n in c['mcp']:
            c['mcp'].remove(n)
        return _testo("Scollegato '%s' dalla chat %s." % (n, c['id']))

    if nome == 'claude_novita':
        c = _trova_chat(arg.get('chat'))
        if c is False:
            return _testo("Id di chat non valido.", True)
        if c is None:
            return _testo("Chat non trovata. Vedi claude_chat_elenco.", True)
        try:
            da = max(0, int(arg.get('da') or 0))
        except Exception:
            da = 0
        ev = _eventi_da(c, da)
        if not ev:
            return _testo("chat %s: %s, niente di nuovo da %d."
                          % (c['id'], c['stato'], da))
        return _testo('chat %s (%s) - prossimo da=%d\n%s'
                      % (c['id'], c['stato'], ev[-1]['n'] + 1,
                         '\n'.join(_righe(ev))))

    if nome == 'claude_chiedi':
        compito = str(arg.get('compito') or '').strip()
        if not compito:
            return _testo("Serve 'compito'.", True)
        if len(compito) > MAX_COMPITO:
            return _testo("Compito troppo lungo (max %d caratteri)." % MAX_COMPITO, True)
        c = None if arg.get('nuova') else _trova_chat(arg.get('chat'))
        if c is False:
            return _testo("Id di chat non valido.", True)
        if c is None:
            if len([x for x in _chat.values() if x['stato'] == 'lavora']) >= MAX_CHAT_VIVE:
                return _testo("Troppe chat gia' al lavoro (%d). Aspetta, oppure "
                              "guarda claude_chat_elenco." % MAX_CHAT_VIVE, True)
            c = _nuova_chat([])
            _chat[c['id']] = c
        if c['stato'] == 'lavora':
            return _testo("La chat %s sta gia' lavorando. Segui con claude_novita, "
                          "oppure apri una chat nuova con nuova=true." % c['id'], True)

        da = len(c['eventi']) + c['persi']
        # Lo stato va messo QUI, non dentro il thread: altrimenti l'attesa
        # qui sotto lo legge ancora 'ferma', conclude che il lavoro e' finito
        # e risponde "fatto" senza che sia partito niente. E' successo.
        c['stato'] = 'lavora'
        threading.Thread(target=_lavora, args=(c, compito), daemon=True).start()
        try:
            attesa = float(arg.get('attendi', 25))
        except Exception:
            attesa = 25
        _attendi_un_po(c, max(0.0, min(120.0, attesa)))

        ev = _eventi_da(c, da)
        prossimo = (ev[-1]['n'] + 1) if ev else da
        if c['stato'] == 'lavora':
            testa = ('chat %s: lavoro in corso. Prosegui con claude_novita '
                     '(chat=%s, da=%d).' % (c['id'], c['id'], prossimo))
        else:
            testa = 'chat %s: finito.' % c['id']
        return _testo(testa + '\n' + '\n'.join(_righe(ev)))

    return _testo("Strumento sconosciuto: %s" % nome, True)


# -------------------------------------------------------------- il protocollo

def rispondi(id_, risultato=None, errore=None):
    m = {'jsonrpc': '2.0', 'id': id_}
    if errore is not None:
        m['error'] = errore
    else:
        m['result'] = risultato
    sys.stdout.write(json.dumps(m, ensure_ascii=False) + '\n')
    sys.stdout.flush()


def main():
    for riga in sys.stdin:
        riga = riga.strip()
        if not riga:
            continue
        try:
            msg = json.loads(riga)
        except Exception:
            continue
        metodo, id_ = msg.get('method'), msg.get('id')

        if metodo == 'initialize':
            rispondi(id_, {
                'protocolVersion': (msg.get('params') or {}).get('protocolVersion', '2024-11-05'),
                'capabilities': {'tools': {}},
                'serverInfo': {'name': 'chiedi-a-claude', 'version': '2.0.0'},
            })
        elif metodo in ('notifications/initialized', 'initialized'):
            continue
        elif metodo == 'tools/list':
            rispondi(id_, {'tools': STRUMENTI})
        elif metodo == 'tools/call':
            p = msg.get('params') or {}
            try:
                rispondi(id_, esegui(p.get('name'), p.get('arguments')))
            except Exception as e:
                rispondi(id_, _testo('Errore interno: %s' % str(e)[:300], True))
        elif metodo in ('resources/list', 'prompts/list'):
            rispondi(id_, {metodo.split('/')[0]: []})
        elif id_ is not None:
            rispondi(id_, errore={'code': -32601,
                                  'message': 'metodo non gestito: %s' % metodo})


if __name__ == '__main__':
    main()
