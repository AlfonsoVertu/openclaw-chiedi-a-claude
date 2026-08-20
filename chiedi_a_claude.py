# -*- coding: utf-8 -*-
"""Un server MCP con un solo strumento: delega una domanda a Claude Code.

PERCHE' MCP E NON UN PLUGIN. Il plugin esisteva gia' e funzionava a meta':
`init` veniva chiamato, `registerTool` pure, ma lo strumento non arrivava mai
nella richiesta al modello. Verificato con una spia sul traffico: 31 strumenti
in arrivo, nessuno dei plugin locali. Gli strumenti esposti via MCP invece
arrivano - misurato: da 31 a 58 dopo aver agganciato Home Assistant.

A COSA SERVE. Un modello da 12 miliardi di parametri sbaglia le cose precise:
la sintassi esatta di una chiamata, un conto, del codice. Gli abbiamo appena
visto dire che 17 per 23 fa 405. Questo strumento gli da' un posto dove
chiedere invece di indovinare.

SULLE AUTORIZZAZIONI. Il plugin di prima girava con
--dangerously-skip-permissions, cioe' Claude poteva scrivere file ed eseguire
comandi senza chiedere - deciso da un modello che sbaglia. Qui il valore
predefinito e' SOLA LETTURA: Claude ragiona e risponde, non agisce. Per farlo
agire si mette CLAUDE_PUO_AGIRE=1, e allora e' una scelta esplicita di chi
configura, non un'impostazione che si eredita senza accorgersene.

Uso in OpenClaw:
    openclaw mcp add claude --command python --arg <percorso di questo file>
"""
import json
import os
import shutil
import subprocess
import sys

ATTESA = int(os.environ.get('CLAUDE_ATTESA', '180'))
PUO_AGIRE = os.environ.get('CLAUDE_PUO_AGIRE', '') not in ('', '0', 'no', 'false')
CARTELLA = os.environ.get('CLAUDE_CARTELLA') or os.path.expanduser('~')

STRUMENTO = {
    'name': 'chiedi_a_claude',
    'description': (
        "Delega a Claude qualunque cosa richieda precisione: la sintassi o i "
        "parametri esatti di una chiamata, un calcolo, scrivere o leggere "
        "codice, un ragionamento difficile. "
        "USALO SEMPRE quando non sei sicuro invece di tirare a indovinare: "
        "una risposta sbagliata data con sicurezza e' peggio di una domanda. "
        "In 'compito' scrivi la richiesta COMPLETA in linguaggio naturale, con "
        "tutto il contesto che serve: Claude non vede la tua conversazione."
    ),
    'inputSchema': {
        'type': 'object',
        'properties': {
            'compito': {
                'type': 'string',
                'description': "La richiesta completa da passare a Claude.",
            },
        },
        'required': ['compito'],
    },
}


def eseguibile():
    """Il percorso vero di Claude Code.

    Su Windows `claude` non e' un programma: e' `claude.cmd`, uno scrittino che
    ne avvia un altro. `subprocess` senza shell cerca un eseguibile e non lo
    trova, e restituisce "non e' nel PATH" anche quando c'e'. shutil.which
    conosce PATHEXT e risolve la questione senza aprire una shell - che non
    vogliamo aprire, perche' il compito arriva da un modello e finirebbe
    dentro una riga di comando interpretata.
    """
    forzato = os.environ.get('CLAUDE_ESEGUIBILE')
    if forzato:
        return forzato
    return shutil.which('claude') or shutil.which('claude.cmd') or 'claude'


def chiedi(compito):
    cmd = [eseguibile(), '-p', compito]
    if PUO_AGIRE:
        cmd.append('--dangerously-skip-permissions')
    try:
        r = subprocess.run(cmd, capture_output=True, text=True,
                           timeout=ATTESA, cwd=CARTELLA, shell=False,
                           encoding='utf-8', errors='replace')
    except FileNotFoundError:
        return "Claude Code non e' installato o non e' nel PATH."
    except subprocess.TimeoutExpired:
        return 'Claude non ha risposto entro %d secondi.' % ATTESA
    except Exception as e:
        return 'Errore avviando Claude: %s' % str(e)[:200]
    if r.returncode == 0 and (r.stdout or '').strip():
        return r.stdout.strip()
    return ('Claude e uscito con codice %s. %s'
            % (r.returncode, (r.stderr or '')[:500]))


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
        metodo = msg.get('method')
        id_ = msg.get('id')

        if metodo == 'initialize':
            rispondi(id_, {
                'protocolVersion': msg.get('params', {}).get('protocolVersion', '2024-11-05'),
                'capabilities': {'tools': {}},
                'serverInfo': {'name': 'chiedi-a-claude', 'version': '1.0.0'},
            })
        elif metodo in ('notifications/initialized', 'initialized'):
            continue                      # notifica: non vuole risposta
        elif metodo == 'tools/list':
            rispondi(id_, {'tools': [STRUMENTO]})
        elif metodo == 'tools/call':
            p = msg.get('params') or {}
            if p.get('name') != 'chiedi_a_claude':
                rispondi(id_, errore={'code': -32601,
                                      'message': 'strumento sconosciuto: %s' % p.get('name')})
                continue
            compito = ((p.get('arguments') or {}).get('compito') or '').strip()
            if not compito:
                rispondi(id_, {'content': [{'type': 'text', 'text': "Serve 'compito'."}],
                               'isError': True})
                continue
            testo = chiedi(compito)
            rispondi(id_, {'content': [{'type': 'text', 'text': testo}]})
        elif metodo in ('resources/list', 'prompts/list'):
            chiave = metodo.split('/')[0]
            rispondi(id_, {chiave: []})
        elif id_ is not None:
            rispondi(id_, errore={'code': -32601, 'message': 'metodo non gestito: %s' % metodo})


if __name__ == '__main__':
    main()
