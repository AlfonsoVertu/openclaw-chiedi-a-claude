# chiedi-a-claude

Un server MCP con un solo strumento: il modello locale delega a Claude Code
le cose che deve azzeccare.

## Perche' esiste

Un modello da 12 miliardi di parametri risponde a tutto, anche quando non sa.
Misurato qui: a `17 x 23` ha risposto `405`, con la stessa sicurezza con cui
avrebbe detto 391. Questo strumento gli da' un posto dove chiedere.

## Perche' MCP e non un plugin

Il primo tentativo era un plugin OpenClaw. Non funzionava, e non si vedeva:
`init` veniva chiamato, `registerTool` pure, nessun errore da nessuna parte -
ma nella richiesta al modello lo strumento non c'era. L'ho verificato mettendo
un passante che registrava il traffico verso il modello: 31 strumenti in
arrivo, nessuno di quelli dei plugin locali.

Gli strumenti esposti via MCP invece arrivano. Stesso passante, stesso
modello: da 31 a 58 dopo aver agganciato un server MCP. Da qui la scelta.

## Installazione

```
openclaw mcp add claude-locale --command python --arg /percorso/chiedi_a_claude.py
openclaw mcp probe        # deve dire: claude-locale: 1 tools
```

Serve Claude Code installato e gia' autenticato: lo strumento riusa
l'abbonamento di chi ha fatto il login, non chiede chiavi API.

## Autorizzazioni

Il valore predefinito e' **sola lettura**: Claude ragiona e risponde, non
tocca niente. Per farlo agire (file, comandi) si mette `CLAUDE_PUO_AGIRE=1`,
e allora e' una scelta esplicita di chi configura. La differenza conta,
perche' a decidere quando chiamarlo e' il modello piccolo, quello che sbaglia.

| variabile | predefinito | cosa fa |
|---|---|---|
| `CLAUDE_PUO_AGIRE` | vuoto (no) | se impostata, aggiunge `--dangerously-skip-permissions` |
| `CLAUDE_ATTESA` | `180` | secondi prima di rinunciare |
| `CLAUDE_CARTELLA` | la home | dove Claude lavora |
| `CLAUDE_ESEGUIBILE` | cercato nel PATH | percorso esplicito, se serve |

## Una nota su Windows

`claude` su Windows non e' un programma: e' `claude.cmd`. Chi lo avvia senza
shell non lo trova e riceve "non e' nel PATH" anche se c'e' - ed e' un errore
insidioso, perche' il modello riceve un messaggio d'errore, calcola da solo,
e attribuisce la risposta a Claude. E' successo: la prima prova ha dato 391,
giusto, con lo strumento che non aveva funzionato affatto. Ora il percorso si
risolve con `shutil.which`, che conosce `PATHEXT`, senza aprire una shell -
il compito arriva da un modello e non deve finire in una riga di comando
interpretata.

## Il set minimo

Questo strumento da' il meglio insieme a una potatura. Con 58 strumenti in
mano, un modello piccolo ne sbaglia la scelta prima ancora di sbagliare i
parametri. In OpenClaw, sull'agente:

```json
"tools": { "profile": "minimal", "alsoAllow": ["claude-locale__chiedi_a_claude", "..."] }
```

Attenzione: `minimal` da solo contiene **un** solo strumento e non include
`bundle-mcp`, quindi spegne anche i server MCP. Va sempre accompagnato da
`alsoAllow` che rinomina per esteso quello che si vuole tenere.
