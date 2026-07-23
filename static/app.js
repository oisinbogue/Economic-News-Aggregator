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
      var index = 0;

      function show(newIndex) {
        slides[index].classList.remove("active");
        if (dots[index]) dots[index].classList.remove("active");
        index = (newIndex + slides.length) % slides.length;
        slides[index].classList.add("active");
        if (dots[index]) dots[index].classList.add("active");
        if (posEl) posEl.textContent = String(index + 1);
      }

      var prev = carousel.querySelector(".carousel-nav.prev");
      var next = carousel.querySelector(".carousel-nav.next");
      if (prev) prev.addEventListener("click", function () { show(index - 1); });
      if (next) next.addEventListener("click", function () { show(index + 1); });
    });
  }

  function initFilters() {
    var chips = document.querySelectorAll(".chip[data-facet]");
    var clearBtn = document.getElementById("clear-filters");
    var cards = document.querySelectorAll(".cluster-card");
    var sections = document.querySelectorAll(".section");
    if (!chips.length || !cards.length) return;

    var active = activeFilters;

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

    chips.forEach(function (chip) {
      chip.addEventListener("click", function () {
        var facet = chip.dataset.facet;
        var value = chip.dataset.value;
        var wasActive = active[facet].has(value);

        // Each facet (topic, country) is single-select: picking a chip
        // clears any other selection in the same facet first, so e.g.
        // Housing & Property and Macroeconomics can't both be active, but
        // a topic and a country can still combine.
        active[facet].clear();
        chips.forEach(function (c) {
          if (c.dataset.facet === facet) c.classList.remove("active");
        });

        if (!wasActive) {
          active[facet].add(value);
          chip.classList.add("active");
        }
        applyFilters();
      });
    });

    clearBtn.addEventListener("click", function () {
      active.topic.clear();
      active.country.clear();
      chips.forEach(function (chip) { chip.classList.remove("active"); });
      applyFilters();
    });

    document.querySelectorAll(".cluster-card .tag[data-topic]").forEach(function (tag) {
      tag.addEventListener("click", function () {
        var value = tag.dataset.topic;
        var target = Array.prototype.find.call(chips, function (c) {
          return c.dataset.facet === "topic" && c.dataset.value === value;
        });
        if (!target) return;
        if (!active.topic.has(value)) target.click();
        target.scrollIntoView({ behavior: "smooth", block: "center" });
      });
    });
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
    initTheme();
    initSearchToggle();
    initSearch();
  });
})();
