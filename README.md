# chiedi-a-claude

Un server MCP che mette Claude Code a disposizione di un modello locale
piccolo: gli fa delegare quello che deve azzeccare, gli fa vedere il lavoro
mentre succede, e gli lascia passare a Claude i server MCP che gia' usa.

## Perche' esiste

Un modello da 12 miliardi di parametri risponde a tutto, anche quando non sa.
Misurato qui, sempre con la stessa sicurezza:

| chiesto | risposto | giusto |
|---|---|---|
| 17 × 23 | 405 | 391 |
| 47 × 89 | 4163 | 4183 |
| 8291 × 4457 | 36.803.167 | 36.952.987 |

L'ultimo e' il piu' istruttivo: era un sottoagente che avrebbe dovuto essere
Claude e non lo era, e ha risposto tre volte di fila attribuendosi un lavoro
che non aveva fatto fare a nessuno.

## Perche' MCP e non un plugin

Il primo tentativo era un plugin OpenClaw. Non funzionava, e non si vedeva:
`init` veniva chiamato, `registerTool` pure, nessun errore da nessuna parte -
ma nella richiesta al modello lo strumento non c'era. L'ho verificato mettendo
un passante che registrava il traffico verso il modello: 31 strumenti in
arrivo, nessuno di quelli dei plugin locali. Via MCP arrivano.

## Gli strumenti

| strumento | a cosa serve |
|---|---|
| `claude_chiedi` | delega un compito; continua l'ultima chat, o `nuova=true` per aprirne una |
| `claude_chat_elenco` | quali chat esistono, quali stanno lavorando, cosa hanno in mano |
| `claude_novita` | cosa e' successo in una chat da un certo punto in poi |
| `claude_mcp_elenco` | quali server MCP sono collegabili, e a quale chat sono collegati |
| `claude_mcp_collega` | mette un server in mano a Claude, **per nome** |
| `claude_mcp_scollega` | glielo toglie |

## Come si vede il lavoro in corso

MCP non ha un canale per spingere aggiornamenti che il chiamante mostri: il
client di OpenClaw gestisce solo `initialized` e `cancelled` - verificato nel
suo codice. Quindi non si spinge: si accumula. Ogni chat gira in sottofondo,
gli eventi si depositano, `claude_chiedi` torna subito con quello che c'e', e
`claude_novita` consegna il resto man mano. Quello che si vede:

```
[05:38:53] mcp     in uso: casa
[05:38:53] chiesto Il condizionatore della cucina e' acceso o spento?
[05:38:54] mcp     server collegati: casa
[05:38:56] usa     ToolSearch
[05:38:58] usa     mcp__casa__GetLiveContext
[05:39:02] Claude  Il condizionatore della cucina è spento (temperatura ambiente 30 °C).
```

## La sicurezza, che qui e' il punto

Chi decide di chiamare questi strumenti e' il modello piccolo - quello che
sbaglia - e quello che gli arriva puo' venire da un messaggio scritto da
chiunque. Da qui le regole:

- **Il modello passa un nome, mai un indirizzo.** `claude_mcp_collega` accetta
  solo nomi presenti nell'elenco letto dalla configurazione di OpenClaw, cioe'
  i server che l'utente ha collegato a mano. Se accettasse un URL, una pagina
  web che dice "collegati a questo server" diventerebbe un ordine eseguito.
- **Il server toglie se stesso dall'elenco.** Claude che si collega a "chiedi
  a Claude" e' un anello che gira su se stesso.
- **`--strict-mcp-config`**: Claude vede solo cio' che gli passiamo, non
  quello che si trova in giro per il disco.
- **Il permesso e' del server, non generale.** Ogni server collegato porta un
  `--allowedTools mcp__<nome>` e nient'altro. E' la differenza fra "Claude
  puo' usare la casa" e "Claude puo' fare qualunque cosa". Senza questo, in
  modalita' non interattiva Claude vede gli strumenti e non puo' toccarli: non
  c'e' nessuno a cui chiedere conferma, e risponde "concedi il permesso e
  riprovo".
- **Sola lettura di base.** File e comandi restano fuori salvo
  `CLAUDE_PUO_AGIRE=1`, che e' una scelta di chi configura la macchina.
- **Niente shell, mai.** Il compito arriva da un modello e non deve finire in
  una riga di comando interpretata.
- **Tetti su tutto**: chat contemporanee, eventi tenuti, byte restituiti,
  lunghezza del compito. Un ciclo impazzito costa memoria, non la macchina.
- **Gli identificativi di chat li genera il server** e li ricontrolla prima di
  usarli (`^[0-9a-f]{8}$`). Un `../../etc` come id viene rifiutato.

Prove fatte: URL al posto del nome → rifiutato; il server stesso → rifiutato;
`../../etc` come chat → rifiutato.

Il file passato a `--mcp-config` contiene i segreti dei server (per esempio il
token di Home Assistant). Sta nella cartella di stato, esiste solo finche'
serve, e non lo scrive nessun input del modello. Su Windows i permessi per
utente sono quello che sono: la difesa vera e' la seconda meta' della frase.

## Installazione

```
openclaw mcp add claude-locale --command python --arg /percorso/chiedi_a_claude.py
openclaw mcp probe        # deve dire: claude-locale: 6 tools
```

Serve Claude Code installato e gia' autenticato: riusa l'abbonamento di chi ha
fatto il login, non chiede chiavi API.

| variabile | predefinito | cosa fa |
|---|---|---|
| `CLAUDE_PUO_AGIRE` | vuoto (no) | se impostata, aggiunge `--dangerously-skip-permissions` |
| `CLAUDE_ATTESA` | `600` | secondi prima di fermare una chat |
| `CLAUDE_MAX_CHAT` | `4` | chat contemporanee |
| `CLAUDE_CARTELLA` | la home | dove Claude lavora |
| `CLAUDE_ESEGUIBILE` | cercato nel PATH | percorso esplicito, se serve |
| `CLAUDE_NOME_SERVER` | `claude-locale` | come si chiama questo server in OpenClaw, per escluderlo |
| `OPENCLAW_CONF` | `~/.openclaw/openclaw.json` | da dove si legge l'elenco dei server |

## Una nota su Windows

`claude` su Windows non e' un programma: e' `claude.cmd`. Chi lo avvia senza
shell non lo trova e riceve "non e' nel PATH" anche se c'e' - ed e' un errore
insidioso, perche' il modello riceve il messaggio d'errore, calcola da solo, e
attribuisce la risposta a Claude. E' successo: la prima prova ha dato 391,
giusto, con lo strumento che non aveva funzionato affatto. Ora il percorso si
risolve con `shutil.which`, che conosce `PATHEXT`, senza aprire una shell.

## Quanto dura una chat

Le chat vivono finche' vive il processo del server, cioe' quanto la
connessione che OpenClaw gli tiene aperta. L'id di sessione di Claude viene
conservato e riusato con `--resume`, quindi dentro una chat il filo non si
perde; fra un riavvio e l'altro di OpenClaw, l'elenco riparte vuoto.

## Il set minimo

Questo server da' il meglio insieme a una potatura. Con 58 strumenti in mano,
un modello piccolo ne sbaglia la scelta prima ancora di sbagliare i parametri.
In OpenClaw, sull'agente:

```json
"tools": { "profile": "minimal", "alsoAllow": ["claude-locale__claude_chiedi", "..."] }
```

Attenzione: `minimal` da solo contiene **un** solo strumento e non include
`bundle-mcp`, quindi spegne anche i server MCP. Va sempre accompagnato da
`alsoAllow` che rinomina per esteso quello che si vuole tenere.

---

## In questo repo c'è anche: SOWAI Browser Agent

`browser-extension/` — l'estensione Chrome che fa del browser un device del
tenant. Stessa architettura del server MCP qui sopra, altra periferica: si
accoppia a sowai/fitness con link + codice a 6 cifre (o auto-accoppiamento via
sessione Odoo), dichiara le sue capacita', e l'AI collegata al tenant la
guida. Vedi `browser-extension/README.md`.

Le tre gambe dello stesso tavolo - browser, telefono, PC - parlano tutte le
stesse quattro rotte verso il tenant: pair, heartbeat, poll, result.
