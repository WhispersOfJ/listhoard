/* ListHoard — the census bench (ListHoard-UX.spec.md §5).
   One hand-written vanilla layer; no libraries, no build step, no CDN.
   Everything progressively enhances the server-rendered pages.
   The abyss voice stays: the hoard keeps its lines. */

(() => {
  "use strict";

  const $ = (s, r = document) => r.querySelector(s);
  const $$ = (s, r = document) => [...r.querySelectorAll(s)];
  const store = {
    get(k, d) { try { return JSON.parse(localStorage.getItem(k)) ?? d; } catch { return d; } },
    set(k, v) { try { localStorage.setItem(k, JSON.stringify(v)); } catch {} },
    del(k) { try { localStorage.removeItem(k); } catch {} },
  };
  const listId = () => {
    const b = $("[data-list-id]");
    return b ? +b.dataset.listId : null;
  };

  /* ---------------------------------------------------------------- toasts */
  function toast(msg, kind = "info") {
    const box = $("#toasts");
    if (!box) return;
    const t = document.createElement("div");
    t.className = "toast " + kind;
    t.textContent = msg;
    box.appendChild(t);
    requestAnimationFrame(() => t.classList.add("arrived"));
    setTimeout(() => { t.classList.remove("arrived"); setTimeout(() => t.remove(), 400); },
      kind === "lume" ? 6000 : 4200);
  }

  /* ------------------------------------------------------- job heartbeat */
  if (document.body.dataset.authed === "true") {
    let seenDone = new Set();
    const poll = async () => {
      try {
        const r = await fetch("/jobs/poll", { headers: { Accept: "application/json" } });
        if (!r.ok) return;
        const d = await r.json();
        for (const j of d.done) {
          if (seenDone.has(j.import_id)) continue;
          seenDone.add(j.import_id);
          if (j.failed > 0) {
            toast(`haul ${j.import_id} surfaced: ${j.added} added, ${j.failed} failed`, "warn");
          } else {
            toast(`haul ${j.import_id} landed: ${j.added} added`, "lume");
          }
        }
      } catch { /* the heartbeat is honest about silence: skip */ }
    };
    poll();
    setInterval(poll, 6000);
  }

  /* -------------------------------------------------------- table sorting */
  function initSorting() {
    $$('table[data-js~="sortable"]').forEach((table) => {
      const head = $("thead th[data-sort]", table);
      if (!head) return;
      const key = "lh_sort" + location.pathname + "#" + table.id;
      const apply = (thIdx, dir) => {
        const tbody = $("tbody", table);
        const rows = $$("tr", tbody);
        const ths = $$("thead th[data-sort]", table);
        const type = ths[thIdx]?.dataset.sort || "text";
        const cmp = (a, b) => {
          const ka = a.children[thIdx].textContent.trim();
          const kb = b.children[thIdx].textContent.trim();
          const va = type === "num" ? parseFloat(ka) || 0 : ka.toLowerCase();
          const vb = type === "num" ? parseFloat(kb) || 0 : kb.toLowerCase();
          return (va > vb ? 1 : va < vb ? -1 : 0) * dir;
        };
        rows.sort(cmp).forEach((r) => tbody.appendChild(r));
        ths.forEach((t) => t.classList.remove("sorted-asc", "sorted-desc"));
        ths[thIdx]?.classList.add(dir > 0 ? "sorted-asc" : "sorted-desc");
      };
      $$("thead th[data-sort]", table).forEach((th) => {
        th.tabIndex = 0;
        th.setAttribute("role", "button");
        th.setAttribute("aria-label", "sort by " + th.textContent.trim());
        const idx = $$("thead th", table).indexOf(th);
        const act = () => {
          const dir = th.classList.contains("sorted-asc") ? -1 : 1;
          store.set(key, [idx, dir]);
          apply(idx, dir);
        };
        th.addEventListener("click", act);
        th.addEventListener("keydown", (e) => {
          if (e.key === "Enter" || e.key === " ") { e.preventDefault(); act(); }
        });
      });
      const saved = store.get(key, null);
      if (saved) apply(saved[0], saved[1]);
    });
  }

  /* --------------------------------------------------------- density mode */
  function initDensity() {
    const btn = $("#density-toggle");
    if (!btn) return;
    const setDense = (on) => {
      document.body.classList.toggle("dense", on);
      btn.classList.toggle("active", on);
      store.set("lh_density", on);
    };
    setDense(store.get("lh_density", false));
    btn.addEventListener("click", () => setDense(store.get("lh_density", false) === false));
  }

  /* ------------------------------------------------- curation: selection */
  let lastChecked = null;

  function checkedNow() {
    return new Set($$('input.pick:checked').map((c) => +c.value));
  }

  function selectionShow() {
    const el = $("#sel-count");
    if (el) el.textContent = checkedNow().size + " checked";
  }

  function initSelection() {
    const table = $("#items-table");
    if (!table) return;
    const lid = listId();
    const key = "lh_sel_" + lid;

    /* restore the cross-page held selection */
    const held = new Set(store.get(key, []));
    $$("input.pick", table).forEach((c) => { c.checked = held.has(+c.value); });

    const commit = () => {
      const merged = new Set([...held, ...checkedNow()]);
      store.set(key, [...merged]);
      const hidden = $("#importer #item_ids");
      if (hidden) hidden.value = [...merged].join(",");
      selectionShow();
    };

    $$("input.pick", table).forEach((c) => {
      c.addEventListener("change", () => {
        mergeDeltas();
        commit();
        rowsSpotlight();
      });
      c.addEventListener("click", (e) => {
        if (e.shiftKey && lastChecked) {
          const all = $$("input.pick", table);
          const i0 = all.indexOf(lastChecked);
          const i1 = all.indexOf(c);
          const on = c.checked;
          const lo = Math.min(i0, i1), hi = Math.max(i0, i1);
          for (let i = lo; i <= hi; i++) { all[i].checked = on; }
          mergeDeltas();
          commit();
        }
        lastChecked = c;
      });
    });

    const mergeDeltas = () => {
      /* rows whose checkbox is gone from the page keep their held status */
      $$("input.pick", table).forEach((c) => { if (c.checked) held.add(+c.value); });
    };

    const all = $("#check-all");
    if (all) {
      all.addEventListener("change", () => {
        $$(".spot", table).forEach((r) => {
          const c = $(".pick", r); if (c && !r.hidden) c.checked = all.checked;
        });
        if (all.checked) { /* haul everything visible into the hold */ }
        mergeDeltas();
        commit();
        rowsSpotlight();
      });
    }

    const clear = $("#sel-clear");
    if (clear) clear.addEventListener("click", () => {
      held.clear();
      store.set(key, []);
      $$("input.pick", table).forEach((c) => { c.checked = false; });
      commit();
      toast("the hold is empty", "info");
    });

    commit();
  }

  /* ------------------------------------------------ curation: quick views */
  const benchState = { qf: {}, q: "" };

  function rowsApply() {
    const table = $("#items-table");
    if (!table) return;
    let visible = 0;
    $$("tbody tr", table).forEach((r) => {
      const d = r.dataset;
      let show = true;
      if (benchState.q) {
        const hay = (d.title + " " + (r.querySelector(".row-title")?.textContent || "")).toLowerCase();
        show = show && hay.includes(benchState.q);
      }
      if (benchState.qf.lib) show = show && d.lib !== "1";
      if (benchState.qf.xxx) show = show && d.xxx !== "1";
      if (benchState.qf.star) show = show && r.dataset.starred === "1";
      r.hidden = !show;
      if (show) visible++;
    });
    const cnt = $("#sel-count");
    if (cnt) cnt.dataset.visible = visible;
  }

  function initBench() {
    const bench = $(".bench");
    if (!bench) return;
    const lid = listId();
    const qk = "lh_qf_" + lid;

    benchState.qf = store.get(qk, { lib: false, xxx: false, anime: false, star: false });

    const qfBtn = (name) => $(`[data-qf="${name}"]`);
    const paint = () => {
      for (const k of ["lib", "xxx", "anime", "star"]) {
        qfBtn(k)?.classList.toggle("active", !!benchState.qf[k]);
      }
      rowsApply();
    };
    for (const k of ["lib", "xxx", "anime", "star"]) {
      qfBtn(k)?.addEventListener("click", () => {
        benchState.qf[k] = !benchState.qf[k];
        store.set(qk, benchState.qf);
        paint();
      });
    }
    qfBtn("clear")?.addEventListener("click", () => {
      benchState.qf = { lib: false, xxx: false, anime: false, star: false };
      $("#live-search").value = "";
      benchState.q = "";
      store.del(qk);
      paint();
    });

    const live = $("#live-search");
    let deb = null;
    live?.addEventListener("input", () => {
      clearTimeout(deb);
      deb = setTimeout(() => { benchState.q = live.value.trim().toLowerCase(); rowsApply(); }, 60);
    });

    /* named saved views (ListHoard-UX.spec.md §5.2 presets) */
    const pname = window.location.pathname;
    const pk = "lh_presets_" + pname;
    const sel = $("#preset-select");
    const fill = () => {
      if (!sel) return;
      const ps = store.get(pk, {});
      sel.innerHTML = '<option value="">— choose a sight —</option>' +
        Object.keys(ps).map((n) => `<option>${n}</option>`).join("");
    };
    fill();
    $("#preset-apply")?.addEventListener("click", () => {
      const ps = store.get(pk, {});
      const p = ps[sel.value];
      if (!p) return;
      benchState.qf = { ...{ lib: false, xxx: false, anime: false, star: false }, ...p.qf };
      benchState.q = p.q || "";
      live.value = benchState.q;
      store.set(qk, benchState.qf);
      paint();
      toast(`the sight “${sel.value}” is raised`, "lume");
    });
    $("#preset-save")?.addEventListener("click", () => {
      const name = window.prompt("name this sight (its filters are remembered)");
      if (!name) return;
      const ps = store.get(pk, {});
      ps[name] = { qf: { ...benchState.qf }, q: benchState.q };
      store.set(pk, ps);
      fill();
      toast(`sight “${name}” pinned below`, "info");
    });

    paint();
  }

  /* --------------------------------------------- curation: keyboard flow */
  let spotIdx = -1;
  function rowsSpotlight() {
    const vis = $$("#items-table tbody tr").filter((r) => !r.hidden);
    vis.forEach((r) => r.classList.add("spot"));
    return vis;
  }

  function initKeyboard() {
    if (!$("#items-table")) return;
    document.addEventListener("keydown", (e) => {
      if (e.ctrlKey || e.metaKey || e.altKey) return;
      const tag = document.activeElement?.tagName;
      if (["INPUT", "SELECT", "TEXTAREA"].includes(tag) &&
          document.activeElement.id !== "items-table") return;
      const vis = rowsSpotlight();
      if (!vis.length) return;
      if (e.key === "j") { spotIdx = Math.min(spotIdx + 1, vis.length - 1); }
      else if (e.key === "k") { spotIdx = Math.max(spotIdx - 1, 0); }
      else if (e.key === "x" && spotIdx >= 0) {
        const c = $(".pick", vis[spotIdx]); if (c) { c.checked = !c.checked; c.dispatchEvent(new Event("change")); }
      }
      else if (e.key === "a") { vis.forEach((r) => { const c = $(".pick", r); if (c) c.checked = true; }); mergeAll(); }
      else if (e.key === "A") { vis.forEach((r) => { const c = $(".pick", r); if (c) c.checked = false; }); mergeAll(); }
      else if (e.key === "g" && spotIdx >= 0) return;
      else return;
      e.preventDefault();
      vis.forEach((r, i) => r.classList.toggle("lit", i === spotIdx));
      vis[spotIdx]?.scrollIntoView({ block: "center" });
    });
    function mergeAll() {
      const all = $("#check-all");
      if (all) all.checked = false;
      $$("#items-table input.pick").forEach((c) =>
        c.dispatchEvent(new Event("change")));
      rowsSpotlight();
    }
    /* g then Enter queues the import: separate key to avoid trap */
    let gDown = false, gTimer = null;
    document.addEventListener("keydown", (e) => {
      if (e.key === "g" && !["INPUT", "SELECT", "TEXTAREA"].includes(document.activeElement?.tagName)) {
        gDown = true; clearTimeout(gTimer); gTimer = setTimeout(() => { gDown = false; }, 900);
      }
      if (e.key === "Enter" && gDown) {
        gDown = false;
        const form = $("#importer");
        if (form) {
          const hidden = $("#item_ids");
          if (hidden) hidden.value = checkedIds();
          form.submit();
        }
      }
    });
  }

  /* --------------------------------------------------------- item drawer */
  function initDrawer() {
    const d = $("#drawer");
    if (!d) return;
    const open = async (list, item, row) => {
      d.classList.add("open");
      d.hidden = false;
      d.innerHTML = '<p class="hint">sonar pinging…</p>';
      let det;
      try {
        const r = await fetch(`/lists/${list}/items/${item}/detail`);
        det = await r.json();
        if (det.error) throw new Error(det.error);
      } catch (e) {
        d.innerHTML = `<p class="error">the sonar returned nothing: ${e.message}</p>
          <button type="button" class="ghost drawer-close">stow</button>`;
        return;
      }
      const links = [];
      if (det.tmdb_id) {
        links.push(`<a href="https://www.themoviedb.org/${det.mediatype === "show" ? "tv" : "movie"}/${det.tmdb_id}" target="_blank" rel="noopener">TMDB ${det.tmdb_id}</a>`);
      }
      if (det.tvdb_id) {
        links.push(`<a href="https://thetvdb.com/series/${det.tvdb_id}" target="_blank" rel="noopener">TVDB ${det.tvdb_id}</a>`);
      }
      if (!links.length) links.push('<span class="hint">no ids surfaced — this row is title-only</span>');
      const others = det.also_in.map((o) =>
        `<li><span class="src src-${o.source}">${o.source}</span> <a href="/lists/${o.id}">${o.name}</a></li>`).join("");
      d.innerHTML = `
        <h3>${det.title} <small>${det.year || ""}</small></h3>
        <p class="ids">${links.join(" · ")}</p>
        <p>${det.mediatype} · resolvability:
          ${det.in_library ? '<span class="badge ' + (det.monitored ? "in" : "off") + '">in hull</span>'
                           : '<span class="badge no">not in hull</span>'}
          ${det.starred ? ' · <span class="star">starred</span>' : ""}</p>
        <details class="note-box" ${det.note ? "open" : ""}>
          <summary>Grounding note</summary>
          <textarea id="drawer-note" rows="3" maxlength="2000"
            placeholder="pin a line to this creature…">${det.note || ""}</textarea>
          <button type="button" id="drawer-note-save">Pin note</button>
        </details>
        <h4>Also catalogued in</h4>
        ${others ? `<ul>${others}</ul>` : '<p class="hint">only this list holds it.</p>'}
        <button type="button" class="ghost drawer-close">Stow the drawer</button>`;
      $("#drawer-note-save", d)?.addEventListener("click", async () => {
        const v = $("#drawer-note", d).value;
        await fetch(`/items/${item}/note`, {
          method: "POST",
          headers: { "Content-Type": "application/x-www-form-urlencoded" },
          body: new URLSearchParams({ note: v }),
        });
        const mark = row?.querySelector(".note-mark");
        if (mark) mark.style.opacity = v.trim() ? "1" : "0.2";
        toast(v.trim() ? "note pinned" : "note dropped", "info");
      });
    };
    $("#drawer").addEventListener("click", (e) => {
      if (e.target === d || e.target.classList.contains("drawer-close")) {
        d.classList.remove("open"); d.hidden = true;
      }
    });
    document.addEventListener("keydown", (e) => {
      if (e.key === "Escape") { d.classList.add("closed"); d.hidden = true; }
    });
    const table = $("#items-table");
    if (!table) return;
    const lid = listId();
    table.addEventListener("click", async (e) => {
      const btn = e.target.closest(".info-btn");
      if (!btn) return;
      const row = btn.closest("tr");
      await open(lid, +row.dataset.itemId, row);
    });
    table.addEventListener("click", async (e) => {
      const star = e.target.closest(".star-btn");
      if (!star) return;
      const row = star.closest("tr");
      const neg = (row.dataset.starred === "1" ? "false" : "true");
      const r = await fetch(`/items/${row.dataset.itemId}/star`, {
        method: "POST",
        headers: { "Content-Type": "application/x-www-form-urlencoded" },
        body: new URLSearchParams({ starred: neg }),
      });
      if (r.ok) {
        row.dataset.starred = neg === "true" ? "1" : "0";
        star.classList.toggle("active", neg === "true");
        toast(neg === "true" ? "star affixed" : "star released", "info");
        rowsApply();
      }
    });
    table.addEventListener("click", async (e) => {
      const noteBtn = e.target.closest(".note-btn");
      if (!noteBtn) return;
      const row = noteBtn.closest("tr");
      const list = listId();
      const r = await fetch(`/lists/${list}/items/${row.dataset.itemId}/detail`);
      const det = await r.json();
      const cur = det.note || "";
      const v = window.prompt("grounding note for this creature", cur);
      if (v === null) return;
      await fetch(`/items/${row.dataset.itemId}/note`, {
        method: "POST",
        headers: { "Content-Type": "application/x-www-form-urlencoded" },
        body: new URLSearchParams({ note: v }),
      });
      const mark = row.querySelector(".note-mark");
      if (mark) mark.style.opacity = v.trim() ? "1" : "0.2";
      toast(v.trim() ? "note pinned" : "note dropped", "info");
    });
  }

  /* --------------------------------------------------------- hover sight */
  function initPreview() {
    const card = $("#preview");
    const table = $("#items-table");
    if (!card || !table) return;
    let timer = null;
    table.addEventListener("mouseover", (e) => {
      const row = e.target.closest("tbody tr");
      if (!row) return;
      clearTimeout(timer);
      timer = setTimeout(() => {
        const url = row.dataset.backdrop || row.dataset.poster;
        if (!url) return;
        card.innerHTML =
          `<img src="${url}" alt=""><p>${row.dataset.title}</p>`;
        card.hidden = false;
        const r = row.getBoundingClientRect();
        card.style.top = Math.min(window.scrollY + r.top, document.body.scrollHeight - 320) + "px";
        card.style.left = (window.innerWidth / 2 - 300) + "px";
      }, 350);
    });
    table.addEventListener("mouseout", (e) => {
      const row = e.target.closest("tbody tr");
      if (row) { clearTimeout(timer); card.hidden = true; }
    });
  }

  /* --------------------------------------------------- browse without reload */
  function initBrowseNoReload() {
    const form = $("#browse-filters");
    if (!form) return;
    const swap = async (href) => {
      const r = await fetch(href);
      if (!r.ok) { location.assign(href); return; }
      const html = await r.text();
      const doc = new DOMParser().parseFromString(html, "text/html");
      const next = doc.querySelector("main");
      if (!next) { location.assign(href); return; }
      document.querySelector("main").innerHTML = next.innerHTML;
      history.pushState({}, "", href);
      boot();
    };
    form.addEventListener("submit", (e) => {
      e.preventDefault();
      swap("/browse?" + new URLSearchParams(new FormData(form)).toString());
    });
    document.addEventListener("click", (e) => {
      const a = e.target.closest('#next-page, a[href*="?page="]');
      if (!a) return;
      e.preventDefault();
      swap(a.href);
    });
    window.addEventListener("popstate", () => boot());
  }

  /* --------------------------------------------- live import instrumentation */
  function initImportLive() {
    const box = $(".imp-live");
    if (!box) return;
    const id = +box.dataset.importId;
    const fill = $("#imp-fill");
    const counts = $("#imp-counts");
    const ticker = $("#imp-buckets");
    const status = $("#imp-status");
    const reissueBox = $("#imp-reissue");
    let lastBuckets = {};
    const t = setInterval(async () => {
      let d;
      try {
        const r = await fetch(`/imports/${id}/progress`);
        d = await r.json();
        if (d.error) { clearInterval(t); return; }
      } catch { return; }
      lastBuckets = d.buckets;
      const run = d.running && d.live;
      const added = run ? d.live.added : (d.added ?? d.buckets.added ?? 0);
      const failed = run ? d.live.failed : (d.failed ?? d.buckets.failed ?? 0);
      const done = run ? d.live.added + d.live.skipped + d.live.failed : d.total;
      const total = Math.max(1, run ? d.live.total : (d.total || 1));
      fill.style.width = Math.round(100 * Math.min(done, total) / total) + "%";
      const segs = [];
      segs.push(`added ${added}`);
      if (run) segs.push(`skipped ${d.live.skipped}`);
      segs.push(`failed ${failed}`);
      counts.textContent = segs.join(" · ") + ` — ${run ? "elapsed " + d.live.elapsed + "s of " + total + " rows" : ""}`;
      for (const li of $$("li", ticker)) {
        const b = li.dataset.b;
        const n = d.buckets[b] || 0;
        if (n !== lastBuckets[b] && lastBuckets[b] !== undefined && n > (lastBuckets[b] || 0)) {
          li.classList.remove("flare"); void li.offsetWidth; li.classList.add("flare");
        }
        li.textContent = `${b}: ${n}`;
      }
      status.textContent = run ? "running" : (d.status || "done");
      if (!run) {
        clearInterval(t);
        if ((d.buckets.failed || 0) > 0) {
          reissueBox.innerHTML =
            `<form method="post" action="/imports/${id}/reissue">
              <button type="submit" title="re-run the failed rows with the stored options">
                Re-issue the ${d.buckets.failed} failed row(s)</button></form>`;
        }
      }
    }, 2500);
  }

  /* --------------------------------------------------------------- boot */
  function boot() {
    initSorting();
    initDensity();
    initSelection();
    initBench();
    initKeyboard();
    initDrawer();
    initPreview();
    initBrowseNoReload();
    initImportLive();
  }

  document.addEventListener("DOMContentLoaded", boot);
  boot();
})();
