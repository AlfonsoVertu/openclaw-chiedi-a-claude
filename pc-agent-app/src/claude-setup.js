// -*- coding: utf-8 -*-
// Il flusso Claude dentro l'app: c'e'? installalo. E' loggato? falgli fare il
// login. Cosi' l'utente non apre mai un terminale nero - fa tutto dalla
// finestra, come nell'app di ChatGPT.
//
// PERCHE' L'UTENTE FA IL LOGIN, NON L'APP. L'abbonamento Claude e' personale.
// Ogni PC usa il proprio: e' il modo in cui ogni tenant ha il suo Claude senza
// toccare quello di nessun altro. L'app apre il browser sul login e aspetta;
// le credenziali non passano mai da noi.

const { execFile, spawn } = require('child_process');
const os = require('os');

const CLAUDE = process.platform === 'win32' ? 'claude.cmd' : 'claude';
const NPM = process.platform === 'win32' ? 'npm.cmd' : 'npm';
const shell = process.platform === 'win32';

function eseguito(exe, args, opz = {}) {
  return new Promise((resolve) => {
    execFile(exe, args, { shell, encoding: 'utf8', timeout: opz.timeout || 60000,
                          maxBuffer: 20 * 1024 * 1024, ...opz },
      (err, stdout, stderr) => resolve({ err, stdout: stdout || '', stderr: stderr || '' }));
  });
}

// C'e' Claude? E' loggato? Ritorna { installato, loggato }.
async function verificaClaude() {
  const v = await eseguito(CLAUDE, ['--version'], { timeout: 20000 });
  const installato = !v.err && /\d+\.\d+/.test(v.stdout);
  if (!installato) return { installato: false, loggato: false };
  const p = await eseguito(CLAUDE, ['-p', 'rispondi solo: ok'], { timeout: 60000 });
  return { installato: true, loggato: !p.err && !!p.stdout.trim() };
}

// Installa Claude Code via npm. Manda le righe di avanzamento alla finestra,
// cosi' l'utente vede che sta lavorando invece di un pulsante muto.
function installaClaude(onRiga) {
  return new Promise((resolve) => {
    const p = spawn(NPM, ['install', '-g', '@anthropic-ai/claude-code'], { shell });
    const inoltra = (buf) => String(buf).split('\n').forEach((r) => r.trim() && onRiga(r.trim()));
    p.stdout.on('data', inoltra);
    p.stderr.on('data', inoltra);   // npm scrive l'avanzamento su stderr
    p.on('close', (code) => resolve({ ok: code === 0, codice: code }));
    p.on('error', (e) => resolve({ ok: false, errore: e.message }));
  });
}

// Il login: apre il flusso di autenticazione di Claude. `claude auth` su un
// terminale interattivo apre il browser; qui lo lanciamo in una finestra di
// console visibile, perche' il login VUOLE che l'utente incolli o confermi
// qualcosa, e forzarlo dentro l'app lo renderebbe piu' fragile, non piu'
// semplice. Quando il login usa il browser, l'utente torna e la verifica
// successiva conferma da sola.
function loginClaude() {
  return new Promise((resolve) => {
    if (process.platform === 'win32') {
      // apre una finestra cmd che lancia `claude auth`: l'utente vede il
      // prompt, accede, e chiude. Poi l'app riverifica.
      spawn('cmd.exe', ['/c', 'start', 'cmd', '/k', 'claude', 'auth'], { shell: true });
    } else {
      // su Linux prova il terminale piu' comune; se non c'e', l'utente lancia a mano.
      const term = process.env.TERMINAL || 'x-terminal-emulator';
      spawn(term, ['-e', 'claude auth'], { detached: true }).on('error', () => {
        spawn('xterm', ['-e', 'claude auth'], { detached: true }).on('error', () => {});
      });
    }
    // non aspettiamo la fine del terminale: la finestra ricontrollera' lo stato
    // col pulsante "Ho fatto l'accesso".
    resolve({ ok: true, avviato: true });
  });
}

module.exports = { verificaClaude, installaClaude, loginClaude };
