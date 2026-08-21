/* SOWAI Browser Agent — service worker MV3.
 * Stesso protocollo del POS agent (poll/result/heartbeat, token-auth) ma controlla
 * TUTTO il browser: tutte le tab, navigazione, screenshot pixel reali.
 */
const POLL_MS = 1000;

async function cfg() {
    return await chrome.storage.local.get(["base", "device_id", "token"]);
}
// ID univoco PER INSTALLAZIONE (browser): distingue device diversi dello stesso
// utente. Generato una volta e persistito in chrome.storage.
async function installId() {
    const c = await chrome.storage.local.get(["install_id"]);
    if (c.install_id) { return c.install_id; }
    const id = (typeof crypto !== "undefined" && crypto.randomUUID)
        ? crypto.randomUUID()
        : (Date.now().toString(36) + Math.random().toString(36).slice(2, 10));
    await chrome.storage.local.set({ install_id: id });
    return id;
}
async function api(path, body) {
    const c = await cfg();
    if (!c.base || !c.token) { return null; }
    const url = c.base.replace(/\/$/, "") + "/odoo-gpt/pos-agent/" + path;
    const r = await fetch(url, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(Object.assign({ device_id: c.device_id, token: c.token }, body || {})),
    });
    return r.json();
}
async function activeTab(tabId) {
    if (tabId) { return await chrome.tabs.get(tabId); }
    const tabs = await chrome.tabs.query({ active: true, lastFocusedWindow: true });
    return tabs[0];
}

// Esegue una promise con timeout: se scade, ritorna un risultato d'errore invece
// di restare appesa. Un comando bloccato (es. cattura non disponibile) NON deve
// fermare la coda degli altri comandi.
function withTimeout(promise, ms, label) {
    return Promise.race([
        promise,
        new Promise(function (resolve) {
            setTimeout(function () {
                resolve({ status: "error", error: "timeout " + (label || "") + " dopo " + ms + "ms" });
            }, ms);
        }),
    ]);
}

// ── Cattura network (per browser_network): buffer per tab via webRequest ──
const netLog = {};
function netPush(tabId, item) {
    if (tabId == null || tabId < 0) { return; }
    if (!netLog[tabId]) { netLog[tabId] = []; }
    const a = netLog[tabId];
    a.push(item);
    if (a.length > 200) { a.splice(0, a.length - 200); }
}
try {
    chrome.webRequest.onCompleted.addListener(function (d) {
        netPush(d.tabId, { url: String(d.url).slice(0, 300), method: d.method, status: d.statusCode, type: d.type, ts: d.timeStamp });
    }, { urls: ["<all_urls>"] });
    chrome.webRequest.onErrorOccurred.addListener(function (d) {
        netPush(d.tabId, { url: String(d.url).slice(0, 300), method: d.method, status: 0, type: d.type, error: d.error, ts: d.timeStamp });
    }, { urls: ["<all_urls>"] });
    chrome.tabs.onRemoved.addListener(function (tabId) { delete netLog[tabId]; });
} catch (e) { /* webRequest non disponibile */ }

// Eseguita NEL CONTESTO DELLA PAGINA (self-contained).
function pageExec(action, args) {
    function findEl(a) {
        const sel = a.selector || "*";
        if (a.text) {
            const els = Array.prototype.slice.call(document.querySelectorAll(sel));
            const w = String(a.text).trim();
            return els.find(function (e) { return (e.innerText || "").trim().includes(w); }) || null;
        }
        return document.querySelector(sel);
    }
    try {
        if (action === "read") {
            const el = document.querySelector(args.selector || "body");
            return { status: "done", result: { text: el ? (el.innerText || "").slice(0, 5000) : null } };
        }
        if (action === "get_value") {
            const el = document.querySelector(args.selector);
            if (!el) { return { status: "error", error: "Campo non trovato" }; }
            return { status: "done", result: { value: el.value != null ? el.value : el.getAttribute("value"), checked: el.checked } };
        }
        if (action === "list_elements") {
            const els = Array.prototype.slice.call(document.querySelectorAll(args.selector || "button, a")).slice(0, 80);
            return { status: "done", result: { elements: els.map(function (e, i) {
                return { i: i, tag: e.tagName, text: (e.innerText || "").trim().slice(0, 60), id: e.id || null, cls: (e.className || "").toString().slice(0, 60) };
            }).filter(function (x) { return x.text || x.id; }) } };
        }
        if (action === "click" || action === "pos_add_product") {
            const a2 = action === "pos_add_product" ? { selector: "article.product, .product", text: args.name } : args;
            const el = findEl(a2);
            if (!el) { return { status: "error", error: "Elemento non trovato" }; }
            const qty = action === "pos_add_product" ? Math.max(1, args.qty || 1) : 1;
            for (let i = 0; i < qty; i++) { el.scrollIntoView({ block: "center" }); el.click(); }
            return { status: "done", result: { clicked: true, text: (el.innerText || "").slice(0, 80) } };
        }
        if (action === "fill") {
            const el = document.querySelector(args.selector);
            if (!el) { return { status: "error", error: "Campo non trovato" }; }
            el.focus(); el.value = args.value != null ? args.value : "";
            el.dispatchEvent(new Event("input", { bubbles: true }));
            el.dispatchEvent(new Event("change", { bubbles: true }));
            return { status: "done", result: { filled: true } };
        }
        if (action === "press") {
            const el = args.selector ? document.querySelector(args.selector) : document.activeElement;
            const key = args.key || "Enter";
            ["keydown", "keypress", "keyup"].forEach(function (t) {
                el.dispatchEvent(new KeyboardEvent(t, { key: key, bubbles: true })); });
            return { status: "done", result: { pressed: key } };
        }
        if (action === "scroll") {
            if (args.selector) { const el = document.querySelector(args.selector); if (el) el.scrollIntoView({ block: "center" }); }
            else if (args.to === "top") { window.scrollTo(0, 0); }
            else if (args.to === "bottom") { window.scrollTo(0, document.body.scrollHeight); }
            return { status: "done", result: { scrolled: true } };
        }
        if (action === "select") {
            const el = document.querySelector(args.selector);
            if (!el) { return { status: "error", error: "Select non trovato" }; }
            if (args.label) { const o = Array.prototype.slice.call(el.options).find(function (x) { return (x.text || "").trim() === args.label; }); if (o) el.value = o.value; }
            else { el.value = args.value; }
            el.dispatchEvent(new Event("change", { bubbles: true }));
            return { status: "done", result: { value: el.value } };
        }
        if (action === "pos_pay") {
            const el = document.querySelector(".pay-order-button, .pay");
            if (!el) { return { status: "error", error: "Pulsante pagamento non trovato" }; }
            el.click(); return { status: "done", result: { pay_clicked: true } };
        }
        if (action === "pos_validate") {
            const el = document.querySelector(".button.next, .next.validation, button.validate, .pay-order-button");
            if (!el) { return { status: "error", error: "Pulsante Valida non trovato" }; }
            el.click(); return { status: "done", result: { validated: true } };
        }
        if (action === "pos_select_payment") {
            const el = findEl({ selector: ".paymentmethod, .payment-method, button", text: args.method });
            if (!el) { return { status: "error", error: "Metodo non trovato" }; }
            el.click(); return { status: "done", result: { method: args.method } };
        }
        return { status: "error", error: "Azione DOM non supportata: " + action };
    } catch (e) {
        return { status: "error", error: String(e).slice(0, 300) };
    }
}

const TAB_OPS = ["ping", "observe", "list_tabs", "new_tab", "switch_tab", "close_tab", "goto", "screenshot", "wait_for"];

async function execute(cmd) {
    const a = cmd.action, args = cmd.args || {};
    try {
        if (a === "ping") { const t = await activeTab(args.tab_id); return { status: "done", result: { pong: true, url: t && t.url, tab_id: t && t.id } }; }
        if (a === "observe") { const t = await activeTab(args.tab_id); return { status: "done", result: { url: t.url, title: t.title, tab_id: t.id } }; }
        if (a === "list_tabs") {
            const ts = await chrome.tabs.query({});
            return { status: "done", result: { tabs: ts.map(function (t) { return { id: t.id, title: t.title, url: t.url, active: t.active }; }) } };
        }
        if (a === "new_tab") { const t = await chrome.tabs.create({ url: args.url || "about:blank" }); return { status: "done", result: { tab_id: t.id } }; }
        if (a === "switch_tab") { await chrome.tabs.update(args.tab_id, { active: true }); return { status: "done", result: { switched: args.tab_id } }; }
        if (a === "close_tab") { await chrome.tabs.remove(args.tab_id); return { status: "done", result: { closed: args.tab_id } }; }
        if (a === "goto") { const t = await activeTab(args.tab_id); await chrome.tabs.update(t.id, { url: args.url }); return { status: "done", result: { url: args.url } }; }
        if (a === "wait_for") {
            const cond = args.condition || "present";
            const deadline = Date.now() + (args.timeout_ms || 5000);
            const t = await activeTab(args.tab_id);
            while (Date.now() < deadline) {
                const [r] = await chrome.scripting.executeScript({
                    target: { tabId: t.id },
                    args: [cond, args.selector || "", args.text || "", args.url_contains || ""],
                    func: function (cond, sel, text, urlc) {
                        if (cond === "absent") { return !(sel && document.querySelector(sel)); }
                        if (cond === "text") { return (document.body && document.body.innerText || "").includes(text); }
                        if (cond === "url_contains") { return location.href.includes(urlc); }
                        return !!(sel && document.querySelector(sel)); // present (default)
                    },
                });
                if (r && r.result) { return { status: "done", result: { met: true, condition: cond } }; }
                await new Promise(function (x) { setTimeout(x, 250); });
            }
            return { status: "done", result: { met: false, condition: cond } };
        }
        if (a === "screenshot" && args.full_page) {
            // Full page: html2canvas dell'intero documentElement (oltre il viewport).
            const tf = await activeTab(args.tab_id);
            try {
                await chrome.scripting.executeScript({ target: { tabId: tf.id }, files: ["html2canvas.min.js"] });
                const [r] = await chrome.scripting.executeScript({
                    target: { tabId: tf.id },
                    func: async function () {
                        if (!window.html2canvas) { return { err: "html2canvas non iniettato" }; }
                        try {
                            const h = document.documentElement.scrollHeight;
                            const c = await window.html2canvas(document.documentElement, { logging: false, useCORS: true, scale: 1, height: h, windowHeight: h });
                            return { data: c.toDataURL("image/png") };
                        } catch (e) { return { err: String(e).slice(0, 150) }; }
                    },
                });
                const r0 = r && r.result;
                if (r0 && r0.data) { return { status: "done", result: { url: tf.url, tab_id: tf.id, method: "html2canvas_full" }, screenshot_b64: r0.data }; }
                return { status: "error", error: "fullpage screenshot: " + ((r0 && r0.err) || "fallito") };
            } catch (e) { return { status: "error", error: "fullpage: " + String(e).slice(0, 150) + " (pagine chrome:// non catturabili)" }; }
        }
        if (a === "screenshot") {
            const t = await activeTab(args.tab_id);
            // race con timeout: ritorna "__TO__" se la promise non si risolve in ms.
            const TO = function (p, ms) {
                return Promise.race([p, new Promise(function (r) { setTimeout(function () { r("__TO__"); }, ms); })]);
            };
            // 1) PIXEL via captureVisibleTab, MAX 5s. Porta la finestra in primo piano
            //    (aiuta la cattura) e attiva la tab target.
            try {
                try { await chrome.windows.update(t.windowId, { focused: true }); } catch (e) { /* no-op */ }
                try { await chrome.tabs.update(t.id, { active: true }); } catch (e) { /* no-op */ }
                const cap = await TO(chrome.tabs.captureVisibleTab(t.windowId, { format: "png" }), 5000);
                if (cap && cap !== "__TO__") {
                    return { status: "done", result: { url: t.url, tab_id: t.id, method: "pixel" }, screenshot_b64: cap };
                }
            } catch (e) { /* pixel non disponibile → fallback */ }
            // 2) FALLBACK html2canvas: inietta la lib e renderizza il DOM. MAX 14s.
            //    Funziona su qualsiasi tab web anche non in primo piano.
            try {
                const inj = await TO(chrome.scripting.executeScript(
                    { target: { tabId: t.id }, files: ["html2canvas.min.js"] }), 5000);
                if (inj === "__TO__") {
                    return { status: "error", error: "screenshot stage=inject_timeout (5s) su " + t.url };
                }
                const exec = chrome.scripting.executeScript({
                    target: { tabId: t.id },
                    func: async function () {
                        if (!window.html2canvas) { return { err: "html2canvas non iniettato (pagina protetta?)" }; }
                        try {
                            const c = await window.html2canvas(document.body, { logging: false, useCORS: true, scale: 1 });
                            return { data: c.toDataURL("image/png") };
                        } catch (e) { return { err: String(e).slice(0, 150) }; }
                    },
                });
                const res = await TO(exec, 14000);
                if (res === "__TO__") {
                    return { status: "error", error: "screenshot stage=html2canvas_timeout (14s) su " + t.url };
                }
                const r0 = res && res[0] && res[0].result;
                if (r0 && r0.data) {
                    return { status: "done", result: { url: t.url, tab_id: t.id, method: "html2canvas" }, screenshot_b64: r0.data };
                }
                return { status: "error", error: "screenshot stage=html2canvas_failed: " + ((r0 && r0.err) || "inject_failed") };
            } catch (e) {
                return { status: "error", error: "screenshot stage=capture_failed: " + String(e).slice(0, 130) + " (pagine chrome:// non catturabili: usa tab_id di una tab web)" };
            }
        }
        if (a === "eval") {
            // Esegue JS arbitrario nella tab (modifica DOM realtime, console, ecc.).
            // world ISOLATED (default, ignora la CSP della pagina) o MAIN (contesto
            // pagina, accede a window.* del sito ma soggetto a CSP).
            const te = await activeTab(args.tab_id);
            const world = args.world === "MAIN" ? "MAIN" : "ISOLATED";
            const [r] = await chrome.scripting.executeScript({
                target: { tabId: te.id }, world: world, args: [String(args.code || "")],
                func: async function (code) {
                    try {
                        const fn = new Function("return (async () => { " + code + " })();");
                        let v = await fn();
                        try { JSON.stringify(v); } catch (e) { v = String(v); }
                        return { ok: true, value: v, jstype: typeof v };
                    } catch (e) { return { ok: false, error: String(e).slice(0, 400) }; }
                },
            });
            const rr = r && r.result;
            if (!rr) { return { status: "error", error: "eval: nessun risultato (pagina non iniettabile, es. chrome://)" }; }
            if (rr.ok) { return { status: "done", result: { value: rr.value, jstype: rr.jstype, tab_id: te.id, world: world } }; }
            return { status: "error", error: "eval: " + rr.error };
        }
        if (a === "storage") {
            // Legge cookies (non-httpOnly), localStorage e sessionStorage della tab.
            const ts = await activeTab(args.tab_id);
            const [r] = await chrome.scripting.executeScript({
                target: { tabId: ts.id },
                func: function () {
                    function dump(s) { const o = {}; try { for (let i = 0; i < s.length; i++) { const k = s.key(i); o[k] = s.getItem(k); } } catch (e) {} return o; }
                    return { url: location.href, cookies: document.cookie || "", localStorage: dump(window.localStorage), sessionStorage: dump(window.sessionStorage) };
                },
            });
            return { status: "done", result: (r && r.result) || {} };
        }
        if (a === "console_logs") {
            // Legge il buffer dei log/errori catturati dal content script (MAIN world).
            const tc = await activeTab(args.tab_id);
            const [r] = await chrome.scripting.executeScript({
                target: { tabId: tc.id }, world: "MAIN",
                func: function () { return (window.__sowai_logs || []).slice(-200); },
            });
            return { status: "done", result: { logs: (r && r.result) || [], tab_id: tc.id } };
        }
        if (a === "dom_snapshot") {
            const td = await activeTab(args.tab_id);
            const [r] = await chrome.scripting.executeScript({
                target: { tabId: td.id },
                args: [args.selector || "a, button, input, select, textarea, [role], [onclick]", args.limit || 150],
                func: function (sel, limit) {
                    function cssPath(el) {
                        if (el.id) { return "#" + CSS.escape(el.id); }
                        const parts = [];
                        while (el && el.nodeType === 1 && parts.length < 5 && el.tagName !== "BODY") {
                            let s = el.tagName.toLowerCase();
                            const p = el.parentNode;
                            if (p) {
                                const sibs = Array.prototype.filter.call(p.children, function (x) { return x.tagName === el.tagName; });
                                if (sibs.length > 1) { s += ":nth-of-type(" + (sibs.indexOf(el) + 1) + ")"; }
                            }
                            parts.unshift(s); el = el.parentNode;
                        }
                        return parts.join(" > ");
                    }
                    const out = [];
                    const els = document.querySelectorAll(sel);
                    for (let i = 0; i < els.length && out.length < limit; i++) {
                        const e = els[i]; const b = e.getBoundingClientRect();
                        if (b.width === 0 && b.height === 0) { continue; }
                        out.push({
                            selector: cssPath(e), tag: e.tagName.toLowerCase(),
                            text: (e.innerText || e.value || e.getAttribute("aria-label") || "").trim().slice(0, 80),
                            role: e.getAttribute("role") || null, href: e.getAttribute("href") || null,
                            name: e.getAttribute("name") || null, type: e.getAttribute("type") || null,
                            bbox: { x: Math.round(b.x), y: Math.round(b.y), w: Math.round(b.width), h: Math.round(b.height) },
                        });
                    }
                    return { url: location.href, title: document.title, viewport: { width: innerWidth, height: innerHeight }, count: out.length, elements: out };
                },
            });
            return { status: "done", result: (r && r.result) || {} };
        }
        if (a === "click_xy") {
            const tx = await activeTab(args.tab_id);
            const [r] = await chrome.scripting.executeScript({
                target: { tabId: tx.id }, args: [Number(args.x) || 0, Number(args.y) || 0],
                func: function (x, y) {
                    const el = document.elementFromPoint(x, y);
                    if (!el) { return { ok: false, error: "nessun elemento a (" + x + "," + y + ")" }; }
                    ["mousedown", "mouseup", "click"].forEach(function (t) {
                        el.dispatchEvent(new MouseEvent(t, { bubbles: true, cancelable: true, clientX: x, clientY: y, view: window }));
                    });
                    return { ok: true, tag: el.tagName, text: (el.innerText || "").slice(0, 60) };
                },
            });
            const rr = r && r.result;
            return rr && rr.ok ? { status: "done", result: rr } : { status: "error", error: (rr && rr.error) || "click_xy fallito" };
        }
        if (a === "network") {
            const tn = await activeTab(args.tab_id);
            const items = (netLog[tn.id] || []).slice(-(args.limit || 100));
            const errors = items.filter(function (x) { return x.error || (x.status >= 400); });
            return { status: "done", result: { tab_id: tn.id, count: items.length, errors: errors, requests: items } };
        }
        // DOM ops → inietta pageExec nella tab target
        const t = await activeTab(args.tab_id);
        const [res] = await chrome.scripting.executeScript({ target: { tabId: t.id }, func: pageExec, args: [a, args] });
        return res && res.result ? res.result : { status: "error", error: "nessun risultato dall'iniezione" };
    } catch (e) {
        return { status: "error", error: String(e).slice(0, 300) };
    }
}

// Auto-accoppiamento: trova una tab Odoo aperta e usa la sessione loggata (same-origin).
async function ensurePaired() {
    const c = await cfg();
    if (c.token) { return true; }
    let tabs = [];
    try {
        tabs = await chrome.tabs.query({ url: ["*://*/pos/ui*", "*://*/odoo*", "*://*/web*", "*://*/web"] });
    } catch (e) { return false; }
    const iid = await installId();
    for (const t of tabs) {
        try {
            const [r] = await chrome.scripting.executeScript({
                target: { tabId: t.id },
                args: [iid],
                func: async function (iid) {
                    try {
                        const resp = await fetch("/odoo-gpt/pos-agent/auto_pair", {
                            method: "POST", credentials: "same-origin",
                            headers: { "Content-Type": "application/json" },
                            body: JSON.stringify({ install_id: iid }),
                        });
                        return await resp.json();
                    } catch (e) { return { ok: false, error: String(e) }; }
                },
            });
            const res = r && r.result;
            if (res && res.ok && res.token) {
                await chrome.storage.local.set({ base: res.base, device_id: res.device_id, token: res.token });
                console.info("[SOWAI] auto-accoppiato:", res.device_id, "@", res.base);
                return true;
            }
        } catch (e) { /* tab non-Odoo o non loggata, prova la prossima */ }
    }
    return false;
}

async function tick() {
    let c = await cfg();
    if (!c.token) { await ensurePaired(); c = await cfg(); }
    if (!c.token) { return; }
    let res;
    try { res = await api("poll", {}); } catch (e) { return; }
    if (!res || !res.ok || !res.commands || !res.commands.length) { return; }
    for (const cmd of res.commands) {
        let out;
        const tmo = cmd.action === "screenshot" ? 30000 : 20000;
        try { out = await withTimeout(execute(cmd), tmo, cmd.action); }
        catch (e) { out = { status: "error", error: String(e).slice(0, 300) }; }
        try { await api("result", Object.assign({ corr_id: cmd.corr_id }, out)); } catch (e) { /* retry next poll */ }
    }
}
async function heartbeat() {
    try { await api("heartbeat", { current_url: "(extension)", capabilities: { tabs: true, screenshot: true, full_browser: true } }); } catch (e) { }
}

// Mantieni vivo il SW e poll veloce mentre attivo.
chrome.alarms.create("sowai-poll", { periodInMinutes: 0.5 });
chrome.alarms.onAlarm.addListener(function (al) { if (al.name === "sowai-poll") { ensurePaired().then(tick); heartbeat(); } });
chrome.runtime.onStartup.addListener(function () { ensurePaired().then(heartbeat); });
chrome.runtime.onInstalled.addListener(function () { ensurePaired(); });
(function loop() { tick().finally(function () { setTimeout(loop, POLL_MS); }); })();
ensurePaired().then(heartbeat);
