/* app.js — WSP 3.0 Operator Console — read-only. GET only, same-origin, CSP-safe.
   Renders /api/v3/overview, /api/v3/receipts, /api/v3/benchmark-history. */
(function () {
  "use strict";

  var API = {
    overview: "/api/v3/overview",
    receipts: function (n) { return "/api/v3/receipts?limit=" + encodeURIComponent(n); },
    bench: function (n) { return "/api/v3/benchmark-history?limit=" + encodeURIComponent(n); }
  };

  var state = { view: "overview", loading: false };

  /* ---------- helpers ---------- */

  function $(id) { return document.getElementById(id); }

  function esc(v) {
    return String(v == null ? "" : v).replace(/[&<>"']/g, function (c) {
      return { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c];
    });
  }

  function fmtTs(sec) {
    if (sec == null) return "—";
    var d = new Date(sec * 1000);
    if (isNaN(d.getTime())) return "—";
    return d.toLocaleString(undefined, {
      year: "numeric", month: "2-digit", day: "2-digit",
      hour: "2-digit", minute: "2-digit", second: "2-digit"
    });
  }

  function fmtBytes(b) {
    if (b == null) return "—";
    if (b < 1024) return b + " B";
    if (b < 1048576) return (b / 1024).toFixed(1) + " KiB";
    if (b < 1073741824) return (b / 1048576).toFixed(1) + " MiB";
    return (b / 1073741824).toFixed(2) + " GiB";
  }

  function fmtInt(n) { return n == null ? "—" : Number(n).toLocaleString("en-US"); }

  function fmtSecondsTtl(s) {
    if (s == null) return "—";
    if (s % 86400 === 0) return (s / 86400) + " d";
    if (s % 3600 === 0) return (s / 3600) + " h";
    return s + " s";
  }

  function fmtLatency(sec) {
    if (sec == null) return "—";
    var ms = sec * 1000;
    return ms < 1000 ? Math.round(ms) + " ms" : (sec).toFixed(2) + " s";
  }

  function fmtRate(r) { return r == null ? "—" : (r * 100).toFixed(1) + " %"; }

  function badge(text, tone) {
    return '<span class="badge b-' + tone + '">' + esc(text) + "</span>";
  }

  function chip(text) { return '<span class="chip">' + esc(text) + "</span>"; }

  function toneForDecision(decision) {
    var d = String(decision || "").toLowerCase();
    if (d.indexOf("select") >= 0 || d.indexOf("success") >= 0 || d.indexOf("used") >= 0) return "ok";
    if (d.indexOf("fail") >= 0 || d.indexOf("error") >= 0 || d.indexOf("block") >= 0) return "bad";
    if (d.indexOf("cool") >= 0 || d.indexOf("skip") >= 0 || d.indexOf("fallback") >= 0) return "warn";
    return "neutral";
  }

  function toneForStatus(status, ok) {
    if (ok === true) return "ok";
    if (ok === false) return "bad";
    var s = String(status || "").toLowerCase();
    if (s.indexOf("ok") >= 0 || s.indexOf("success") >= 0 || s.indexOf("complete") >= 0) return "ok";
    if (s.indexOf("fail") >= 0 || s.indexOf("error") >= 0) return "bad";
    if (s.indexOf("partial") >= 0 || s.indexOf("degrad") >= 0 || s.indexOf("warn") >= 0) return "warn";
    return "neutral";
  }

  function showError(msg) {
    var b = $("errorBanner");
    b.textContent = msg;
    b.classList.remove("hidden");
  }
  function clearError() { $("errorBanner").classList.add("hidden"); }

  function getJson(url) {
    return fetch(url, { method: "GET", credentials: "same-origin", headers: { "Accept": "application/json" } })
      .then(function (res) {
        if (!res.ok) throw new Error("HTTP " + res.status + " on " + url);
        return res.json();
      });
  }

  function stampUpdated() {
    $("lastUpdated").textContent = "updated " + new Date().toLocaleTimeString();
  }

  /* ---------- overview ---------- */

  function providerReadiness(p) {
    if (p.disabled) return badge("disabled", "neutral");
    if (!p.configured) return badge("not configured", "bad");
    if (!p.key_present) return badge("no key", "bad");
    if (p.cooldown_active) return badge("cooldown", "warn");
    return badge("ready", "ok");
  }

  function renderOverview(d) {
    var eng = d.engine || {};
    $("engineBadge").textContent =
      "contract " + (eng.contract_version || "?") + " · plugin " + (eng.plugin_version || "?");
    $("schemaNote").textContent = "overview schema v" + (d.schema_version != null ? d.schema_version : "?");

    var html = "";

    /* engine + circuits */
    var c = d.circuits || {};
    html += '<div class="grid-2">';
    html += '<div class="card"><h2>Engine</h2><dl class="kv">' +
      "<dt>Contract version</dt><dd>" + esc(eng.contract_version || "—") + "</dd>" +
      "<dt>Plugin version</dt><dd>" + esc(eng.plugin_version || "—") + "</dd>" +
      "<dt>State store</dt><dd>" + (eng.state_available ? badge("available", "ok") : badge("unavailable", "bad")) + "</dd>" +
      "</dl></div>";
    html += '<div class="card"><h2>Circuits</h2><div class="tiles">' +
      '<div class="tile t-ok"><div class="n">' + fmtInt(c.closed) + '</div><div class="l">closed</div></div>' +
      '<div class="tile ' + (c.open > 0 ? "t-bad" : "t-neutral") + '"><div class="n">' + fmtInt(c.open) + '</div><div class="l">open</div></div>' +
      '<div class="tile ' + (c.blocked_auth > 0 ? "t-bad" : "t-neutral") + '"><div class="n">' + fmtInt(c.blocked_auth) + '</div><div class="l">blocked auth</div></div>' +
      '<div class="tile ' + (c.blocked_quota > 0 ? "t-warn" : "t-neutral") + '"><div class="n">' + fmtInt(c.blocked_quota) + '</div><div class="l">blocked quota</div></div>' +
      '<div class="tile t-neutral"><div class="n">' + fmtInt(c.unknown) + '</div><div class="l">unknown</div></div>' +
      "</div></div>";
    html += "</div>";

    /* providers */
    var provs = d.providers || [];
    html += '<div class="card"><h2>Providers <span class="sub">' + provs.length + " registered</span></h2>";
    if (provs.length === 0) {
      html += '<div class="empty">No providers registered.</div>';
    } else {
      html += '<div class="tbl-scroll"><table><thead><tr>' +
        "<th>Provider</th><th>Readiness</th><th>Capabilities</th><th>Key</th><th>Auto</th><th>Cooldown</th>" +
        "</tr></thead><tbody>";
      provs.forEach(function (p) {
        html += "<tr><td class=\"provider-name\" data-label=\"Provider\">" + esc(p.display_name || p.provider) +
          ' <span class="mono">' + esc(p.provider) + "</span></td>" +
          '<td data-label="Readiness">' + providerReadiness(p) + "</td>" +
          '<td data-label="Capabilities">' + (p.capabilities || []).map(chip).join(" ") + "</td>" +
          '<td data-label="Key">' + (p.key_present ? badge("present", "ok") : badge("missing", "bad")) + "</td>" +
          '<td data-label="Auto">' + (p.auto_allowed ? badge("allowed", "ok") : badge("manual only", "neutral")) + "</td>" +
          '<td data-label="Cooldown">' + (p.cooldown_active ? badge("active", "warn") : badge("none", "neutral")) + "</td></tr>";
      });
      html += "</tbody></table></div>";
    }
    html += "</div>";

    /* bounds + cache */
    var b = d.bounds || {}, ca = d.cache || {};
    html += '<div class="grid-2">';
    html += '<div class="card"><h2>Bounds</h2><dl class="kv">' +
      "<dt>Max URLs</dt><dd>" + fmtInt(b.max_urls_default) + " default · " + fmtInt(b.max_urls_hard) + " hard</dd>" +
      "<dt>Context chars</dt><dd>" + fmtInt(b.max_context_chars_default) + " default · " +
        fmtInt(b.max_context_chars_min) + " min · " + fmtInt(b.max_context_chars_hard) + " hard</dd>" +
      "<dt>Full-text TTL</dt><dd>" + fmtSecondsTtl(b.full_text_ttl_seconds) + "</dd>" +
      "<dt>Full-text max size</dt><dd>" + fmtBytes(b.full_text_max_bytes) + "</dd>" +
      "</dl></div>";
    html += '<div class="card"><h2>Cache</h2><dl class="kv">' +
      "<dt>Response cache</dt><dd>" + fmtInt(ca.response_entries) + " entries · " + fmtBytes(ca.response_bytes) + "</dd>" +
      "<dt>Full-text cache</dt><dd>" + fmtInt(ca.full_text_entries) + " entries · " + fmtBytes(ca.full_text_bytes) + "</dd>" +
      "<dt>Oldest entry</dt><dd>" + fmtTs(ca.oldest_timestamp) + "</dd>" +
      "<dt>Newest entry</dt><dd>" + fmtTs(ca.newest_timestamp) + "</dd>" +
      "</dl></div>";
    html += "</div>";

    /* activity summaries */
    var rs = d.receipts_summary || {}, bs = d.benchmark_summary || {};
    html += '<div class="grid-2">';
    html += '<div class="card"><h2>Receipts</h2><dl class="kv">' +
      "<dt>Stored</dt><dd>" + fmtInt(rs.count) + "</dd>" +
      "<dt>Latest</dt><dd>" + fmtTs(rs.latest_timestamp) + "</dd></dl></div>";
    html += '<div class="card"><h2>Benchmarks</h2><dl class="kv">' +
      "<dt>Runs stored</dt><dd>" + fmtInt(bs.count) + "</dd>" +
      "<dt>Latest</dt><dd>" + fmtTs(bs.latest_timestamp) + "</dd>" +
      "<dt>Kinds</dt><dd>" + ((bs.kinds || []).map(chip).join(" ") || "—") + "</dd>" +
      "<dt>Extract collected</dt><dd>" +
        (bs.extract_collected ? badge("collected", "ok") : badge("not_collected", "warn")) + "</dd>" +
      "</dl></div>";
    html += "</div>";

    $("view-overview").innerHTML = html;
  }

  /* ---------- receipts ---------- */

  function renderDecisions(list) {
    if (!list || list.length === 0) return '<div class="empty">No candidate decisions recorded.</div>';
    return '<div class="decisions">' + list.map(function (cd) {
      return '<div class="decision">' +
        '<span class="pos">#' + esc(cd.position != null ? cd.position : "?") + "</span>" +
        '<span class="prov">' + esc(cd.provider) + "</span>" +
        badge(cd.decision || "unknown", toneForDecision(cd.decision)) +
        (cd.reason_code ? chip(cd.reason_code) : "") +
        (cd.attempt_id ? '<span class="attempt">' + esc(cd.attempt_id) + "</span>" : "") +
        "</div>";
    }).join("") + "</div>";
  }

  function renderRoutingMeta(r) {
    var out = '<div class="route-meta">';
    if (r.mode) out += chip("mode: " + r.mode);
    if (r.authority) out += chip("authority: " + r.authority);
    if (r.execution_scope) out += chip("scope: " + r.execution_scope);
    if (r.selected_provider) out += badge("selected: " + r.selected_provider, "accent");
    if (r.fallback_reason) out += badge("fallback: " + r.fallback_reason, "warn");
    out += "</div>";
    return out;
  }

  function renderReceipt(r) {
    var routing = r.routing || {};
    var cache = r.cache || {};
    var lim = r.limits || {};
    var disp = String(cache.disposition || "").toLowerCase();
    var isCacheHit = disp.indexOf("hit") >= 0;

    var html = '<div class="card receipt">';
    html += '<div class="receipt-head">' +
      '<span class="cap">' + esc(r.capability || "?") + "</span>" +
      badge(r.status || "unknown", toneForStatus(r.status)) +
      (cache.disposition ? badge("cache: " + cache.disposition, isCacheHit ? "warn" : "neutral") : "") +
      (r.error_code ? badge("error: " + r.error_code, "bad") : "") +
      '<span class="ts">' + fmtTs(r.timestamp) + ' · <span class="mono">' + esc(r.execution_id || "") + "</span></span>" +
      "</div>";
    html += '<div class="receipt-body">';

    /* current execution panel */
    html += '<div class="exec-panel current"><div class="panel-title">Current execution</div>';
    html += renderRoutingMeta(routing);
    html += renderDecisions(routing.candidate_decisions);
    if (r.current_provider_attempts && r.current_provider_attempts.length > 0) {
      html += '<div class="route-meta">' +
        r.current_provider_attempts.map(function (a) { return chip("attempt: " + (typeof a === "string" ? a : JSON.stringify(a))); }).join("") +
        "</div>";
    } else if (isCacheHit) {
      html += '<div class="empty">No live provider attempts — response served from cache.</div>';
    }
    html += "</div>";

    /* cache-origin panel, clearly separated */
    if (isCacheHit && routing.cache_origin) {
      var o = routing.cache_origin;
      html += '<div class="exec-panel origin"><div class="panel-title">Served from cache origin</div>';
      html += '<div class="route-meta">' +
        chip("origin execution: " + (o.execution_id || cache.origin_execution_id || "?")) +
        (o.selected_provider ? badge("origin provider: " + o.selected_provider, "accent") : "") +
        "</div>";
      html += renderDecisions(o.candidate_decisions);
      html += "</div>";
    } else if (isCacheHit) {
      html += '<div class="exec-panel origin"><div class="panel-title">Served from cache origin</div>' +
        '<div class="route-meta">' + chip("origin execution: " + (cache.origin_execution_id || "unknown")) + "</div></div>";
    }

    /* limits */
    html += '<div class="limits-row">' +
      chip("urls: " + fmtInt(lim.requested_url_count) + " requested / " + fmtInt(lim.processed_url_count) +
        " processed / " + fmtInt(lim.omitted_url_count) + " omitted (max " + fmtInt(lim.max_urls) + ")") +
      chip("context: " + fmtInt(lim.context_chars_returned) + " / " + fmtInt(lim.max_context_chars) + " chars") +
      (lim.truncated ? badge("truncated", "warn") : badge("complete", "ok")) +
      "</div>";

    /* warnings */
    if (r.warning_codes && r.warning_codes.length > 0) {
      html += '<div class="warnings">' + r.warning_codes.map(function (w) { return badge(w, "warn"); }).join("") + "</div>";
    }

    html += "</div></div>";
    return html;
  }

  function renderReceipts(d) {
    var list = d.receipts || [];
    if (list.length === 0) {
      $("receiptsList").innerHTML = '<div class="empty">No routing receipts recorded yet.</div>';
      return;
    }
    $("receiptsList").innerHTML = list.map(renderReceipt).join("");
  }

  /* ---------- benchmarks ---------- */

  function renderBench(d) {
    var html = "";
    var av = d.availability || {};
    html += '<div class="availability">';
    ["search", "extract"].forEach(function (lane) {
      var v = av[lane];
      if (v === "collected") html += badge(lane + ": collected", "ok");
      else if (v === "not_collected") html += badge(lane + ": not_collected", "warn");
      else if (v != null) html += badge(lane + ": " + v, "neutral");
    });
    html += "</div>";

    if (av.extract === "not_collected") {
      html += '<div class="notcollected">Extract benchmarks have <strong>not been collected</strong> yet — ' +
        "no extract numbers are shown below, and none are implied. This is a data gap, not a zero.</div>";
    }

    var runs = d.runs || [];
    if (runs.length === 0) {
      html += '<div class="empty">No benchmark runs recorded yet.</div>';
      $("benchList").innerHTML = html;
      return;
    }

    runs.forEach(function (run) {
      html += '<div class="card">';
      html += '<div class="run-head">' +
        '<span class="kind">' + esc(run.kind || "?") + "</span>" +
        badge(run.status || "unknown", toneForStatus(run.status, run.ok)) +
        (run.ok === true ? badge("ok", "ok") : run.ok === false ? badge("not ok", "bad") : "") +
        '<span class="ts">' + fmtTs(run.timestamp) + "</span></div>";

      var provs = run.providers || [];
      if (provs.length === 0) {
        html += '<div class="empty">No provider results in this run.</div>';
      } else {
        html += '<div class="tbl-scroll"><table><thead><tr>' +
          '<th>Provider</th><th class="num">Score</th><th class="num">Success rate</th>' +
          '<th class="num">Median latency</th><th class="num">Errors</th></tr></thead><tbody>';
        provs.forEach(function (p) {
          html += '<tr><td class="provider-name" data-label="Provider">' + esc(p.provider) + "</td>" +
            '<td class="num" data-label="Score">' + (p.score != null ? esc(Number(p.score).toFixed(3)) : "—") + "</td>" +
            '<td class="num" data-label="Success rate">' + fmtRate(p.success_rate) + "</td>" +
            '<td class="num" data-label="Median latency">' + fmtLatency(p.median_latency_seconds) + "</td>" +
            '<td class="num" data-label="Errors">' + (p.error_count > 0 ? badge(fmtInt(p.error_count), "warn") : fmtInt(p.error_count)) + "</td></tr>";
        });
        html += "</tbody></table></div>";
      }

      if (run.recommended_priority && run.recommended_priority.length > 0) {
        html += '<div class="prio">recommended priority:' +
          run.recommended_priority.map(function (p, i) { return chip((i + 1) + " · " + p); }).join("") +
          "</div>";
      }
      html += "</div>";
    });

    $("benchList").innerHTML = html;
  }

  /* ---------- loading orchestration ---------- */

  function loadOverview() {
    return getJson(API.overview).then(renderOverview);
  }
  function loadReceipts() {
    return getJson(API.receipts($("receiptsLimit").value)).then(renderReceipts);
  }
  function loadBench() {
    return getJson(API.bench($("benchLimit").value)).then(renderBench);
  }

  function loadCurrentView() {
    if (state.loading) return;
    state.loading = true;
    clearError();
    var btn = $("refreshBtn");
    btn.disabled = true;
    var job;
    if (state.view === "receipts") job = loadReceipts();
    else if (state.view === "benchmarks") job = loadBench();
    else job = loadOverview();
    /* keep the engine badge in the header fresh on every view */
    if (state.view !== "overview") job = job.then(function () { return loadOverview(); });
    job.then(function () { stampUpdated(); })
      .catch(function (e) { showError("Load failed: " + e.message); })
      .then(function () { state.loading = false; btn.disabled = false; });
  }

  function switchView(view) {
    state.view = view;
    document.querySelectorAll(".tab").forEach(function (t) {
      var active = t.getAttribute("data-view") === view;
      t.classList.toggle("active", active);
      t.setAttribute("aria-selected", active ? "true" : "false");
    });
    ["overview", "receipts", "benchmarks"].forEach(function (v) {
      $("view-" + v).classList.toggle("hidden", v !== view);
    });
    loadCurrentView();
  }

  /* ---------- wire up ---------- */

  document.querySelectorAll(".tab").forEach(function (t) {
    t.addEventListener("click", function () { switchView(t.getAttribute("data-view")); });
  });
  $("refreshBtn").addEventListener("click", loadCurrentView);
  $("receiptsLimit").addEventListener("change", loadCurrentView);
  $("benchLimit").addEventListener("change", loadCurrentView);

  loadCurrentView();
})();
