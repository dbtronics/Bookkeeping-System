/* pagination.js — client-side search + pagination for any <table>.
 *
 * Usage:
 *   initPagination('table-element-id', { defaultPageSize: 10 });
 *
 * Injects a controls bar (search left, page-size right) above the table's
 * .table-scroll wrapper, and a page-nav bar below it.
 */

function initPagination(tableId, opts) {
  opts = opts || {};
  var defaultSize = opts.defaultPageSize || 10;

  var table = document.getElementById(tableId);
  if (!table) return;

  var tbody   = table.querySelector('tbody');
  var allRows = Array.from(tbody.querySelectorAll('tr'));

  // State
  var currentPage    = 1;
  var pageSize       = defaultSize;
  var filteredRows   = allRows.slice();

  // ---- Inject controls bar above .table-scroll ----------------------
  var scrollWrap = table.closest('.table-scroll') || table.parentElement;
  var container  = scrollWrap.parentElement;

  var bar = document.createElement('div');
  bar.className = 'pag-bar';
  bar.innerHTML =
    '<input type="text" class="pag-search" placeholder="Search…" aria-label="Search table">' +
    '<div class="pag-size-wrap">' +
      '<span class="pag-size-label">Show</span>' +
      '<select class="pag-size-select">' +
        '<option value="10">10</option>' +
        '<option value="25">25</option>' +
        '<option value="50">50</option>' +
        '<option value="100">100</option>' +
        '<option value="all">All</option>' +
      '</select>' +
      '<span class="pag-size-label">rows</span>' +
    '</div>';

  container.insertBefore(bar, scrollWrap);

  // ---- Inject page nav below .table-scroll --------------------------
  var nav = document.createElement('div');
  nav.className = 'pag-nav';
  scrollWrap.insertAdjacentElement('afterend', nav);

  // ---- Wire events --------------------------------------------------
  var searchEl = bar.querySelector('.pag-search');
  var sizeEl   = bar.querySelector('.pag-size-select');

  searchEl.addEventListener('input', function () {
    currentPage = 1;
    applyFilter(this.value);
  });

  sizeEl.addEventListener('change', function () {
    pageSize    = this.value === 'all' ? Infinity : parseInt(this.value, 10);
    currentPage = 1;
    render();
  });

  // ---- Core logic ---------------------------------------------------
  function applyFilter(query) {
    var q = query.toLowerCase().trim();
    filteredRows = q
      ? allRows.filter(function (row) {
          return row.textContent.toLowerCase().includes(q);
        })
      : allRows.slice();
    render();
  }

  function render() {
    var total      = filteredRows.length;
    var totalPages = pageSize === Infinity ? 1 : Math.ceil(total / pageSize) || 1;

    if (currentPage > totalPages) currentPage = totalPages;

    var start = pageSize === Infinity ? 0 : (currentPage - 1) * pageSize;
    var end   = pageSize === Infinity ? total : Math.min(start + pageSize, total);

    // Show/hide rows
    allRows.forEach(function (row) { row.style.display = 'none'; });
    filteredRows.slice(start, end).forEach(function (row) { row.style.display = ''; });

    renderNav(totalPages, total, start, end);
  }

  function renderNav(totalPages, total, start, end) {
    if (total === 0) {
      nav.innerHTML = '<span class="pag-info">No results</span>';
      return;
    }

    var infoText = pageSize === Infinity
      ? 'Showing all ' + total + ' rows'
      : 'Showing ' + (start + 1) + '–' + end + ' of ' + total;

    var pagesHtml = '';
    if (totalPages > 1) {
      pagesHtml += '<div class="pag-pages">';
      pagesHtml += '<button class="pag-btn pag-prev" data-page="' + (currentPage - 1) + '"' +
                   (currentPage === 1 ? ' disabled' : '') + '>&#8249;</button>';

      buildPageList(currentPage, totalPages).forEach(function (p) {
        if (p === '...') {
          pagesHtml += '<span class="pag-ellipsis">…</span>';
        } else {
          pagesHtml += '<button class="pag-btn' + (p === currentPage ? ' active' : '') +
                       '" data-page="' + p + '">' + p + '</button>';
        }
      });

      pagesHtml += '<button class="pag-btn pag-next" data-page="' + (currentPage + 1) + '"' +
                   (currentPage === totalPages ? ' disabled' : '') + '>&#8250;</button>';
      pagesHtml += '</div>';
    }

    nav.innerHTML = '<span class="pag-info">' + infoText + '</span>' + pagesHtml;

    nav.querySelectorAll('[data-page]').forEach(function (btn) {
      btn.addEventListener('click', function () {
        var p = parseInt(this.dataset.page, 10);
        if (p >= 1 && p <= totalPages) {
          currentPage = p;
          render();
          // Scroll table into view smoothly
          scrollWrap.scrollIntoView({ behavior: 'smooth', block: 'nearest' });
        }
      });
    });
  }

  // Build page number list with ellipsis for large page counts
  function buildPageList(current, total) {
    if (total <= 7) {
      var pages = [];
      for (var i = 1; i <= total; i++) pages.push(i);
      return pages;
    }
    var list = [1];
    if (current > 3) list.push('...');
    var lo = Math.max(2, current - 1);
    var hi = Math.min(total - 1, current + 1);
    for (var j = lo; j <= hi; j++) list.push(j);
    if (current < total - 2) list.push('...');
    list.push(total);
    return list;
  }

  // Initial render
  render();
}


/* initListPagination — pagination for a div-based list (not a table).
 *
 * Usage:
 *   initListPagination('container-id', { defaultPageSize: 5 });
 *
 * The container must contain direct child elements (e.g. .suggestion-row divs).
 */
function initListPagination(containerId, opts) {
  opts = opts || {};
  var defaultSize = opts.defaultPageSize || 5;

  var container = document.getElementById(containerId);
  if (!container) return;

  var allItems = Array.from(container.children);
  if (allItems.length === 0) return;

  var currentPage = 1;
  var pageSize    = defaultSize;

  // ---- Inject controls bar above the container ----
  var wrapper = container.parentElement;

  var bar = document.createElement('div');
  bar.className = 'pag-bar';
  bar.innerHTML =
    '<div style="flex:1"></div>' +
    '<div class="pag-size-wrap">' +
      '<span class="pag-size-label">Show</span>' +
      '<select class="pag-size-select">' +
        '<option value="5">5</option>' +
        '<option value="10">10</option>' +
        '<option value="25">25</option>' +
        '<option value="all">All</option>' +
      '</select>' +
      '<span class="pag-size-label">per page</span>' +
    '</div>';
  wrapper.insertBefore(bar, container);

  var nav = document.createElement('div');
  nav.className = 'pag-nav';
  container.insertAdjacentElement('afterend', nav);

  bar.querySelector('.pag-size-select').addEventListener('change', function () {
    pageSize    = this.value === 'all' ? Infinity : parseInt(this.value, 10);
    currentPage = 1;
    render();
  });

  function render() {
    var total      = allItems.length;
    var totalPages = pageSize === Infinity ? 1 : Math.ceil(total / pageSize) || 1;
    if (currentPage > totalPages) currentPage = totalPages;

    var start = pageSize === Infinity ? 0 : (currentPage - 1) * pageSize;
    var end   = pageSize === Infinity ? total : Math.min(start + pageSize, total);

    allItems.forEach(function (el, i) {
      el.style.display = (i >= start && i < end) ? '' : 'none';
    });

    var infoText = pageSize === Infinity
      ? 'Showing all ' + total
      : 'Showing ' + (start + 1) + '–' + end + ' of ' + total;

    var pagesHtml = '';
    if (totalPages > 1) {
      pagesHtml += '<div class="pag-pages">';
      pagesHtml += '<button class="pag-btn pag-prev" data-page="' + (currentPage - 1) + '"' +
                   (currentPage === 1 ? ' disabled' : '') + '>&#8249;</button>';
      for (var p = 1; p <= totalPages; p++) {
        pagesHtml += '<button class="pag-btn' + (p === currentPage ? ' active' : '') +
                     '" data-page="' + p + '">' + p + '</button>';
      }
      pagesHtml += '<button class="pag-btn pag-next" data-page="' + (currentPage + 1) + '"' +
                   (currentPage === totalPages ? ' disabled' : '') + '>&#8250;</button>';
      pagesHtml += '</div>';
    }

    nav.innerHTML = '<span class="pag-info">' + infoText + '</span>' + pagesHtml;
    nav.querySelectorAll('[data-page]').forEach(function (btn) {
      btn.addEventListener('click', function () {
        var p = parseInt(this.dataset.page, 10);
        if (p >= 1 && p <= totalPages) { currentPage = p; render(); }
      });
    });
  }

  render();
}
