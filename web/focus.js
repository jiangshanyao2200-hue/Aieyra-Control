// Floating regions stay non-modal on desktop. Return to a visible origin,
// including when polling replaced it or its menu has since closed.
export function focusVisible(...candidates) {
  for (const candidate of candidates) {
    const node = typeof candidate === 'string' ? document.querySelector(candidate) : candidate;
    if (!node?.isConnected || !node.getClientRects().length || node.closest('[hidden],[inert]') || node.disabled) continue;
    node.focus?.({preventScroll:true});
    if (document.activeElement === node) return true;
  }
  return false;
}

export function returnFocus(origin = document.activeElement, ...fallbacks) {
  const selector = origin?.id ? '#' + CSS.escape(origin.id) : ['data-floor-id','data-signal-seat','data-focus','data-key']
    .filter(name => origin?.hasAttribute?.(name))
    .map(name => `[${name}="${CSS.escape(origin.getAttribute(name))}"]`)[0];
  const menu = origin?.closest('.channel-members') ? '[data-cc-action="members"]' :
    origin?.closest('.channel-popover') ? '[data-cc-action="channel-menu"]' : null;
  return () => focusVisible(origin, selector, menu, ...fallbacks, '#control-dock [data-page="command"]');
}
