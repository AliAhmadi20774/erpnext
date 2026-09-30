(() => {
  const catalogNode = document.getElementById('item-catalog');
  if (!catalogNode) return;
  const catalog = JSON.parse(catalogNode.textContent);
  const prices = new Map(catalog.map(item => [String(item.id), item]));
  const kind = location.pathname.includes('/purchase/') ? 'purchase_price' : 'sale_price';
  const lines = document.getElementById('order-lines');
  const totalInput = document.getElementById('id_lines-TOTAL_FORMS');
  function updatePrices() {
    lines.querySelectorAll('.order-line').forEach(row => {
      const select = row.querySelector('select');
      const label = row.querySelector('.unit-preview');
      const product = prices.get(select.value);
      label.textContent = product ? Number(product[kind]).toLocaleString('fa-IR') + ' تومان' : '—';
    });
  }
  lines.addEventListener('change', updatePrices);
  document.getElementById('add-line').addEventListener('click', () => {
    const count = Number(totalInput.value);
    if (count >= 20) return;
    const html = document.getElementById('empty-line').innerHTML.replaceAll('__prefix__', String(count));
    lines.insertAdjacentHTML('beforeend', html);
    totalInput.value = count + 1;
  });
  updatePrices();
})();
