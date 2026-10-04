(() => {
  const data = document.getElementById('demo-guide-data');
  const dialog = document.getElementById('demo-guide');
  if (!data || !dialog || !dialog.showModal) return;
  const config = JSON.parse(data.textContent);
  const card = dialog.querySelector('.guide-card');
  const spotlight = dialog.querySelector('.guide-spotlight');
  const title = document.getElementById('guide-title');
  const body = document.getElementById('guide-body');
  const progress = document.getElementById('guide-progress');
  const location = document.getElementById('guide-location');
  const back = document.getElementById('guide-back');
  const next = document.getElementById('guide-next');
  const fa = new Intl.NumberFormat('fa-IR');
  let mode = 'page', index = 0, steps = [], target = null, opener = null;
  const saved = (storage, key) => { try { return JSON.parse(storage.getItem(key)); } catch { return null; } };
  const write = (storage, key, value) => { try { storage.setItem(key, JSON.stringify(value)); } catch { /* Storage may be disabled. */ } };
  const remove = (storage, key) => { try { storage.removeItem(key); } catch { /* Guide still works in memory. */ } };
  const key = config.storage_key;
  const visible = node => node && node.getClientRects().length && getComputedStyle(node).visibility !== 'hidden';

  function position() {
    if (!dialog.open) return;
    const rect = target?.getBoundingClientRect();
    const viewport = window.visualViewport;
    const width = viewport?.width || innerWidth;
    const height = viewport?.height || innerHeight;
    const left = viewport?.offsetLeft || 0, top = viewport?.offsetTop || 0;
    if (rect) {
      const x = Math.max(left+7, rect.left-7), y = Math.max(top+7, rect.top-7);
      const right = Math.min(left+width-7, rect.right+7), bottom = Math.min(top+height-7, rect.bottom+7);
      spotlight.style.cssText = `left:${x}px;top:${y}px;width:${Math.max(0,right-x)}px;height:${Math.max(0,bottom-y)}px`;
    } else spotlight.style.cssText = `left:${left+width/2}px;top:${top+height/2}px;width:0;height:0`;
    const cardWidth = Math.min(390, width-24);
    card.style.width = `${cardWidth}px`;
    card.style.maxHeight = `${height-24}px`;
    const cardHeight = card.getBoundingClientRect().height;
    let x = left+(width-cardWidth)/2, y = top+height-cardHeight-12;
    if (width >= 760 && rect) {
      const below = rect.bottom+18, above = rect.top-cardHeight-18;
      y = below+cardHeight <= top+height-12 ? below : above >= top+12 ? above : y;
      x = Math.max(left+12, Math.min(rect.right-cardWidth, left+width-cardWidth-12));
    }
    card.style.left = `${x}px`;
    card.style.top = `${Math.max(top+12,y)}px`;
  }

  function render() {
    const entry = steps[index];
    if (!entry) { close(true); return; }
    if (mode === 'journey' && new URL(entry.url, window.location.origin).pathname !== window.location.pathname) {
      write(sessionStorage, `${key}:pending`, {index, url:entry.url});
      write(localStorage, key, {index});
      window.location.assign(entry.url);
      return;
    }
    target = document.querySelector(entry.target);
    const found = visible(target);
    if (!found) target = document.querySelector('#main-content h1, .page-heading, #main-content');
    title.textContent = entry.title;
    body.textContent = entry.body;
    progress.textContent = `${mode === 'journey' ? 'مسیر ارائه' : 'راهنمای صفحه'} · ${fa.format(index+1)} از ${fa.format(steps.length)}`;
    location.textContent = found ? 'بخش روشن‌شده را ببینید؛ سپس ادامه دهید.' : 'این بخش در وضعیت فعلی نمایش داده نمی‌شود؛ توضیح را بخوانید یا ادامه دهید.';
    back.disabled = index === 0;
    next.textContent = index === steps.length-1 ? 'پایان راهنما' : 'بعدی';
    document.getElementById('guide-journey').hidden = mode === 'journey';
    if (mode === 'journey') write(localStorage, key, {index});
    if (!dialog.open) dialog.showModal();
    target?.scrollIntoView({block:'center', behavior:'instant'});
    position();
    next.focus({preventScroll:true});
  }

  function open(which, restart = false) {
    if (!dialog.open) opener = document.activeElement;
    mode = which;
    steps = which === 'journey' ? config.journey : config.page.filter(entry => visible(document.querySelector(entry.target)));
    if (!steps.length) steps = config.page;
    const previous = saved(localStorage, key);
    index = which === 'journey' && !restart && Number.isInteger(previous?.index) ? Math.min(Math.max(previous.index,0),steps.length-1) : 0;
    render();
  }

  function close(completed = false) {
    remove(sessionStorage, `${key}:pending`);
    if (completed && mode === 'journey') remove(localStorage, key);
    dialog.close();
    target = null;
    opener?.focus({preventScroll:true});
  }
  document.querySelectorAll('[data-guide-start]').forEach(button => button.addEventListener('click', () => open(button.dataset.guideStart)));
  next.addEventListener('click', () => { if (index === steps.length-1) close(true); else { index++; render(); } });
  back.addEventListener('click', () => { if (index > 0) { index--; render(); } });
  dialog.querySelector('.guide-close').addEventListener('click', () => close());
  document.getElementById('guide-reset').addEventListener('click', () => open(mode,true));
  document.getElementById('guide-journey').addEventListener('click', () => open('journey'));
  dialog.addEventListener('cancel', event => { event.preventDefault(); close(); });
  window.addEventListener('resize', position);
  window.addEventListener('scroll', position, true);
  window.visualViewport?.addEventListener('resize', position);
  const pending = saved(sessionStorage, `${key}:pending`);
  remove(sessionStorage, `${key}:pending`);
  if (pending && Number.isInteger(pending.index) && config.journey[pending.index]?.url === pending.url
      && new URL(pending.url, window.location.origin).pathname === window.location.pathname) {
    mode = 'journey'; steps = config.journey; index = pending.index; render();
  }
})();
