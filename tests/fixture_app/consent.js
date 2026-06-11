// Blocking consent overlay shown on every page until accepted (cookie).
(function () {
  if (document.cookie.includes('plateful_consent=1')) return;
  var ov = document.createElement('div');
  ov.id = 'consent-overlay';
  ov.setAttribute('data-testid', 'consent-overlay');
  ov.style.cssText = 'position:fixed;inset:0;background:rgba(20,20,20,.92);z-index:9999;' +
    'display:flex;align-items:center;justify-content:center;';
  ov.innerHTML =
    '<div class="consent-card">' +
    '<h2>Cookies at our table</h2>' +
    '<p>We use cookies to remember your pantry and your saved recipes.</p>' +
    '<button data-testid="consent-accept">Accept cookies</button> ' +
    '<button data-testid="consent-decline">Decline</button>' +
    '</div>';
  function mount() {
    document.body.appendChild(ov);
    ov.querySelector('[data-testid="consent-accept"]').addEventListener('click', function () {
      document.cookie = 'plateful_consent=1; path=/';
      ov.remove();
    });
    ov.querySelector('[data-testid="consent-decline"]').addEventListener('click', function () {
      ov.remove();
    });
  }
  if (document.body) mount();
  else window.addEventListener('DOMContentLoaded', mount);
})();
