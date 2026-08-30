// -*- coding: utf-8 -*-
// Gestione strutturata di un server FiveM (FXServer) su Windows: ricerca
// installazioni esistenti, installazione, avvio/stop/riavvio, console,
// lettura/scrittura file di configurazione, whitelist/ban.
//
// Stile coerente con agent.js: solo Node core, execFile con timeout (mai
// exec libero su input non fidato), funzioni async separate, niente
// dipendenze esterne.

const { execFile, spawn } = require('child_process');
const fs = require('fs');
const path = require('path');
const os = require('os');
const https = require('https');

// ------------------------------------------------------------- impostazioni

// URL del build server Windows piu' recente ("recommended" e' lo stream
// stabile ufficiale, evita di dover fare parsing dell'indice artifacts).
const URL_RECOMMENDED = 'https://runtime.fivem.net/artifacts/fivem/build_server_windows/master/';
const URL_ARTIFACTS_INDEX = 'https://changelogs-live.fivem.net/api/changelog/versions/win32/server';

// Cartelle tipiche dove un utente installa un server FiveM. Si scansionano
// solo queste + i drive disponibili con profondita' limitata: una scansione
// completa del disco sarebbe troppo lenta e invasiva.
function cartellePredefinite() {
  const home = os.homedir();
  const elenco = [
    'C:\\FXServer', 'C:\\FiveM', 'C:\\txData', 'C:\\server',
    path.join(home, 'FXServer'), path.join(home, 'FiveM'),
    path.join(home, 'Desktop', 'FXServer'), path.join(home, 'Desktop', 'FiveM'),
    path.join(home, 'Documents', 'FXServer'),
    'C:\\Games\\FXServer', 'D:\\FXServer', 'D:\\FiveM',
  ];
  return [...new Set(elenco)];
}

// ------------------------------------------------------------- ricerca

// Un'installazione e' riconosciuta dalla presenza di FXServer.exe. La
// cartella server-data (con server.cfg) puo' stare accanto o essere indicata
// a parte via --alt.
function esisteFXServer(cartella) {
  try {
    return fs.existsSync(path.join(cartella, 'FXServer.exe')) ||
           fs.existsSync(path.join(cartella, 'run.cmd'));
  } catch { return false; }
}

function trovaServerDataIn(cartella) {
  // server-data puo' chiamarsi in mille modi diversi; si cerca prima
  // il nome convenzionale, poi qualunque sottocartella con un server.cfg.
  const candidati = ['server-data', 'txData\\default\\resources\\..', 'resources\\..'];
  const diretto = path.join(cartella, 'server-data', 'server.cfg');
  if (fs.existsSync(diretto)) return path.join(cartella, 'server-data');
  const cfgQui = path.join(cartella, 'server.cfg');
  if (fs.existsSync(cfgQui)) return cartella;
  try {
    for (const voce of fs.readdirSync(cartella, { withFileTypes: true })) {
      if (voce.isDirectory() && fs.existsSync(path.join(cartella, voce.name, 'server.cfg'))) {
        return path.join(cartella, voce.name);
      }
    }
  } catch { /* cartella non leggibile: si ignora */ }
  return null;
}

// Scansiona le cartelle tipiche e ritorna le installazioni trovate. Nessuna
// eccezione anche se non trova nulla: "vuoto" e' normale, non un errore.
async function cercaInstallazioni() {
  const trovate = [];
  for (const cartella of cartellePredefinite()) {
    try {
      if (!fs.existsSync(cartella)) continue;
      if (esisteFXServer(cartella)) {
        trovate.push({
          cartella,
          serverData: trovaServerDataIn(cartella),
          exe: fs.existsSync(path.join(cartella, 'FXServer.exe'))
            ? path.join(cartella, 'FXServer.exe') : null,
        });
      } else {
        // magari FXServer.exe sta in una sottocartella diretta (es. una
        // versione estratta con nome tipo "server").
        let voci = [];
        try { voci = fs.readdirSync(cartella, { withFileTypes: true }); } catch { /* ignora */ }
        for (const v of voci) {
          if (!v.isDirectory()) continue;
          const sub = path.join(cartella, v.name);
          if (esisteFXServer(sub)) {
            trovate.push({
              cartella: sub,
              serverData: trovaServerDataIn(sub),
              exe: path.join(sub, 'FXServer.exe'),
            });
          }
        }
      }
    } catch { /* cartella inaccessibile: si salta, non e' un errore fatale */ }
  }
  return trovate;
}

// Cerca processi FXServer.exe gia' in esecuzione con la loro working
// directory (via WMIC, disponibile su ogni Windows senza dipendenze).
function processiInEsecuzione() {
  return new Promise((resolve) => {
    execFile('wmic', [
      'process', 'where', "name='FXServer.exe'",
      'get', 'ProcessId,ExecutablePath,CommandLine', '/format:csv',
    ], { timeout: 15000, encoding: 'utf8' }, (err, stdout) => {
      if (err || !stdout) { resolve([]); return; }
      const righe = stdout.split(/\r?\n/).map((r) => r.trim()).filter(Boolean);
      const intestazione = righe.findIndex((r) => r.toLowerCase().startsWith('node,'));
      if (intestazione === -1) { resolve([]); return; }
      const risultati = [];
      for (let i = intestazione + 1; i < righe.length; i++) {
        const parti = righe[i].split(',');
        if (parti.length < 4) continue;
        const [, commandLine, executablePath, processId] = parti;
        if (!executablePath) continue;
        risultati.push({
          pid: Number(processId) || null,
          exe: executablePath,
          cartella: path.dirname(executablePath),
          comando: commandLine || '',
        });
      }
      resolve(risultati);
    }).on('error', () => resolve([]));
  });
}

// Combina la scansione statica con i processi attivi: unica funzione che
// l'agent chiama per sapere "che situazione FiveM c'e' su questo PC".
async function ricognizione() {
  const [installazioni, processi] = await Promise.all([cercaInstallazioni(), processiInEsecuzione()]);
  return { installazioni, processi };
}

// ------------------------------------------------------------- installazione

function scaricaFile(url, dest, attesaMs = 300000) {
  return new Promise((resolve, reject) => {
    const t = setTimeout(() => { req.destroy(); reject(new Error('timeout download')); }, attesaMs);
    const file = fs.createWriteStream(dest);
    const req = https.get(url, { headers: { 'User-Agent': 'sowai-agent' } }, (res) => {
      if (res.statusCode >= 300 && res.statusCode < 400 && res.headers.location) {
        clearTimeout(t);
        file.close(() => fs.unlink(dest, () => {}));
        scaricaFile(res.headers.location, dest, attesaMs).then(resolve, reject);
        return;
      }
      if (res.statusCode !== 200) {
        clearTimeout(t);
        file.close(() => fs.unlink(dest, () => {}));
        reject(new Error('download fallito: HTTP ' + res.statusCode));
        return;
      }
      res.pipe(file);
      file.on('finish', () => { clearTimeout(t); file.close(resolve); });
    });
    req.on('error', (e) => { clearTimeout(t); reject(e); });
  });
}

// Ricava l'URL dell'ultimo artifact (o di una versione specifica) dall'indice
// ufficiale dei changelog FiveM.
async function urlArtifact(versione) {
  if (versione) {
    return `https://runtime.fivem.net/artifacts/fivem/build_server_windows/master/${versione}/server.zip`;
  }
  const dati = await new Promise((resolve, reject) => {
    https.get(URL_ARTIFACTS_INDEX, { headers: { 'User-Agent': 'sowai-agent' } }, (res) => {
      let buf = '';
      res.on('data', (d) => { buf += d; });
      res.on('end', () => {
        try { resolve(JSON.parse(buf)); } catch (e) { reject(e); }
      });
    }).on('error', reject);
  });
  const recommended = dati && (dati.recommended || dati.latest);
  if (!recommended) throw new Error('impossibile determinare la versione consigliata');
  return { url: `https://runtime.fivem.net/artifacts/fivem/build_server_windows/master/${recommended}/server.zip`, versione: recommended };
}

// Estrae uno zip usando tar (presente di default da Windows 10 1803+): niente
// dipendenza npm per lo zip.
function estraiZip(zip, destinazione) {
  return new Promise((resolve, reject) => {
    fs.mkdirSync(destinazione, { recursive: true });
    execFile('tar', ['-xf', zip, '-C', destinazione], { timeout: 120000 }, (err, stdout, stderr) => {
      if (err) reject(new Error('estrazione fallita: ' + (stderr || err.message)));
      else resolve();
    });
  });
}

// server.cfg minimale: l'utente lo personalizzera', serve solo perche' il
// server possa partire la prima volta senza crashare subito.
function serverCfgMinimo(nomeServer) {
  return [
    `endpoint_add_tcp "0.0.0.0:30120"`,
    `endpoint_add_udp "0.0.0.0:30120"`,
    ``,
    `sv_maxclients 32`,
    `sv_hostname "${nomeServer || 'Server FiveM'}"`,
    ``,
    `# licenza: da inserire, ottenibile su https://keymaster.fivem.net`,
    `sv_licenseKey "CHANGE_ME"`,
    ``,
    `sets tags "default"`,
    `sets locale "it-IT"`,
    ``,
    `add_ace group.admin command allow`,
    `add_ace group.admin command.quit deny`,
    `add_principal identifier.license:CHANGE_ME group.admin`,
    ``,
    `ensure mapmanager`,
    `ensure chat`,
    `ensure spawnmanager`,
    `ensure sessionmanager`,
    `ensure basic-gamemode`,
    `ensure hardcap`,
    ``,
  ].join('\r\n');
}

// Installa (o aggiorna, se richiesto esplicitamente) un server FXServer in
// `cartella`. Rifiuta di sovrascrivere un'installazione esistente a meno di
// forzareSovrascrittura=true, per non rovinare un server gia' in uso.
async function installa(cartella, opzioni = {}) {
  if (!cartella) throw new Error("serve 'cartella'");
  if (esisteFXServer(cartella) && !opzioni.forzareSovrascrittura) {
    throw new Error(`esiste gia' un'installazione in ${cartella}: passa forzareSovrascrittura per sovrascriverla`);
  }
  const risultatoUrl = await urlArtifact(opzioni.versione);
  const { url, versione } = typeof risultatoUrl === 'string'
    ? { url: risultatoUrl, versione: opzioni.versione || '?' }
    : risultatoUrl;

  fs.mkdirSync(cartella, { recursive: true });
  const zip = path.join(os.tmpdir(), `fxserver-${Date.now()}.zip`);
  await scaricaFile(url, zip);
  try {
    await estraiZip(zip, cartella);
  } finally {
    fs.unlink(zip, () => {});
  }

  let serverData = trovaServerDataIn(cartella);
  if (!serverData) {
    serverData = path.join(cartella, 'server-data');
    fs.mkdirSync(path.join(serverData, 'resources'), { recursive: true });
    fs.writeFileSync(path.join(serverData, 'server.cfg'), serverCfgMinimo(opzioni.nomeServer), 'utf8');
  }

  return {
    cartella,
    serverData,
    exe: path.join(cartella, 'FXServer.exe'),
    versione,
  };
}

// ------------------------------------------------------------- processo

// Registro in memoria dei processi avviati da questo modulo, per poterli
// fermare/interrogare senza dover ripescare il PID da altrove. Chiave =
// cartella server-data (una sola istanza per server-data ha senso).
const processiGestiti = new Map();

// Avvia FXServer con lo stdin collegato (pipe), condizione necessaria per
// poter mandare comandi alla console dopo l'avvio (vedi note su console piu'
// sotto). Non e' bloccante: spawn ritorna subito, il processo resta in vita
// sotto la gestione dell'agent finche' non viene fermato esplicitamente.
function avvia(exePath, serverData, opzioni = {}) {
  if (!exePath || !fs.existsSync(exePath)) throw new Error('FXServer.exe non trovato: ' + exePath);
  if (!serverData || !fs.existsSync(path.join(serverData, 'server.cfg'))) {
    throw new Error('server-data/server.cfg non trovato: ' + serverData);
  }
  const chiave = serverData;
  const esistente = processiGestiti.get(chiave);
  if (esistente && !esistente.processo.killed) {
    throw new Error('un server e\' gia\' in esecuzione per questa cartella (pid ' + esistente.processo.pid + ')');
  }

  const cfg = opzioni.cfgNome || 'server.cfg';
  const proc = spawn(exePath, ['+exec', cfg], {
    cwd: serverData,
    stdio: ['pipe', 'pipe', 'pipe'],
    windowsHide: true,
  });

  const bufferLog = [];
  const RIGHE_MAX = 500;
  const aggiungiLog = (chunk) => {
    const testo = chunk.toString('utf8');
    for (const riga of testo.split(/\r?\n/)) {
      if (!riga) continue;
      bufferLog.push(riga);
      if (bufferLog.length > RIGHE_MAX) bufferLog.shift();
    }
  };
  proc.stdout.on('data', aggiungiLog);
  proc.stderr.on('data', aggiungiLog);

  const voce = { processo: proc, avviatoAlle: Date.now(), log: bufferLog, exePath, serverData };
  processiGestiti.set(chiave, voce);
  proc.on('exit', () => { /* resta in mappa per stato/log finche' non viene rimpiazzato */ });

  return { pid: proc.pid, serverData, avviatoAlle: voce.avviatoAlle };
}

function stato(serverData) {
  const voce = processiGestiti.get(serverData);
  if (!voce || voce.processo.killed || voce.processo.exitCode !== null) {
    return { inEsecuzione: false };
  }
  return {
    inEsecuzione: true,
    pid: voce.processo.pid,
    avviatoAlle: voce.avviatoAlle,
    uptimeMs: Date.now() - voce.avviatoAlle,
  };
}

// Ferma il server mandando 'quit' in console (spegnimento pulito) e solo se
// non termina entro il timeout ricorre a kill().
function ferma(serverData, attesaMs = 15000) {
  return new Promise((resolve) => {
    const voce = processiGestiti.get(serverData);
    if (!voce || voce.processo.killed || voce.processo.exitCode !== null) {
      resolve({ giaFermo: true }); return;
    }
    const proc = voce.processo;
    let risolto = false;
    const finisci = (info) => { if (!risolto) { risolto = true; resolve(info); } };
    proc.once('exit', (code) => finisci({ fermato: true, codice: code }));
    try { proc.stdin.write('quit\n'); } catch { /* stdin gia' chiuso: si passa al kill */ }
    setTimeout(() => {
      if (!risolto) { try { proc.kill(); } catch { /* ignora */ } finisci({ fermato: true, forzato: true }); }
    }, attesaMs);
  });
}

async function riavvia(serverData, opzioniAvvio = {}) {
  const voce = processiGestiti.get(serverData);
  if (!voce) throw new Error('nessun server gestito per ' + serverData);
  const exePath = voce.exePath;
  await ferma(serverData);
  return avvia(exePath, serverData, opzioniAvvio);
}

// ------------------------------------------------------------- console
//
// SCELTA: stdin del processo, non txAdmin API.
// - txAdmin espone una API REST ma richiede autenticazione (sessione admin)
//   che l'agent non ha e non deve gestire come segreto extra; inoltre
//   txAdmin non e' sempre presente (dipende da come e' stato avviato il
//   server) mentre stdin c'e' sempre se avviamo noi il processo con spawn().
// - Scrivendo su proc.stdin i comandi vengono interpretati dalla console
//   nativa di FXServer esattamente come digitati a mano (ban, add_principal,
//   restart, say, ecc.), senza bisogno di credenziali aggiuntive.
// - Limite: funziona solo per i server AVVIATI da questo modulo (ne
//   possediamo lo stdin). Per un server gia' in esecuzione trovato da
//   `processiInEsecuzione()` non c'e' modo di riagganciarsi allo stdin di un
//   processo Windows altrui: va fermato e riavviato tramite questo modulo se
//   si vuole la console.

function mandaComando(serverData, comando) {
  const voce = processiGestiti.get(serverData);
  if (!voce || voce.processo.killed || voce.processo.exitCode !== null) {
    throw new Error('nessun server in esecuzione (gestito da questo modulo) per ' + serverData);
  }
  voce.processo.stdin.write(String(comando).trim() + '\n');
  return { inviato: true };
}

function leggiLog(serverData, righe = 100) {
  const voce = processiGestiti.get(serverData);
  if (!voce) return { righe: [] };
  return { righe: voce.log.slice(-Math.max(1, Math.min(righe, voce.log.length))) };
}

// ------------------------------------------------------------- file

function leggiServerCfg(serverData) {
  const p = path.join(serverData, 'server.cfg');
  if (!fs.existsSync(p)) throw new Error('server.cfg non trovato in ' + serverData);
  return fs.readFileSync(p, 'utf8');
}

// Scrive server.cfg con backup del precedente (server.cfg.bak), per non
// perdere la configurazione se qualcosa va storto.
function scriviServerCfg(serverData, contenuto) {
  const p = path.join(serverData, 'server.cfg');
  if (fs.existsSync(p)) {
    fs.copyFileSync(p, p + '.bak');
  }
  fs.writeFileSync(p, contenuto, 'utf8');
  return { scritto: true, backup: fs.existsSync(p + '.bak') };
}

function elencaRisorse(serverData) {
  const dir = path.join(serverData, 'resources');
  if (!fs.existsSync(dir)) return [];
  const risultato = [];
  const scandaglia = (cartella, prefisso) => {
    let voci = [];
    try { voci = fs.readdirSync(cartella, { withFileTypes: true }); } catch { return; }
    for (const v of voci) {
      if (!v.isDirectory()) continue;
      const percorso = path.join(cartella, v.name);
      const haFxmanifest = fs.existsSync(path.join(percorso, 'fxmanifest.lua')) ||
                            fs.existsSync(path.join(percorso, '__resource.lua'));
      if (haFxmanifest) {
        risultato.push(prefisso ? `${prefisso}/${v.name}` : v.name);
      } else {
        // categorie/cartelle organizzative: si scende di un livello soltanto.
        scandaglia(percorso, prefisso ? `${prefisso}/${v.name}` : v.name);
      }
    }
  };
  scandaglia(dir, '');
  return risultato;
}

// ------------------------------------------------------------- utenti (whitelist/ban)
//
// Via server.cfg (add_principal / add_ace), la stessa che usa la console:
// robusta perche' non dipende da txAdmin ne' da API esterne, funziona anche
// a server fermo (si applica al riavvio) o a caldo (via mandaComando).

function whitelistAggiungi(serverData, identificativo, gruppo = 'user') {
  const riga = `add_principal ${identificativo} group.${gruppo}`;
  const voce = processiGestiti.get(serverData);
  if (voce && voce.processo.exitCode === null) {
    mandaComando(serverData, riga);
  }
  const cfg = leggiServerCfg(serverData);
  if (!cfg.includes(riga)) scriviServerCfg(serverData, cfg + '\r\n' + riga + '\r\n');
  return { aggiunto: true, riga };
}

function bannaGiocatore(serverData, identificativoOPid, motivo = '') {
  // 'ban' via console: FXServer applica subito se il server e' attivo;
  // richiede il resource 'ban' o e' un comando builtin a seconda della
  // versione, quindi si tenta comunque il comando standard.
  const comando = `ban ${identificativoOPid} ${motivo}`.trim();
  return mandaComando(serverData, comando);
}

module.exports = {
  cercaInstallazioni,
  processiInEsecuzione,
  ricognizione,
  installa,
  avvia,
  ferma,
  riavvia,
  stato,
  mandaComando,
  leggiLog,
  leggiServerCfg,
  scriviServerCfg,
  elencaRisorse,
  whitelistAggiungi,
  bannaGiocatore,
};
