// modeac.net home page: live totals (/stats.json, refreshed every few minutes) and "your feed"
// (/api/myfeed: the feeds connected from this visitor's own internet address).
(function () {
  "use strict";

  function el(tag, cls, text) {
    var e = document.createElement(tag);
    if (cls) e.className = cls;
    if (text != null) e.textContent = text;
    return e;
  }
  function dur(s) {
    if (s < 90) return Math.round(s) + " s";
    if (s < 5400) return Math.round(s / 60) + " min";
    if (s < 172800) return (s / 3600).toFixed(1) + " h";
    return Math.round(s / 86400) + " days";
  }
  function getJson(url) {
    return fetch(url, { cache: "no-store" }).then(function (r) { if (!r.ok) throw new Error(r.status); return r.json(); });
  }

  // ---- totals ------------------------------------------------------------------------------------
  var stats = document.getElementById("stats");
  function showStats(d) {
    var items = [];
    if (typeof d.aircraft === "number") items.push([d.aircraft, "aircraft"]);
    items.push([d.feeders || 0, d.feeders === 1 ? "feeder" : "feeders"]);
    items.push([d.mlat_synced || 0, "MLAT receivers in sync"]);
    if (typeof d.modeac_targets === "number") items.push([d.modeac_targets, "located from Mode A/C"]);
    stats.textContent = "";
    items.forEach(function (it) {
      var li = el("li");
      li.appendChild(el("b", null, String(it[0])));
      li.appendChild(document.createTextNode(it[1]));
      stats.appendChild(li);
    });
  }
  function loadStats() { getJson("/stats.json").then(showStats).catch(function () {}); }

  // ---- your feed ---------------------------------------------------------------------------------
  var box = document.getElementById("myfeed");
  function line(state, text) {
    var p = el("p", "status-line");
    p.appendChild(el("span", "dot " + state));
    p.appendChild(el("span", null, text));
    return p;
  }
  function showMyFeed(d) {
    box.textContent = "";
    if (!d.feeds.length && !d.mlat.length) {
      box.appendChild(line("off", "We don't see a feed from your internet address (" + d.ip + ")."));
      box.appendChild(el("p", "muted", "Just set it up? Give it a minute and reload. If your receiver is on a different " +
        "network than this device, it won't show here."));
      var a = el("a", null, "How to start feeding");
      a.href = "/feed/";
      box.appendChild(a);
      return;
    }
    d.feeds.forEach(function (f) {
      box.appendChild(line(f.msgs > 0 ? "good" : "wait",
        "Map feed connected for " + dur(f.connected_s) + ": " + f.msgs + " messages/s, " + f.positions + " positions/s"));
    });
    if (!d.feeds.length) box.appendChild(line("wait", "No map feed from this address. Check step 2 of the setup guide."));
    d.mlat.forEach(function (m) {
      var text = "MLAT client “" + m.name + "”: ";
      if (m.bad_sync) box.appendChild(line("bad", text + "clock problem. Check its latitude, longitude and altitude."));
      else if (m.peers > 0) box.appendChild(line("good", text + "in sync with " + m.peers + (m.peers === 1 ? " receiver" : " receivers")));
      else box.appendChild(line("wait", text + "connected, waiting for another receiver that hears the same aircraft"));
    });
    if (!d.mlat.length) box.appendChild(line("wait", "No modeac.net MLAT client from this address. Check step 3 of the setup guide."));
  }
  function loadMyFeed() {
    getJson("/api/myfeed").then(showMyFeed).catch(function () {
      box.textContent = "";
      box.appendChild(el("p", "muted", "The feed check isn't available right now. Try again in a minute."));
    });
  }

  if (stats) { loadStats(); setInterval(loadStats, 60000); }
  if (box) { loadMyFeed(); setInterval(loadMyFeed, 30000); }
})();
