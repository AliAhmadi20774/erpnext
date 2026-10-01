(() => {
  const addButton = document.getElementById('add-bom-line');
  const container = document.getElementById('bom-components');
  const template = document.getElementById('empty-bom-line');
  const total = document.getElementById('id_components-TOTAL_FORMS');
  if (!addButton || !container || !template || !total) return;
  addButton.addEventListener('click', () => {
    const index = Number(total.value);
    const html = template.innerHTML.replaceAll('__prefix__', String(index));
    container.insertAdjacentHTML('beforeend', html);
    total.value = String(index + 1);
    container.lastElementChild?.querySelector('select, input')?.focus();
  });
})();
