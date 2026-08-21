/* SOWAI console capture — gira nel MAIN world a document_start su ogni pagina.
 * Bufferizza console.log/info/warn/error/debug + errori JS in window.__sowai_logs,
 * che il verbo browser_console_logs legge a richiesta. Buffer limitato. */
(function () {
    "use strict";
    if (window.__sowai_logs) { return; }
    window.__sowai_logs = [];
    var buf = window.__sowai_logs;
    function push(level, parts) {
        try {
            buf.push({
                level: level, ts: Date.now(),
                msg: Array.prototype.map.call(parts, function (a) {
                    try { return typeof a === "string" ? a : JSON.stringify(a); }
                    catch (e) { return String(a); }
                }).join(" ").slice(0, 1000),
            });
            if (buf.length > 500) { buf.splice(0, buf.length - 500); }
        } catch (e) { /* no-op */ }
    }
    ["log", "info", "warn", "error", "debug"].forEach(function (lvl) {
        var orig = console[lvl];
        console[lvl] = function () { push(lvl, arguments); return orig.apply(console, arguments); };
    });
    window.addEventListener("error", function (e) {
        push("error", [(e.message || "error") + " @ " + (e.filename || "") + ":" + (e.lineno || "")]);
    });
    window.addEventListener("unhandledrejection", function (e) {
        var r = e && e.reason;
        push("error", ["unhandledrejection: " + (r && r.message ? r.message : String(r))]);
    });
})();
