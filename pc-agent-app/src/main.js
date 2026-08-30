// -*- coding: utf-8 -*-
// Il processo principale di Electron: la finestra di configurazione, l'icona
// nella tray, e l'agent che gira in sottofondo.
//
// L'app vive nella tray, non nella barra: si configura una volta, poi resta
// li' come Claude Desktop. Chiudere la finestra la nasconde, non spegne
// l'agent - spegnerlo si fa dal menu della tray, apposta.

const { app, BrowserWindow, Tray, Menu, ipcMain, shell, nativeImage } = require('electron');
const path = require('path');
const { Agent, capacita } = require('./agent');
const { verificaClaude, installaClaude, loginClaude } = require('./claude-setup');
const fivem = require('./fivem');

const STATO_FILE = path.join(app.getPath('userData'), 'stato.json');

let finestra = null;
let tray = null;
let agent = null;
let ultimoStato = { tipo: 'avvio' };
// tenuto separato da ultimoStato (che l'avvio dell'agent sovrascrive subito
// dopo) cosi' tray e finestra sanno sempre se la sessione Claude e' valida,
// non solo al momento della verifica al lancio.
let ultimoClaudeStato = { verificato: false, installato: false, loggato: false };

// una sola istanza: se l'app e' gia' aperta, la seconda apertura mostra quella.
if (!app.requestSingleInstanceLock()) {
  app.quit();
} else {
  app.on('second-instance', () => mostraFinestra());
}

function creaFinestra() {
  finestra = new BrowserWindow({
    width: 480, height: 640, resizable: false,
    title: 'SOWAI Agent',
    autoHideMenuBar: true,
    icon: iconaApp(),
    webPreferences: {
      preload: path.join(__dirname, 'preload.js'),
      contextIsolation: true,
      nodeIntegration: false,
    },
  });
  finestra.loadFile(path.join(__dirname, 'ui.html'));
  // chiudere = nascondere, non uscire: l'agent deve restare vivo.
  finestra.on('close', (e) => {
    if (!app.inChiusura) { e.preventDefault(); finestra.hide(); }
  });
}

function mostraFinestra() {
  if (!finestra) creaFinestra();
  finestra.show();
  finestra.focus();
}

function iconaApp() {
  // un quadrato semplice generato al volo, cosi' l'app non dipende da un file
  // asset che potrebbe mancare. Verde se connesso, grigio se no.
  const verde = ultimoStato.tipo === 'avviato';
  const c = verde ? '#2C6360' : '#6B7684';
  const svg = `<svg xmlns="http://www.w3.org/2000/svg" width="32" height="32"><rect width="32" height="32" rx="7" fill="${c}"/><text x="16" y="22" font-size="16" fill="#fff" text-anchor="middle" font-family="sans-serif" font-weight="bold">S</text></svg>`;
  return nativeImage.createFromDataURL('data:image/svg+xml;base64,' + Buffer.from(svg).toString('base64'));
}

function creaTray() {
  tray = new Tray(iconaApp());
  aggiornaTray();
  tray.on('click', () => mostraFinestra());
}

function aggiornaTray() {
  if (!tray) return;
  tray.setImage(iconaApp());
  const connesso = ultimoStato.tipo === 'avviato';
  const claudeAvviso = ultimoClaudeStato.installato && !ultimoClaudeStato.loggato;
  const menu = Menu.buildFromTemplate([
    { label: connesso ? `Connesso · device ${ultimoStato.device_id}` : 'Non connesso', enabled: false },
    ...(claudeAvviso ? [{ label: '⚠ Claude non loggato: apri e "Accedi"', enabled: false }] : []),
    { type: 'separator' },
    { label: 'Apri', click: () => mostraFinestra() },
    connesso
      ? { label: 'Ferma agente', click: () => { agent && agent.ferma(); } }
      : { label: 'Avvia agente', click: () => { agent && agent.avvia(); } },
    { type: 'separator' },
    { label: 'Esci', click: () => { app.inChiusura = true; app.quit(); } },
  ]);
  tray.setContextMenu(menu);
  tray.setToolTip((connesso ? 'SOWAI Agent · connesso' : 'SOWAI Agent · fermo') +
                  (claudeAvviso ? ' · Claude non loggato' : ''));
}

function inoltraStato(s) {
  ultimoStato = s;
  aggiornaTray();
  if (finestra && !finestra.isDestroyed()) finestra.webContents.send('stato', s);
}

// ------------------------------------------------------------- IPC (finestra <-> agent)

ipcMain.handle('stato-attuale', async () => ({
  ...ultimoStato,
  accoppiato: agent ? agent.accoppiato : false,
  capacita: await capacita(false),
  claudeLancio: ultimoClaudeStato,
}));

ipcMain.handle('accoppia', async (_e, { base, codice }) => {
  const r = await agent.accoppia(base, codice);
  if (r.ok) agent.avvia();   // parte subito dopo l'accoppiamento
  return r;
});

ipcMain.handle('scollega', async () => { agent.scollega(); return { ok: true }; });
ipcMain.handle('avvia', async () => { agent.avvia(); return { ok: true }; });
ipcMain.handle('ferma', async () => { agent.ferma(); return { ok: true }; });

// Il flusso Claude: installazione + login, guidato dalla finestra.
ipcMain.handle('claude-stato', async () => verificaClaude());
ipcMain.handle('claude-installa', async () => installaClaude((riga) => {
  if (finestra) finestra.webContents.send('claude-log', riga);
}));
ipcMain.handle('claude-login', async () => loginClaude());

ipcMain.handle('agenti-rete', async () => agent ? agent.agentiInRete() : { ok: false, error: 'agent non pronto' });

ipcMain.handle('fivem-stato', async () => {
  try {
    const ric = await fivem.ricognizione();
    const prima = ric.installazioni[0] || null;
    return {
      trovato: ric.installazioni.length > 0 || ric.processi.length > 0,
      inEsecuzione: ric.processi.length > 0,
      cartella: (ric.processi[0] && ric.processi[0].cartella) || (prima && prima.cartella) || null,
    };
  } catch (e) {
    return { trovato: false, errore: String(e && e.message || e) };
  }
});

// ------------------------------------------------------------- avvio

app.whenReady().then(async () => {
  agent = new Agent(STATO_FILE, {
    puoEseguire: process.env.SOWAI_AGENT_SHELL ? true : false,
    onStato: inoltraStato,
  });
  creaTray();
  creaFinestra();

  // Claude Code gestisce da solo il refresh del proprio token OAuth quando
  // viene invocato - ma solo se lo si invoca. Qui lo si dichiara subito al
  // lancio, cosi' l'utente scopre una sessione scaduta dal tray/finestra,
  // non a meta' di un task che fallisce senza preavviso.
  try {
    const cs = await verificaClaude();
    ultimoClaudeStato = { verificato: true, installato: cs.installato, loggato: cs.loggato };
  } catch (e) {
    ultimoClaudeStato = { verificato: true, installato: false, loggato: false,
                          errore: String(e && e.message || e) };
  }
  aggiornaTray();
  if (finestra && !finestra.isDestroyed()) finestra.webContents.send('claude-stato-lancio', ultimoClaudeStato);

  // se gia' accoppiato, parte da solo senza mostrare la finestra
  if (agent.accoppiato) { finestra.hide(); agent.avvia(); }

  // avvio automatico all'accensione (l'utente lo vede/toglie dalle impostazioni
  // di Windows; qui lo attiviamo di default perche' un agent che non riparte
  // e' inutile).
  app.setLoginItemSettings({ openAtLogin: true, args: ['--hidden'] });
});

app.on('window-all-closed', (e) => { /* non usciamo: viviamo nella tray */ });
