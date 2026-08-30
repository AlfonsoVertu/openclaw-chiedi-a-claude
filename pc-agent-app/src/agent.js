// -*- coding: utf-8 -*-
// Il cuore dell'agent: le quattro rotte verso il tenant, il rilevamento di
// Claude, il ciclo. Portato da agent.py (gia' provato: 36952987 end-to-end),
// qui in Node perche' l'app e' Electron e il runtime e' uno solo.
//
// Nessuna dipendenza oltre a Node: fetch e' nativo dalla 18, e cosi' l'app
// resta leggera e l'installer non deve portarsi dietro mezzo npm.

const { execFile } = require('child_process');
const os = require('os');
const fs = require('fs');
const path = require('path');
const fivem = require('./fivem');

// ------------------------------------------------------------- impostazioni

const PAUSA_ATTIVA = 3000;   // ms fra un giro e l'altro quando c'e' movimento
const PAUSA_RIPOSO = 20000;  // quando e' un po' che non arriva niente
const GIRI_PRIMA_DI_RALLENTARE = 5;
const BATTITO_OGNI = 8;      // giri fra un heartbeat e l'altro

// ------------------------------------------------------------- Claude

// Trova l'eseguibile di Claude. Su Windows 'claude' e' claude.cmd, quindi si
// provano entrambi. Ritorna il comando da lanciare, o null.
function claudeComando() {
  if (process.env.CLAUDE_ESEGUIBILE) return process.env.CLAUDE_ESEGUIBILE;
  // su Windows execFile risolve claude.cmd solo con shell; qui restituiamo il
  // nome e lasciamo che il chiamante usi shell:true dove serve.
  return process.platform === 'win32' ? 'claude.cmd' : 'claude';
}

// Claude c'e' ED e' loggato? Solo allora la capacita' vale. Un Claude
// installato ma non autenticato promette cio' che non puo' mantenere.
function claudeLoggato() {
  return new Promise((resolve) => {
    const exe = claudeComando();
    const p = execFile(exe, ['-p', 'rispondi solo: ok'],
      { timeout: 60000, shell: process.platform === 'win32', encoding: 'utf8' },
      (err, stdout) => {
        resolve(!err && !!(stdout || '').trim());
      });
    p.on('error', () => resolve(false));
  });
}

function chiediAClaude(compito, puoAgire) {
  return new Promise((resolve, reject) => {
    const exe = claudeComando();
    const args = ['-p', compito];
    if (puoAgire) args.push('--dangerously-skip-permissions');
    execFile(exe, args, {
      timeout: 600000, shell: process.platform === 'win32',
      encoding: 'utf8', maxBuffer: 20 * 1024 * 1024,
      cwd: os.homedir(),
    }, (err, stdout, stderr) => {
      if (!err && (stdout || '').trim()) resolve(stdout.trim());
      else reject(new Error((stderr || (err && err.message) || 'Claude non ha risposto').slice(0, 500)));
    });
  });
}

// ------------------------------------------------------------- capacita'

async function capacita(puoEseguire) {
  const c = {
    stato_pc: true,
    sistema: `${os.type()} ${os.release()}`,
    hostname: os.hostname(),
    claude: await claudeLoggato(),
  };
  if (puoEseguire) c.esegui = true;
  // FiveM: capacita' sempre annunciata (ricerca/installazione non richiedono
  // 'esegui'), ma le azioni di gestione processo/console sono comunque
  // filtrate da puoEseguire dentro _eseguiAzione.
  try {
    const ric = await fivem.ricognizione();
    c.fivem = true;
    c.fivem_trovato = ric.installazioni.length > 0 || ric.processi.length > 0;
  } catch {
    c.fivem = true;
    c.fivem_trovato = false;
  }
  return c;
}

// ------------------------------------------------------------- rete (contratto)

async function post(base, rotta, corpo, attesaMs = 30000) {
  const ctrl = new AbortController();
  const t = setTimeout(() => ctrl.abort(), attesaMs);
  try {
    const r = await fetch(base.replace(/\/+$/, '') + '/odoo-gpt/pos-agent' + rotta, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json', 'Accept': 'application/json' },
      body: JSON.stringify(corpo),
      signal: ctrl.signal,
    });
    return await r.json();
  } catch (e) {
    // la rete che cade e' la normalita' di un PC: non e' un errore da urlare.
    return { ok: false, error: String(e && e.message || e).slice(0, 200) };
  } finally {
    clearTimeout(t);
  }
}

// ------------------------------------------------------------- l'agent

class Agent {
  constructor(statoFile, opzioni = {}) {
    this.statoFile = statoFile;
    this.puoEseguire = !!opzioni.puoEseguire;
    this.onStato = opzioni.onStato || (() => {});   // callback per la UI/tray
    this.attivo = false;
    this.stato = this._carica();
  }

  _carica() {
    try { return JSON.parse(fs.readFileSync(this.statoFile, 'utf8')); }
    catch { return {}; }
  }

  _salva() {
    fs.mkdirSync(path.dirname(this.statoFile), { recursive: true });
    const tmp = this.statoFile + '.tmp';
    fs.writeFileSync(tmp, JSON.stringify(this.stato), { mode: 0o600 });
    fs.renameSync(tmp, this.statoFile);
  }

  get accoppiato() {
    return !!(this.stato.base && this.stato.device_id && this.stato.token);
  }

  async accoppia(base, codice) {
    const caps = await capacita(this.puoEseguire);
    const r = await post(base, '/pair', { pairing_code: codice.trim(), capabilities: caps });
    if (r.ok) {
      this.stato = { base: base.replace(/\/+$/, ''), device_id: r.device_id, token: r.token };
      this._salva();
    }
    return r;
  }

  scollega() {
    this.attivo = false;
    this.stato = {};
    try { fs.unlinkSync(this.statoFile); } catch {}
    this.onStato({ tipo: 'scollegato' });
  }

  async _eseguiUno(cmd) {
    const { corr_id, action, args = {} } = cmd;
    if (!corr_id || !action) return;
    const inizio = Date.now();
    try {
      const ris = await this._eseguiAzione(action, args);
      await this._rispondi(corr_id, 'done', ris, null, Date.now() - inizio);
      this.onStato({ tipo: 'eseguito', azione: action, ok: true });
    } catch (e) {
      await this._rispondi(corr_id, 'error', null, String(e.message || e).slice(0, 400), Date.now() - inizio);
      this.onStato({ tipo: 'eseguito', azione: action, ok: false, errore: String(e.message || e) });
    }
  }

  async _eseguiAzione(azione, args) {
    const p = args.params || args;
    if (azione === 'stato_pc' || azione === 'ping') {
      return { pong: true, sistema: os.platform(), hostname: os.hostname(),
               claude: await claudeLoggato() };
    }
    if (azione === 'claude' || azione === 'chiedi_a_claude') {
      const compito = (p.compito || '').trim();
      if (!compito) throw new Error("serve 'compito'");
      return { risposta: await chiediAClaude(compito, !!p.puo_agire) };
    }
    if (azione === 'esegui') {
      if (!this.puoEseguire) throw new Error("l'esecuzione di comandi e' disattivata");
      const comando = p.comando;
      if (!comando) throw new Error("serve 'comando'");
      return await this._shell(comando);
    }
    if (azione === 'esegui_admin') {
      // stessa guardia di 'esegui': l'elevazione non aggira il consenso
      // dell'utente sull'agent, aggiunge solo il consenso UAC sopra.
      if (!this.puoEseguire) throw new Error("l'esecuzione di comandi e' disattivata");
      const comando = p.comando;
      if (!comando) throw new Error("serve 'comando'");
      return await this._shellAdmin(comando);
    }
    if (azione === 'agenti_rete' || azione === 'lista_peer') {
      return await this.agentiInRete();
    }
    if (azione === 'fivem') {
      return await this._eseguiFivem(p);
    }
    throw new Error('azione sconosciuta: ' + azione);
  }

  // Sotto-comandi FiveM: p.sotto seleziona l'operazione, gli altri campi di
  // p sono i parametri specifici. La ricerca e' sempre permessa (sola
  // lettura); installazione/avvio/stop/console/scrittura richiedono
  // puoEseguire, come per l'azione 'esegui'.
  async _eseguiFivem(p) {
    const sotto = p.sotto;
    if (!sotto) throw new Error("serve 'sotto' (es. cerca, stato, avvia, ferma, console, ...)");
    const soloLettura = new Set(['cerca', 'stato', 'log', 'leggi_cfg', 'risorse']);
    if (!soloLettura.has(sotto) && !this.puoEseguire) {
      throw new Error("l'esecuzione di comandi e' disattivata, non posso fare '" + sotto + "' su FiveM");
    }
    switch (sotto) {
      case 'cerca':
        return await fivem.ricognizione();
      case 'installa':
        if (!p.cartella) throw new Error("serve 'cartella'");
        return await fivem.installa(p.cartella, {
          versione: p.versione, nomeServer: p.nome_server,
          forzareSovrascrittura: !!p.forza,
        });
      case 'avvia':
        if (!p.exe || !p.server_data) throw new Error("servono 'exe' e 'server_data'");
        return fivem.avvia(p.exe, p.server_data, { cfgNome: p.cfg_nome });
      case 'ferma':
        if (!p.server_data) throw new Error("serve 'server_data'");
        return await fivem.ferma(p.server_data);
      case 'riavvia':
        if (!p.server_data) throw new Error("serve 'server_data'");
        return await fivem.riavvia(p.server_data, { cfgNome: p.cfg_nome });
      case 'stato':
        if (!p.server_data) throw new Error("serve 'server_data'");
        return fivem.stato(p.server_data);
      case 'console':
        if (!p.server_data || !p.comando) throw new Error("servono 'server_data' e 'comando'");
        return fivem.mandaComando(p.server_data, p.comando);
      case 'log':
        if (!p.server_data) throw new Error("serve 'server_data'");
        return fivem.leggiLog(p.server_data, p.righe);
      case 'leggi_cfg':
        if (!p.server_data) throw new Error("serve 'server_data'");
        return { contenuto: fivem.leggiServerCfg(p.server_data) };
      case 'scrivi_cfg':
        if (!p.server_data || p.contenuto == null) throw new Error("servono 'server_data' e 'contenuto'");
        return fivem.scriviServerCfg(p.server_data, p.contenuto);
      case 'risorse':
        if (!p.server_data) throw new Error("serve 'server_data'");
        return { risorse: fivem.elencaRisorse(p.server_data) };
      case 'whitelist_aggiungi':
        if (!p.server_data || !p.identificativo) throw new Error("servono 'server_data' e 'identificativo'");
        return fivem.whitelistAggiungi(p.server_data, p.identificativo, p.gruppo);
      case 'banna':
        if (!p.server_data || !p.identificativo) throw new Error("servono 'server_data' e 'identificativo'");
        return fivem.bannaGiocatore(p.server_data, p.identificativo, p.motivo);
      default:
        throw new Error('sotto-azione fivem sconosciuta: ' + sotto);
    }
  }

  _shell(comando) {
    const { exec } = require('child_process');
    return new Promise((resolve) => {
      exec(comando, { timeout: 300000, encoding: 'utf8', maxBuffer: 8 * 1024 * 1024 },
        (err, stdout, stderr) => {
          resolve({ stdout: (stdout || '').slice(0, 8000),
                    stderr: (stderr || '').slice(0, 2000),
                    codice: err ? (err.code || 1) : 0 });
        });
    });
  }

  // Come _shell, ma con elevazione: su Windows rilancia il comando in un
  // processo con privilegi amministrativi via PowerShell
  // 'Start-Process -Verb RunAs -Wait', che fa comparire il prompt UAC
  // nativo. Non c'e' modo di leggere stdout/stderr del processo elevato
  // attraverso RunAs (e' un altro token, un'altra sessione): si scrive tutto
  // su file temporanei e li si legge dopo - stesso trucco di chiunque debba
  // elevare senza dipendenze npm in piu'.
  _shellAdmin(comando) {
    if (process.platform !== 'win32') {
      // fuori da Windows non c'e' UAC: si esegue e basta, l'elevazione la
      // gestisce l'utente con sudo se serve.
      return this._shell(comando);
    }
    const os2 = require('os');
    const { execFile } = require('child_process');
    const bollo = Date.now() + '-' + Math.floor(Math.random() * 1e6);
    const outFile = path.join(os2.tmpdir(), `sowai-admin-out-${bollo}.txt`);
    const errFile = path.join(os2.tmpdir(), `sowai-admin-err-${bollo}.txt`);
    const codeFile = path.join(os2.tmpdir(), `sowai-admin-code-${bollo}.txt`);
    const cmdFile = path.join(os2.tmpdir(), `sowai-admin-cmd-${bollo}.cmd`);
    const runnerFile = path.join(os2.tmpdir(), `sowai-admin-runner-${bollo}.ps1`);
    // il comando dell'utente finisce in un .cmd separato: evita ogni problema
    // di escaping fra PowerShell e cmd.exe, ognuno legge solo il proprio file.
    fs.writeFileSync(cmdFile, '@echo off\r\n' + comando + '\r\n');
    const runner = [
      `$p = Start-Process -FilePath cmd.exe -ArgumentList '/c "${cmdFile}" > "${outFile}" 2> "${errFile}"' -Verb RunAs -WindowStyle Hidden -Wait -PassThru`,
      `$p.ExitCode | Out-File -FilePath "${codeFile}" -Encoding ascii`,
    ].join('\r\n');
    fs.writeFileSync(runnerFile, runner);
    return new Promise((resolve) => {
      execFile('powershell.exe',
        ['-NoProfile', '-NonInteractive', '-ExecutionPolicy', 'Bypass', '-File', runnerFile],
        { timeout: 300000, encoding: 'utf8', maxBuffer: 8 * 1024 * 1024 },
        (err) => {
          let stdout = '', stderr = '', codice = 1;
          try { stdout = fs.readFileSync(outFile, 'utf8').slice(0, 8000); } catch {}
          try { stderr = fs.readFileSync(errFile, 'utf8').slice(0, 2000); } catch {}
          try { codice = parseInt(fs.readFileSync(codeFile, 'utf8').trim(), 10); } catch {}
          if (Number.isNaN(codice)) codice = err ? 1 : 0;
          for (const f of [outFile, errFile, codeFile, cmdFile, runnerFile]) {
            try { fs.unlinkSync(f); } catch {}
          }
          resolve({ stdout, stderr, codice, elevato: true });
        });
    });
  }

  _rispondi(corr, stato, risultato, errore, durataMs) {
    const c = { device_id: this.stato.device_id, token: this.stato.token,
                corr_id: corr, status: stato, duration_ms: durataMs };
    if (risultato != null) c.result = risultato;
    if (errore != null) c.error = errore;
    return post(this.stato.base, '/result', c);
  }

  // Gli altri device dello stesso tenant (stessa company del device
  // corrente): usata sia come azione remota che dalla UI locale via IPC.
  // Se non accoppiato ritorna un fallimento pulito, senza eccezioni.
  async agentiInRete() {
    if (!this.accoppiato) return { ok: false, error: 'non accoppiato' };
    const r = await post(this.stato.base, '/list_peers',
      { device_id: this.stato.device_id, token: this.stato.token });
    if (!r || !r.ok) return { ok: false, error: (r && r.error) || 'richiesta non riuscita' };
    return { ok: true, devices: r.devices || [] };
  }

  async avvia() {
    if (this.attivo) return;
    if (!this.accoppiato) { this.onStato({ tipo: 'non_accoppiato' }); return; }
    this.attivo = true;
    this.onStato({ tipo: 'avviato', device_id: this.stato.device_id, base: this.stato.base });
    let giro = 0, vuoti = 0;
    while (this.attivo) {
      try {
        if (giro % BATTITO_OGNI === 0) {
          await post(this.stato.base, '/heartbeat', {
            device_id: this.stato.device_id, token: this.stato.token,
            capabilities: await capacita(this.puoEseguire),
          });
        }
        const r = await post(this.stato.base, '/poll',
          { device_id: this.stato.device_id, token: this.stato.token });
        const comandi = (r && r.commands) || [];
        if (comandi.length === 0) { vuoti++; }
        else { vuoti = 0; for (const c of comandi) await this._eseguiUno(c); }
        giro++;
        await sleep(vuoti >= GIRI_PRIMA_DI_RALLENTARE ? PAUSA_RIPOSO : PAUSA_ATTIVA);
      } catch (e) {
        // qualunque cosa vada storta, si aspetta e si riprova.
        this.onStato({ tipo: 'errore', messaggio: String(e.message || e) });
        await sleep(PAUSA_RIPOSO);
      }
    }
  }

  ferma() { this.attivo = false; this.onStato({ tipo: 'fermato' }); }
}

function sleep(ms) { return new Promise((r) => setTimeout(r, ms)); }

module.exports = { Agent, capacita, claudeLoggato, claudeComando };
