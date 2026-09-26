// Patch existing elements so polling never replaces an active editor or scene.
const key = node => node.nodeType === 1 ? node.id || node.getAttribute('data-key') || '' : '';
function compatible(a, b) { return a.nodeType === b.nodeType && a.nodeName === b.nodeName && key(a) === key(b); }
function patch(a, b) {
  if (a.nodeType !== 1) { if (a.nodeValue !== b.nodeValue) a.nodeValue = b.nodeValue; return; }
  const persistent = a.hasAttribute('data-persistent');
  // 原位刷新的说明折叠保留用户选择；显式受控的详情仍由业务状态管理。
  const preserveOpen = a instanceof HTMLDetailsElement && b.hasAttribute('data-preserve-open');
  for (const attr of [...a.attributes]) if (!b.hasAttribute(attr.name) && attr.name !== 'data-composing' && !(preserveOpen && attr.name === 'open')) a.removeAttribute(attr.name);
  for (const attr of [...b.attributes]) if (!(preserveOpen && attr.name === 'open') && a.getAttribute(attr.name) !== attr.value) a.setAttribute(attr.name, attr.value);
  if (persistent) return;
  if (a instanceof HTMLTextAreaElement || a instanceof HTMLInputElement) {
    if (!a.dataset.composing && a.value !== b.value) a.value = b.value;
    if (a instanceof HTMLInputElement) a.checked = b.checked;
    return;
  }
  children(a, b);
  if (a instanceof HTMLSelectElement && a.value !== b.value) a.value = b.value;
}
function children(parent, next) {
  const wanted = [...next.childNodes];
  let cursor = parent.firstChild;
  for (const fresh of wanted) {
    let old = cursor;
    if (!old || !compatible(old, fresh)) {
      old = key(fresh) ? [...parent.childNodes].find(node => compatible(node, fresh)) : null;
      if (old) {
        // Moving an existing keyed node can drop focus in the browser.
        const focused=old.contains(document.activeElement)?document.activeElement:null;
        parent.insertBefore(old, cursor);
        focused?.focus({preventScroll:true});
      }
      else { old = fresh.cloneNode(true); parent.insertBefore(old, cursor); }
    }
    patch(old, fresh);
    cursor = old.nextSibling;
  }
  while (cursor) { const next = cursor.nextSibling; cursor.remove(); cursor = next; }
}
export function updateHTML(element, html) {
  if (!element) return;
  const template = document.createElement('template'); template.innerHTML = html;
  children(element, template.content);
}
document.addEventListener('compositionstart', event => { if (event.target.dataset) event.target.dataset.composing = 'true'; });
document.addEventListener('compositionend', event => { if (event.target.dataset) delete event.target.dataset.composing; });
