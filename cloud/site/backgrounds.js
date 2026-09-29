const host = document.querySelector('[data-backgrounds]');
if (host) {
  const home = document.body.dataset.page === 'home';
  const motion = matchMedia('(prefers-reduced-motion: reduce)');
  const connection = navigator.connection;
  const constrained = () =>
    connection?.saveData || /(^|-)2g$/.test(connection?.effectiveType || '');
  const sequence = [1, 2, 3, 5, 6, 7, 8];
  const nextImage = () => sequence[(sequence.indexOf(current) + 1) % sequence.length];
  const artwork = (number) =>
    `scene-${String(number).padStart(2, '0')}${[1, 2, 3, 5].includes(number) ? '-clean' : ''}`;
  const initial = { home: 3, center: 1, download: 7, feedback: 5, callback: 6 };
  let current = initial[document.body.dataset.page] || 3;
  let timer,
    changing = false,
    paused = motion.matches;
  const toggle = document.querySelector('[data-background-toggle]');
  const remember = () => {
    try {
      sessionStorage.setItem('control-background', String(current));
    } catch {
      /* Optional visual preference. */
    }
  };
  const show = async (number) => {
    if (changing || number === current) return;
    changing = true;
    const frame = document.createElement('picture');
    frame.className = 'background-frame';
    const source = document.createElement('source');
    source.media = '(max-width: 600px)';
    source.srcset = `/assets/${artwork(number)}-small.webp`;
    const img = new Image(1920, 1080);
    img.alt = '';
    img.decoding = 'async';
    img.fetchPriority = 'low';
    img.src = `/assets/${artwork(number)}.webp`;
    frame.append(source, img);
    host.append(frame);
    try {
      await img.decode();
      const previous = host.querySelector('.is-visible');
      // Give the decoded layer one paint at opacity zero before fading it in.
      await new Promise((resolve) => requestAnimationFrame(() => requestAnimationFrame(resolve)));
      frame.classList.add('is-visible');
      // Keep the previous image fully opaque underneath the crossfade.
      setTimeout(() => previous?.remove(), motion.matches ? 0 : 1200);
      current = number;
      host.dataset.scene = String(current);
      remember();
    } catch {
      frame.remove();
    } finally {
      changing = false;
    }
  };
  const schedule = () => {
    clearInterval(timer);
    if (
      home &&
      !paused &&
      !constrained() &&
      !document.hidden &&
      !document.body.classList.contains('demo-active')
    )
      timer = setInterval(() => show(nextImage()), 7000);
    if (toggle) {
      toggle.textContent = paused ? '播放背景' : '暂停背景';
      toggle.setAttribute('aria-pressed', String(paused));
    }
  };
  host.dataset.scene = String(current);
  let last = 0;
  try {
    last = Number(sessionStorage.getItem('control-background'));
  } catch {
    /* No storage needed. */
  }
  if (sequence.includes(last) && last !== current) void show(last);
  else remember();
  toggle?.addEventListener('click', () => {
    paused = !paused;
    schedule();
  });
  motion.addEventListener('change', () => {
    paused = motion.matches;
    schedule();
  });
  document.addEventListener('visibilitychange', schedule);
  document.addEventListener('control-demo-state', schedule);
  connection?.addEventListener('change', schedule);
  window.addEventListener('pagehide', () => clearInterval(timer));
  window.addEventListener('pageshow', schedule);
  schedule();
}
