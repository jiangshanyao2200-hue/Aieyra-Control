import { transition } from './transitions.js';

const start = document.querySelector('[data-demo-start]');
const stage = document.querySelector('[data-demo]');
if (start && stage) {
  const hero = document.querySelector('.hero-copy');
  const navigation = document.querySelector('[data-site-nav]');
  const demoRail = document.querySelector('[data-demo-rail]');
  const scenes = [...stage.querySelectorAll('[data-demo-scene]')];
  const steps = [...stage.querySelectorAll('[data-demo-step]')];
  const pause = stage.querySelector('[data-demo-pause]');
  const next = stage.querySelector('[data-demo-next]');
  const status = stage.querySelector('[data-demo-status]');
  const motion = matchMedia('(prefers-reduced-motion: reduce)');
  let index = 0,
    opened = false,
    playing = false,
    timer;
  function schedule() {
    clearTimeout(timer);
    stage.classList.toggle('is-paused', !playing || document.hidden);
    pause.textContent = playing ? '暂停' : '播放';
    pause.setAttribute('aria-pressed', String(!playing));
    if (playing && !stage.hidden && !document.hidden) timer = setTimeout(advance, 5500);
  }
  function render(i) {
    index = i;
    stage.dataset.scene = String(i);
    scenes.forEach((scene, n) => {
      scene.hidden = n !== i;
    });
    demoRail.querySelectorAll('[data-demo-nav]').forEach((button) => {
      if (button.dataset.demoNav === scenes[i].dataset.nav)
        button.setAttribute('aria-current', 'page');
      else button.removeAttribute('aria-current');
    });
    stage.querySelector('.demo-scenes').scrollTop = 0;
    steps.forEach((button, n) => {
      if (n === i) button.setAttribute('aria-current', 'step');
      else button.removeAttribute('aria-current');
    });
    status.textContent = `${i + 1} / ${scenes.length} · ${scenes[i].dataset.title}`;
    next.textContent = i === scenes.length - 1 ? '完成演示' : '下一幕';
    schedule();
  }
  function show(i) {
    if (!opened) return;
    index = i;
    void transition(() => render(i), stage);
  }
  function close() {
    opened = false;
    clearTimeout(timer);
    playing = false;
    void transition(() => {
      stage.hidden = true;
      hero.hidden = false;
      navigation.hidden = false;
      demoRail.hidden = true;
      document.body.classList.remove('demo-active');
      document.dispatchEvent(new Event('control-demo-state'));
      start.focus({ preventScroll: true });
    });
  }
  function advance() {
    if (index + 1 >= scenes.length) close();
    else show(index + 1);
  }
  start.addEventListener('click', () => {
    opened = true;
    void transition(() => {
      hero.hidden = true;
      stage.hidden = false;
      navigation.hidden = true;
      demoRail.hidden = false;
      playing = !motion.matches;
      document.body.classList.add('demo-active');
      document.dispatchEvent(new Event('control-demo-state'));
      render(0);
      stage.querySelector('[data-demo-close]').focus({ preventScroll: true });
    });
  });
  next.addEventListener('click', advance);
  pause.addEventListener('click', () => {
    playing = !playing;
    schedule();
  });
  steps.forEach((button, i) => button.addEventListener('click', () => show(i)));
  demoRail.querySelectorAll('[data-demo-nav]').forEach((button) =>
    button.addEventListener('click', () => {
      playing = false;
      show({ tasks: 1, chat: 2, account: 6 }[button.dataset.demoNav]);
    }),
  );
  stage.querySelectorAll('[data-demo-seat]').forEach((button) =>
    button.addEventListener('click', () => {
      playing = false;
      show(4);
    }),
  );
  stage.querySelectorAll('[data-demo-filter]').forEach((button) =>
    button.addEventListener('click', () => {
      playing = false;
      schedule();
      stage
        .querySelectorAll('[data-demo-filter]')
        .forEach((b) => b.setAttribute('aria-pressed', String(b === button)));
      stage.querySelectorAll('.home-task').forEach((row, i) => {
        row.hidden =
          button.dataset.demoFilter === 'active'
            ? i === 2
            : button.dataset.demoFilter === 'done'
              ? i !== 2
              : false;
      });
    }),
  );
  stage.querySelector('[data-demo-update-check]').addEventListener('click', () => {
    playing = false;
    schedule();
    stage.querySelector('[data-demo-update-status]').textContent =
      '演示：签名已验证，等待领导 Agent 审阅本地改动。';
  });
  stage.querySelector('[data-demo-close]').addEventListener('click', close);
  document.addEventListener('keydown', (e) => {
    if (e.key === 'Escape' && opened) close();
  });
  document.addEventListener('visibilitychange', schedule);
  motion.addEventListener('change', () => {
    if (motion.matches) {
      playing = false;
      schedule();
    }
  });
  window.addEventListener('pagehide', () => clearTimeout(timer));
  window.addEventListener('pageshow', schedule);
}
