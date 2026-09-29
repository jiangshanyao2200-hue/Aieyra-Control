const reducedMotion = matchMedia('(prefers-reduced-motion: reduce)');
let active,
  serial = 0;

// Preserve native navigation and focus; motion is an optional visual layer.
export function transition(update, target = document.querySelector('main')) {
  const mine = ++serial;
  active?.skipTransition?.();
  if (reducedMotion.matches || document.hidden || !document.startViewTransition) {
    update();
    if (!reducedMotion.matches && !document.hidden && target?.animate) {
      target.getAnimations().forEach((animation) => animation.cancel());
      target.animate([{ opacity: 0.7 }, { opacity: 1 }], {
        duration: 180,
        easing: 'ease-out',
      });
    }
    return Promise.resolve();
  }
  active = document.startViewTransition(() => {
    if (mine === serial) update();
  });
  // A superseded transition rejects ready even when its DOM callback succeeded.
  active.ready.catch(() => {});
  active.finished.catch(() => {});
  return active.updateCallbackDone.catch(() => {});
}
