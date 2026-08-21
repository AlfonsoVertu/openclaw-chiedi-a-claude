// Ponte sicuro fra la finestra e il processo principale. contextIsolation e'
// acceso: la pagina non tocca Node direttamente, passa solo da qui, e vede
// solo le funzioni che le esponiamo. Niente di piu'.
const { contextBridge, ipcRenderer } = require('electron');

contextBridge.exposeInMainWorld('sowai', {
  statoAttuale: () => ipcRenderer.invoke('stato-attuale'),
  accoppia: (base, codice) => ipcRenderer.invoke('accoppia', { base, codice }),
  scollega: () => ipcRenderer.invoke('scollega'),
  avvia: () => ipcRenderer.invoke('avvia'),
  ferma: () => ipcRenderer.invoke('ferma'),
  claudeStato: () => ipcRenderer.invoke('claude-stato'),
  claudeInstalla: () => ipcRenderer.invoke('claude-installa'),
  claudeLogin: () => ipcRenderer.invoke('claude-login'),
  suStato: (cb) => ipcRenderer.on('stato', (_e, s) => cb(s)),
  suClaudeLog: (cb) => ipcRenderer.on('claude-log', (_e, r) => cb(r)),
});
