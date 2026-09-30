(() => {
  const toggle = document.querySelector('.mobile-menu');
  const sidebar = document.getElementById('primary-navigation');
  const setMenu = (open) => {
    document.body.classList.toggle('menu-open', open);
    toggle?.setAttribute('aria-expanded', String(open));
    if (open) sidebar?.querySelector('.mobile-close')?.focus();
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
})();
