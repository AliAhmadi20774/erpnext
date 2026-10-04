(() => {
  const toggle = document.querySelector('.mobile-menu');
  const sidebar = document.getElementById('primary-navigation');
  const revealActiveLink = () => {
    const nav = sidebar?.querySelector('.nav');
    const active = nav?.querySelector('a.active');
    if (!active) return;
    const linkBox = active.getBoundingClientRect();
    const navBox = nav.getBoundingClientRect();
    if (linkBox.top < navBox.top) nav.scrollTop += linkBox.top - navBox.top;
    else if (linkBox.bottom > navBox.bottom) nav.scrollTop += linkBox.bottom - navBox.bottom;
  };
  revealActiveLink();
  document.fonts?.ready.then(revealActiveLink);
  const setMenu = (open) => {
    document.body.classList.toggle('menu-open', open);
    toggle?.setAttribute('aria-expanded', String(open));
    if (open) {
      revealActiveLink();
      sidebar?.querySelector('.mobile-close')?.focus();
    }
    else if (document.activeElement?.closest('.sidebar')) toggle?.focus();
  };
  toggle?.addEventListener('click', () => setMenu(!document.body.classList.contains('menu-open')));
  document.querySelectorAll('[data-close-menu]').forEach(button => button.addEventListener('click', () => setMenu(false)));
  document.addEventListener('keydown', event => {
    if (event.key === 'Escape' && document.body.classList.contains('menu-open')) setMenu(false);
  });
  document.querySelectorAll('form[method="post"]').forEach(form => {
    form.addEventListener('submit', event => {
      if (form.dataset.confirm && !window.confirm(form.dataset.confirm)) {
        event.preventDefault();
        return;
      }
      if (!form.checkValidity()) return;
      const button = form.querySelector('button[type="submit"]');
      if (button) {
        button.disabled = true;
        button.textContent = 'در حال ثبت…';
      }
    });
  });
  document.querySelectorAll('[data-print]').forEach(button => {
    button.addEventListener('click', () => window.print());
  });
  const tree = document.querySelector('.product-tree');
  if (tree) {
    document.querySelectorAll('[data-tree-expand], [data-tree-collapse]').forEach(button => {
      button.hidden = false;
      button.addEventListener('click', () => {
        tree.querySelectorAll('details').forEach(details => {
          details.open = button.hasAttribute('data-tree-expand') || details.parentElement.classList.contains('level-0');
        });
      });
    });
  }
})();
