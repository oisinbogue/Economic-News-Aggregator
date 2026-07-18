// Carousel nav, country/topic filter chips, and client-side search against
// search-index.json (brief Phase 4: carousel arrow nav, filters, search).
// No build step / framework -- the whole site is static files, so this
// stays plain DOM + fetch.
(function () {
  "use strict";

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
    if (!chips.length || !cards.length) return;

    var active = { topic: new Set(), country: new Set() };

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

        card.classList.toggle("filtered-out", !(topicOk && countryOk));
      });
    }

    chips.forEach(function (chip) {
      chip.addEventListener("click", function () {
        var facet = chip.dataset.facet;
        var value = chip.dataset.value;
        if (active[facet].has(value)) {
          active[facet].delete(value);
          chip.classList.remove("active");
        } else {
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
  }

  function initSearch() {
    var box = document.getElementById("search-box");
    var results = document.getElementById("search-results");
    var script = document.currentScript || document.querySelector("script[data-search-index]");
    if (!box || !results || !script) return;

    var indexUrl = script.getAttribute("data-search-index");
    var indexPromise = null;

    function loadIndex() {
      if (!indexPromise) {
        indexPromise = fetch(indexUrl).then(function (r) { return r.json(); }).catch(function () { return []; });
      }
      return indexPromise;
    }

    function render(matches, query) {
      if (!matches.length) {
        results.innerHTML = '<div class="sr-empty">No matches for "' + escapeHtml(query) + '"</div>';
        results.hidden = false;
        return;
      }
      results.innerHTML = matches.slice(0, 20).map(function (item) {
        return '<a href="' + escapeHtml(item.u) + '" target="_blank" rel="noopener">' +
          escapeHtml(item.t) +
          '<div class="sr-source">' + escapeHtml(item.s) + (item.c ? " &middot; " + escapeHtml(item.c) : "") +
          (item.d ? " &middot; " + escapeHtml(item.d) : "") + "</div></a>";
      }).join("");
      results.hidden = false;
    }

    function escapeHtml(s) {
      return String(s || "").replace(/[&<>"']/g, function (c) {
        return { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c];
      });
    }

    var debounceTimer = null;
    box.addEventListener("input", function () {
      var query = box.value.trim().toLowerCase();
      clearTimeout(debounceTimer);
      if (!query) {
        results.hidden = true;
        return;
      }
      debounceTimer = setTimeout(function () {
        loadIndex().then(function (items) {
          var matches = items.filter(function (item) {
            return (item.t && item.t.toLowerCase().indexOf(query) !== -1) ||
              (item.sm && item.sm.toLowerCase().indexOf(query) !== -1) ||
              (item.c && item.c.toLowerCase().indexOf(query) !== -1) ||
              (item.tp && item.tp.join(" ").toLowerCase().indexOf(query) !== -1);
          });
          render(matches, query);
        });
      }, 150);
    });

    document.addEventListener("click", function (e) {
      if (!results.contains(e.target) && e.target !== box) {
        results.hidden = true;
      }
    });
  }

  document.addEventListener("DOMContentLoaded", function () {
    initCarousels();
    initFilters();
    initSearch();
  });
})();
