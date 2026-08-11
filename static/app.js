// Carousel nav, country/topic filter chips, Pagefind-backed search,
// dark-mode toggle, and the topbar search expand/collapse.
// No build step / framework -- the whole site is static files, so this
// stays plain DOM + fetch (Pagefind's own JS/WASM is lazy-loaded via
// dynamic import when the user first searches).
(function () {
  "use strict";

  // Shared between initFilters and initSearch so an active chip selection
  // narrows an in-progress search instead of the two systems being separate.
  var activeFilters = { topic: new Set(), country: new Set() };
  var searchRefresh = null;

  function initCarousels() {
    document.querySelectorAll(".carousel").forEach(function (carousel) {
      var slides = carousel.querySelectorAll(".carousel-slide");
      if (slides.length < 2) return;
      var dots = carousel.querySelectorAll(".dot");
      var posEl = carousel.querySelector(".carousel-pos");
      var statusEl = carousel.querySelector(".carousel-status");
      var index = 0;

      function show(newIndex, opts) {
        var announce = !opts || opts.announce !== false;
        slides[index].classList.remove("active");
        if (dots[index]) {
          dots[index].classList.remove("active");
          dots[index].removeAttribute("aria-current");
        }
        index = (newIndex + slides.length) % slides.length;
        slides[index].classList.add("active");
        if (dots[index]) {
          dots[index].classList.add("active");
          dots[index].setAttribute("aria-current", "true");
        }
        if (posEl) posEl.textContent = String(index + 1);
        if (statusEl && announce) {
          var heading = slides[index].querySelector("h3");
          var title = heading ? heading.textContent.trim() : "";
          statusEl.textContent = "Take " + (index + 1) + " of " + slides.length + (title ? ": " + title : "");
        }
      }

      var prev = carousel.querySelector(".carousel-nav.prev");
      var next = carousel.querySelector(".carousel-nav.next");
      if (prev) prev.addEventListener("click", function () { show(index - 1); });
      if (next) next.addEventListener("click", function () { show(index + 1); });

      dots.forEach(function (dot, dotIndex) {
        dot.addEventListener("click", function () { show(dotIndex); });
      });

      carousel.addEventListener("keydown", function (e) {
        if (e.key === "ArrowRight") { e.preventDefault(); show(index + 1); }
        else if (e.key === "ArrowLeft") { e.preventDefault(); show(index - 1); }
      });

      var touchStartX = null;
      var touchStartY = null;
      carousel.addEventListener("touchstart", function (e) {
        var t = e.changedTouches[0];
        touchStartX = t.clientX;
        touchStartY = t.clientY;
      }, { passive: true });
      carousel.addEventListener("touchend", function (e) {
        if (touchStartX === null) return;
        var t = e.changedTouches[0];
        var dx = t.clientX - touchStartX;
        var dy = t.clientY - touchStartY;
        touchStartX = null;
        touchStartY = null;
        if (Math.abs(dx) < 40 || Math.abs(dx) < Math.abs(dy)) return;
        show(dx < 0 ? index + 1 : index - 1);
      }, { passive: true });
    });
  }

  function initFilters() {
    var selects = document.querySelectorAll(".filter-select[data-facet]");
    var clearBtn = document.getElementById("clear-filters");
    var cards = document.querySelectorAll(".cluster-card");
    var sections = document.querySelectorAll(".section");
    if (!selects.length || !cards.length) return;

    var active = activeFilters;

    function selectFor(facet) {
      return Array.prototype.find.call(selects, function (s) { return s.dataset.facet === facet; });
    }

    // Dropdown options come from the site-wide tag union (pipeline/build.py),
    // so a facet value valid on one page may not appear in this page's own
    // <select> (e.g. no card here carries that topic). Only honor a URL
    // param if it matches a real option; otherwise drop it instead of
    // leaving the select stuck on an unmatched value.
    function hasOption(select, value) {
      return !!select && Array.prototype.some.call(select.options, function (o) { return o.value === value; });
    }

    // Reflects the current activeFilters into ?topic=&country= via
    // replaceState so a filtered view is bookmarkable without adding a
    // history entry per selection change.
    function syncUrl() {
      var params = new URLSearchParams(window.location.search);
      ["topic", "country"].forEach(function (facet) {
        var values = Array.from(active[facet]);
        if (values.length) params.set(facet, values[0]);
        else params.delete(facet);
      });
      var qs = params.toString();
      var newUrl = window.location.pathname + (qs ? "?" + qs : "") + window.location.hash;
      history.replaceState(history.state, "", newUrl);
    }

    function readUrlIntoFilters() {
      var params = new URLSearchParams(window.location.search);
      ["topic", "country"].forEach(function (facet) {
        var select = selectFor(facet);
        var value = params.get(facet);
        active[facet].clear();
        if (select) select.value = "";
        if (value && hasOption(select, value)) {
          active[facet].add(value);
          select.value = value;
        }
      });
    }

    function applyFilters() {
      var anyActive = active.topic.size > 0 || active.country.size > 0;
      clearBtn.hidden = !anyActive;

      cards.forEach(function (card) {
        var cardTopics = (card.dataset.topics || "").split(",").filter(Boolean);
        var cardCountries = (card.dataset.countries || "").split(",").filter(Boolean);

        var topicOk = active.topic.size === 0 ||
          cardTopics.some(function (t) { return active.topic.has(t); });
        var countryOk = active.country.size === 0 ||
          cardCountries.some(function (c) { return active.country.has(c); });

        var hide = !(topicOk && countryOk);
        card.classList.toggle("filtered-out", hide);
        var flank = card.closest(".hero-flank, .hero-center");
        if (flank) flank.classList.toggle("filtered-out", hide);
      });

      sections.forEach(function (section) {
        var hasVisible = !!section.querySelector(".cluster-card:not(.filtered-out)");
        section.classList.toggle("filtered-out", !hasVisible);
      });

      if (searchRefresh) searchRefresh();
    }

    selects.forEach(function (select) {
      select.addEventListener("change", function () {
        var facet = select.dataset.facet;
        // Each facet (topic, country) is single-select via its dropdown, so
        // e.g. Housing & Property and Macroeconomics can't both be active,
        // but a topic and a country can still combine.
        active[facet].clear();
        if (select.value) active[facet].add(select.value);
        applyFilters();
        syncUrl();
      });
    });

    clearBtn.addEventListener("click", function () {
      active.topic.clear();
      active.country.clear();
      selects.forEach(function (select) { select.value = ""; });
      applyFilters();
      syncUrl();
    });

    document.querySelectorAll(".cluster-card .tag[data-topic]").forEach(function (tag) {
      tag.addEventListener("click", function () {
        var value = tag.dataset.topic;
        var target = selectFor("topic");
        if (!target) return;
        if (!active.topic.has(value)) {
          target.value = value;
          target.dispatchEvent(new Event("change"));
        }
        target.scrollIntoView({ behavior: "smooth", block: "center" });
      });
    });

    // Query string is the source of truth on load, and again if the user
    // navigates with the browser's back/forward buttons (including a
    // bfcache-restored page, which doesn't fire popstate).
    readUrlIntoFilters();
    applyFilters();

    window.addEventListener("popstate", function () {
      readUrlIntoFilters();
      applyFilters();
    });
    window.addEventListener("pageshow", function (e) {
      if (e.persisted) {
        readUrlIntoFilters();
        applyFilters();
      }
    });
  }

  // Source Health page: client-side sort/filter over the plain feeds table
  // (no framework, matches the rest of the site -- see initFilters above for
  // the same pattern applied to homepage cluster cards).
  function initHealthTable() {
    var table = document.getElementById("health-table");
    if (!table) return;
    var tbody = table.querySelector("tbody");
    var rows = Array.prototype.slice.call(tbody.querySelectorAll("tr"));
    var headers = Array.prototype.slice.call(table.querySelectorAll("th[data-sort]"));
    var textInput = document.getElementById("health-filter-text");
    var statusSelect = document.getElementById("health-filter-status");
    var regionSelect = document.getElementById("health-filter-region");
    var visibleCount = document.getElementById("health-visible-count");
    var noMatches = document.getElementById("health-no-matches");

    var sortKey = null;
    var sortDir = 1;

    function applyFilters() {
      var text = (textInput.value || "").trim().toLowerCase();
      var status = statusSelect.value;
      var region = regionSelect.value;
      var visible = 0;

      rows.forEach(function (row) {
        var textOk = !text || (row.dataset.filterText || "").indexOf(text) !== -1;
        var statusOk = !status || row.dataset.status === status;
        var regionOk = !region || row.dataset.region === region;
        var hide = !(textOk && statusOk && regionOk);
        row.classList.toggle("health-row-hidden", hide);
        if (!hide) visible += 1;
      });

      visibleCount.textContent = visible + " of " + rows.length + " feeds";
      noMatches.hidden = visible !== 0;
    }

    function applySort(key) {
      var type = headers.filter(function (h) { return h.dataset.sort === key; })[0].dataset.sortType;
      if (sortKey === key) {
        sortDir = -sortDir;
      } else {
        sortKey = key;
        sortDir = 1;
      }
      headers.forEach(function (h) {
        h.removeAttribute("data-sort-dir");
      });
      var activeHeader = headers.filter(function (h) { return h.dataset.sort === key; })[0];
      activeHeader.setAttribute("data-sort-dir", sortDir === 1 ? "asc" : "desc");

      var attr = "data-" + key + "-sort";
      rows.sort(function (a, b) {
        var av = a.getAttribute(attr) || "";
        var bv = b.getAttribute(attr) || "";
        if (type === "number") {
          return (parseFloat(av) - parseFloat(bv)) * sortDir;
        }
        if (av < bv) return -1 * sortDir;
        if (av > bv) return 1 * sortDir;
        return 0;
      });
      rows.forEach(function (row) { tbody.appendChild(row); });
    }

    headers.forEach(function (h) {
      h.addEventListener("click", function () { applySort(h.dataset.sort); });
    });
    textInput.addEventListener("input", applyFilters);
    statusSelect.addEventListener("change", applyFilters);
    regionSelect.addEventListener("change", applyFilters);

    applyFilters();
  }

  // Indicators landing/country grid: search + topic/country chips + core/all
  // toggle, all client-side over cards already in the DOM (server-rendered
  // by indicators/build.py -- see indicators/data/manifest.json for the
  // machine-readable equivalent). Cards beyond PAGE_SIZE start hidden so the
  // landing page doesn't paint all ~700 cards at once (INDICATORS_BRIEF.md
  // #9); "Show more" reveals another page at a time.
  var INDICATOR_PAGE_SIZE = 24;

  function initIndicators() {
    var grid = document.getElementById("indicator-grid");
    if (!grid) return;

    var cards = Array.prototype.slice.call(grid.querySelectorAll(".indicator-card"));
    var searchInput = document.getElementById("indicator-search");
    var tierChips = Array.prototype.slice.call(document.querySelectorAll("[data-tier-chip]"));
    var topicChips = Array.prototype.slice.call(document.querySelectorAll("[data-topic-chip]"));
    var countryChips = Array.prototype.slice.call(document.querySelectorAll("[data-country-chip]"));
    var clearBtn = document.getElementById("indicator-clear-filters");
    var loadMoreBtn = document.getElementById("indicator-load-more");
    var visibleCountEl = document.getElementById("indicator-visible-count");
    var noMatchesEl = document.getElementById("indicator-no-matches");

    var state = {
      tier: "core",
      topics: new Set(),
      countries: new Set(),
      query: "",
      revealCount: INDICATOR_PAGE_SIZE
    };

    function matches(card) {
      if (state.tier === "core" && card.dataset.tier !== "core") return false;
      if (state.topics.size && !state.topics.has(card.dataset.topic)) return false;
      if (state.countries.size && !state.countries.has(card.dataset.country)) return false;
      if (state.query && (card.dataset.search || "").indexOf(state.query) === -1) return false;
      return true;
    }

    function render() {
      var matched = cards.filter(matches);
      var shown = 0;
      cards.forEach(function (card) {
        var isMatch = matches(card);
        card.classList.toggle("filtered-out", !isMatch);
        if (!isMatch) return;
        shown += 1;
        card.classList.toggle("paginated-hidden", shown > state.revealCount);
      });

      var anyActive = state.tier !== "core" || state.topics.size || state.countries.size || state.query;
      if (clearBtn) clearBtn.hidden = !anyActive;

      var visibleNow = Math.min(matched.length, state.revealCount);
      if (visibleCountEl) {
        visibleCountEl.textContent = matched.length
          ? "Showing " + visibleNow + " of " + matched.length + " indicators"
          : "";
      }
      if (noMatchesEl) noMatchesEl.hidden = matched.length !== 0;
      if (loadMoreBtn) loadMoreBtn.hidden = matched.length <= state.revealCount;
    }

    function resetPaginationAndRender() {
      state.revealCount = INDICATOR_PAGE_SIZE;
      render();
    }

    if (searchInput) {
      var debounce = null;
      searchInput.addEventListener("input", function () {
        clearTimeout(debounce);
        debounce = setTimeout(function () {
          state.query = searchInput.value.trim().toLowerCase();
          resetPaginationAndRender();
        }, 120);
      });
    }

    tierChips.forEach(function (chip) {
      chip.addEventListener("click", function () {
        state.tier = chip.dataset.tierChip;
        tierChips.forEach(function (c) { c.classList.toggle("active", c === chip); });
        resetPaginationAndRender();
      });
    });

    topicChips.forEach(function (chip) {
      chip.addEventListener("click", function () {
        var value = chip.dataset.topicChip;
        if (state.topics.has(value)) { state.topics.delete(value); chip.classList.remove("active"); }
        else { state.topics.add(value); chip.classList.add("active"); }
        resetPaginationAndRender();
      });
    });

    countryChips.forEach(function (chip) {
      chip.addEventListener("click", function () {
        var value = chip.dataset.countryChip;
        if (state.countries.has(value)) { state.countries.delete(value); chip.classList.remove("active"); }
        else { state.countries.add(value); chip.classList.add("active"); }
        resetPaginationAndRender();
      });
    });

    if (clearBtn) {
      clearBtn.addEventListener("click", function () {
        state.tier = "core";
        state.topics.clear();
        state.countries.clear();
        state.query = "";
        if (searchInput) searchInput.value = "";
        tierChips.forEach(function (c) { c.classList.toggle("active", c.dataset.tierChip === "core"); });
        topicChips.forEach(function (c) { c.classList.remove("active"); });
        countryChips.forEach(function (c) { c.classList.remove("active"); });
        resetPaginationAndRender();
      });
    }

    if (loadMoreBtn) {
      loadMoreBtn.addEventListener("click", function () {
        state.revealCount += INDICATOR_PAGE_SIZE;
        render();
      });
    }

    render();
  }

  function initReviewHelp() {
    var btn = document.getElementById("review-help-btn");
    var dialog = document.getElementById("review-help-dialog");
    var closeBtn = document.getElementById("review-help-close");
    if (!btn || !dialog) return;

    btn.addEventListener("click", function () { dialog.showModal(); });
    if (closeBtn) closeBtn.addEventListener("click", function () { dialog.close(); });
    // Clicking the backdrop (outside the dialog's own box) closes it too.
    dialog.addEventListener("click", function (e) {
      if (e.target === dialog) dialog.close();
    });
  }

  // Backup/DB status panel on the Source Health page. Fetched client-side
  // (rather than baked into the template at build time) so it stays fresh
  // between site rebuilds -- status.json is rewritten every pipeline run,
  // the HTML isn't. Missing/unreachable file just leaves the panel hidden.
  var SNAPSHOT_STALE_HOURS = 24;

  function initBackupStatus() {
    var panel = document.getElementById("backup-status-panel");
    if (!panel) return;

    fetch("status.json", { cache: "no-store" }).then(function (res) {
      if (!res.ok) throw new Error("status.json " + res.status);
      return res.json();
    }).then(function (status) {
      var counts = status.counts || {};
      setStat(panel, "db-size", formatBytes(status.db_size_bytes));
      setStat(panel, "articles", formatCount(counts.articles));
      setStat(panel, "clusters", formatCount(counts.clusters));
      setStat(panel, "predictions", formatCount(counts.predictions));

      var snapshotEl = panel.querySelector('[data-status="last-snapshot"]');
      if (snapshotEl) {
        var at = status.last_snapshot_at ? new Date(status.last_snapshot_at) : null;
        if (at && !isNaN(at.getTime())) {
          var ageHours = (Date.now() - at.getTime()) / 3600000;
          snapshotEl.textContent = at.toLocaleString();
          if (ageHours > SNAPSHOT_STALE_HOURS) {
            snapshotEl.textContent += " (stale)";
            snapshotEl.classList.add("status-stat-stale");
          }
        } else {
          snapshotEl.textContent = "never";
          snapshotEl.classList.add("status-stat-stale");
        }
      }

      panel.hidden = false;
    }).catch(function () {
      // No status.json (older build, or first deploy before a pipeline run
      // has written one yet) -- degrade to just not showing the panel.
    });
  }

  function setStat(panel, key, text) {
    var el = panel.querySelector('[data-status="' + key + '"]');
    if (el) el.textContent = text;
  }

  function formatBytes(n) {
    if (typeof n !== "number") return "unknown";
    if (n < 1048576) return (n / 1024).toFixed(1) + " KiB";
    return (n / 1048576).toFixed(1) + " MiB";
  }

  function formatCount(n) {
    return typeof n === "number" ? n.toLocaleString() : "unknown";
  }

  function initTheme() {
    var btn = document.getElementById("theme-toggle");
    if (!btn) return;
    var KEY = "econ-theme";

    function apply(mode) {
      document.body.classList.toggle("dark", mode === "dark");
      btn.textContent = mode === "dark" ? "☀" : "☽";
    }

    var saved = null;
    try { saved = localStorage.getItem(KEY); } catch (e) {}
    apply(saved === "dark" ? "dark" : "light");

    btn.addEventListener("click", function () {
      var mode = document.body.classList.contains("dark") ? "light" : "dark";
      apply(mode);
      try { localStorage.setItem(KEY, mode); } catch (e) {}
    });
  }

  function initSearchToggle() {
    var wrap = document.getElementById("search-wrap");
    var toggle = document.getElementById("search-toggle");
    var box = document.getElementById("search-box");
    var results = document.getElementById("search-results");
    if (!wrap || !toggle || !box) return;

    toggle.addEventListener("click", function () {
      var opening = !wrap.classList.contains("open");
      wrap.classList.toggle("open", opening);
      if (opening) {
        box.focus();
      } else {
        box.value = "";
        if (results) results.hidden = true;
      }
    });

    document.addEventListener("click", function (e) {
      if (!wrap.contains(e.target) && wrap.classList.contains("open") && !box.value) {
        wrap.classList.remove("open");
      }
    });
  }

  function initSearch() {
    var box = document.getElementById("search-box");
    var results = document.getElementById("search-results");
    var script = document.currentScript || document.querySelector("script[data-pagefind]");
    if (!box || !results || !script) return;

    // Resolved to an absolute URL because dynamic import() from a classic
    // (non-module) script rejects bare/relative specifiers without an
    // import map -- see "Failed to resolve module specifier".
    var pagefindUrl = new URL(script.getAttribute("data-pagefind"), document.baseURI).href;
    var pagefindPromise = null;

    function loadPagefind() {
      if (!pagefindPromise) {
        pagefindPromise = import(pagefindUrl).then(function (mod) {
          return mod.init().then(function () { return mod; });
        });
      }
      return pagefindPromise;
    }

    function currentFilters() {
      var f = {};
      if (activeFilters.topic.size) f.topic = Array.from(activeFilters.topic);
      if (activeFilters.country.size) f.country = Array.from(activeFilters.country);
      return f;
    }

    function escapeHtml(s) {
      return String(s || "").replace(/[&<>"']/g, function (c) {
        return { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c];
      });
    }

    function render(datas, query) {
      var items = [];
      datas.forEach(function (d) {
        if (d.sub_results && d.sub_results.length) {
          d.sub_results.forEach(function (sr) {
            items.push({ url: sr.url, title: sr.title, excerpt: sr.excerpt });
          });
        } else {
          items.push({ url: d.url, title: (d.meta && d.meta.title) || d.url, excerpt: d.excerpt });
        }
      });

      if (!items.length) {
        results.innerHTML = '<div class="sr-empty">No matches for "' + escapeHtml(query) + '"</div>';
        results.hidden = false;
        return;
      }
      results.innerHTML = items.slice(0, 20).map(function (item) {
        return '<a href="' + escapeHtml(item.url) + '">' +
          escapeHtml(item.title) +
          '<div class="sr-source">' + (item.excerpt || "") + "</div></a>";
      }).join("");
      results.hidden = false;
    }

    function runSearch(query) {
      loadPagefind().then(function (pagefind) {
        return pagefind.search(query, { filters: currentFilters() });
      }).then(function (search) {
        return Promise.all(search.results.slice(0, 20).map(function (r) { return r.data(); }));
      }).then(function (datas) {
        render(datas, query);
      });
    }

    var debounceTimer = null;
    box.addEventListener("input", function () {
      var query = box.value.trim();
      clearTimeout(debounceTimer);
      if (!query) {
        results.hidden = true;
        return;
      }
      debounceTimer = setTimeout(function () { runSearch(query); }, 150);
    });

    // Called when the active topic/country chips change so a search
    // already in progress narrows to the new filter set immediately.
    searchRefresh = function () {
      var query = box.value.trim();
      if (query) runSearch(query);
    };

    document.addEventListener("click", function (e) {
      if (!results.contains(e.target) && e.target !== box) {
        results.hidden = true;
      }
    });
  }

  document.addEventListener("DOMContentLoaded", function () {
    initCarousels();
    initFilters();
    initHealthTable();
    initIndicators();
    initReviewHelp();
    initBackupStatus();
    initTheme();
    initSearchToggle();
    initSearch();
  });
})();
