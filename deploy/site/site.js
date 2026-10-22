// modeac.net: live totals on the home page (from /stats.json, refreshed by the server every few minutes)
(function () {
  "use strict";
  var el = document.getElementById("live");
  if (!el) return;

  function plural(n, one, many) { return n + " " + (n === 1 ? one : many); }
  function ago(s) {
    if (s < 90) return "just now";
    if (s < 5400) return Math.round(s / 60) + " min ago";
    return Math.round(s / 3600) + " h ago";
  }
  function b(text) { var x = document.createElement("b"); x.textContent = text; return x; }

  function show(d) {
    el.textContent = "";
    var parts = [b(plural(d.feeders || 0, "receiver", "receivers")), document.createTextNode(" feeding, "),
                 b(String(d.mlat_synced || 0)), document.createTextNode(" in sync for MLAT")];
    if (typeof d.aircraft === "number") {
      parts.push(document.createTextNode(", "), b(plural(d.aircraft, "aircraft", "aircraft")),
                 document.createTextNode(" on the map"));
    }
    if (typeof d.modeac_targets === "number") {
      parts.push(document.createTextNode(", "), b(String(d.modeac_targets)),
                 document.createTextNode(" located from Mode A/C replies"));
    }
    parts.push(document.createTextNode(". Updated " + ago(Date.now() / 1000 - d.now) + "."));
    el.appendChild(document.createTextNode("Right now: "));
    parts.forEach(function (p) { el.appendChild(p); });
  }

  function load() {
    fetch("/stats.json", { cache: "no-store" })
      .then(function (r) { if (!r.ok) throw new Error(r.status); return r.json(); })
      .then(show)
      .catch(function () { el.textContent = "Live numbers aren't available right now. The map may still be working."; });
  }
  load();
  setInterval(load, 60000);
})();
