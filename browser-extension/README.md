# SOWAI Browser Agent

L'estensione che fa del browser un device del tenant. Si accoppia a
sowai/fitness (o a qualunque Odoo con odoo_gpt_pos_agent) e da quel momento
l'AI, collegata al tenant via MCP, puo' guidare il browser come un operatore:
leggere le pagine, cliccare, compilare, scattare schermate.

## Come si installa

1. `chrome://extensions` → attiva "Modalita' sviluppatore"
2. "Carica estensione non pacchettizzata" → scegli questa cartella

Su un tenant vero l'estensione si scarica gia' pronta da Odoo, alla rotta
`/odoo-gpt/pos-agent/extension.zip`.

## Come si accoppia

Due strade, nel popup:

- **Auto-accoppia** — se sei loggato in Odoo nella stessa finestra, un clic:
  l'estensione usa la sessione (`/odoo-gpt/pos-agent/auto_pair`) e nasce gia'
  legata al tuo utente e alla tua azienda. Niente codice da digitare.
- **Manuale** — indirizzo del server + il codice a 6 cifre generato dalla
  backend (`Attivita' > Device`). Il codice dura 15 minuti.

## Il contratto

Parla le stesse quattro rotte del telefono e del PC:
`pair` / `heartbeat` / `poll` / `result`. Dichiara le sue capacita' -
`tabs`, `screenshot`, `full_browser` - e il tenant offre all'AI solo quelle.
E' la stessa architettura di manageDevice: il device dichiara, il tenant
instrada, ogni client collegato eredita gli strumenti col proprio scoping.

Nasce in sola osservazione: puo' leggere, non agire, finche' non lo si
autorizza a "puo' agire" dalla backend.
