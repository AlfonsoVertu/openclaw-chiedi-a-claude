async function load() {
    const c = await chrome.storage.local.get(["base", "device_id"]);
    if (c.base) { document.getElementById("base").value = c.base; }
    if (c.device_id) {
        document.getElementById("status").innerHTML =
            '<span class="ok">Accoppiato (device ' + c.device_id + ')</span>';
    }
}
async function installId() {
    const c = await chrome.storage.local.get(["install_id"]);
    if (c.install_id) { return c.install_id; }
    const id = (typeof crypto !== "undefined" && crypto.randomUUID)
        ? crypto.randomUUID()
        : (Date.now().toString(36) + Math.random().toString(36).slice(2, 10));
    await chrome.storage.local.set({ install_id: id });
    return id;
}
document.getElementById("auto").addEventListener("click", async function () {
    const st = document.getElementById("status");
    st.textContent = "Cerco una sessione Odoo aperta...";
    const iid = await installId();
    const tabs = await chrome.tabs.query({ url: ["*://*/pos/ui*", "*://*/odoo*", "*://*/web*", "*://*/web"] });
    for (const t of tabs) {
        try {
            const [r] = await chrome.scripting.executeScript({
                target: { tabId: t.id },
                args: [iid],
                func: async function (iid) {
                    try { const resp = await fetch("/odoo-gpt/pos-agent/auto_pair", { method: "POST", credentials: "same-origin", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ install_id: iid }) }); return await resp.json(); }
                    catch (e) { return { ok: false, error: String(e) }; }
                },
            });
            const res = r && r.result;
            if (res && res.ok && res.token) {
                await chrome.storage.local.set({ base: res.base, device_id: res.device_id, token: res.token });
                st.innerHTML = '<span class="ok">Auto-accoppiato! device ' + res.device_id + ' @ ' + res.base + '</span>';
                return;
            }
        } catch (e) { /* prova la prossima tab */ }
    }
    st.innerHTML = '<span class="err">Nessuna sessione Odoo loggata trovata. Apri il POS/backend e riprova, o usa il manuale.</span>';
});

document.getElementById("pair").addEventListener("click", async function () {
    const base = document.getElementById("base").value.trim().replace(/\/$/, "");
    const code = document.getElementById("code").value.trim();
    const st = document.getElementById("status");
    if (!base || !code) { st.innerHTML = '<span class="err">Compila server e codice.</span>'; return; }
    st.textContent = "Accoppiamento...";
    try {
        const r = await fetch(base + "/odoo-gpt/pos-agent/pair", {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ pairing_code: code, capabilities: { tabs: true, screenshot: true, full_browser: true } }),
        });
        const res = await r.json();
        if (res && res.ok) {
            await chrome.storage.local.set({ base: base, device_id: res.device_id, token: res.token });
            st.innerHTML = '<span class="ok">Accoppiato! device ' + res.device_id + '</span>';
        } else {
            st.innerHTML = '<span class="err">' + ((res && res.error) || "Errore") + '</span>';
        }
    } catch (e) {
        st.innerHTML = '<span class="err">' + String(e) + '</span>';
    }
});
load();
