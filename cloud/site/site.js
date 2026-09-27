const CLOUD = 'https://ctrlupdate.aieyra.cn';
const $ = (s) => document.querySelector(s),
  status = $('[role="status"]');
const say = (value) => {
  if (status) status.textContent = value;
};
async function api(path, body, token) {
  const controller = new AbortController(),
    timeout = setTimeout(() => controller.abort(), 12000);
  try {
    const r = await fetch(CLOUD + path, {
      method: body ? 'POST' : 'GET',
      headers: {
        ...(body ? { 'Content-Type': 'application/json' } : {}),
        ...(token ? { Authorization: 'Bearer ' + token } : {}),
      },
      body: body ? JSON.stringify(body) : undefined,
      cache: 'no-store',
      credentials: 'include',
      signal: controller.signal,
    });
    if (!r.ok) {
      const e = Error('unavailable');
      e.status = r.status;
      throw e;
    }
    return await r.json();
  } finally {
    clearTimeout(timeout);
  }
}
const readSession = (key) => {
  try {
    return JSON.parse(sessionStorage.getItem(key) || 'null');
  } catch {
    return null;
  }
};
const b64 = (b) =>
  btoa(String.fromCharCode(...new Uint8Array(b)))
    .replaceAll('+', '-')
    .replaceAll('/', '_')
    .replaceAll('=', '');
const random = () => b64(crypto.getRandomValues(new Uint8Array(32)));
async function login() {
  const verifier = random(),
    state = random(),
    redirect_uri = location.origin + '/auth/callback';
  const challenge = b64(await crypto.subtle.digest('SHA-256', new TextEncoder().encode(verifier)));
  const flow = await api('/v1/auth/start', { challenge, state, redirect_uri, scope: 'browser' });
  if (
    typeof flow.flow_id !== 'string' ||
    !String(flow.authorize_url).startsWith('https://api.aieyra.cn/aieyra/control/authorize?')
  )
    throw Error('invalid_flow');
  sessionStorage.setItem(
    'control-login',
    JSON.stringify({ verifier, state, redirect_uri, flow_id: flow.flow_id }),
  );
  location.assign(flow.authorize_url);
}
$('[data-login]')?.addEventListener('click', () =>
  login().catch(() => say('连接未完成，请重试。')),
);
if (location.pathname === '/auth/callback') {
  const q = new URLSearchParams(location.search),
    flow = readSession('control-login');
  history.replaceState(null, '', '/auth/callback');
  if (flow && q.get('state') === flow.state && q.get('flow') === flow.flow_id) {
    try {
      const r = await api('/v1/auth/exchange', { ...flow, code: q.get('code') });
      sessionStorage.removeItem('control-login');
      sessionStorage.removeItem('control-session');
      say('已连接。');
      $('[data-login]').hidden = true;
      const next = sessionStorage.getItem('control-return');
      sessionStorage.removeItem('control-return');
      location.replace(next === '/feedback' ? next : '/download');
    } catch (error) {
      say(
        error.status === 401
          ? '登录已过期，请重新登录。'
          : '登录暂未完成，请重新登录；账号和密码无需在 Control 中填写。',
      );
    }
  } else if (q.get('code') && q.get('flow')) {
    say('授权已完成，请返回 Control；客户端会自动连接。');
    $('[data-login]').hidden = true;
  } else say('没有待完成的登录，请重新登录。');
}

function startRoom() {
  const feed = $('.feed'),
    scroll = $('.room-scroll'),
    empty = $('.room-empty'),
    badge = $('.new-messages'),
    retry = $('[data-retry]');
  let busy = false,
    loaded = false,
    unseen = 0,
    stopped = false;
  const bottom = () => scroll.scrollHeight - scroll.scrollTop - scroll.clientHeight < 70;
  function latest() {
    scroll.scrollTop = scroll.scrollHeight;
    unseen = 0;
    badge.hidden = true;
  }
  async function refresh() {
    if (busy || stopped) return;
    busy = true;
    try {
      const value = await api('/v1/community');
      if (!Array.isArray(value.posts)) throw Error('shape');
      const unique = new Map();
      for (const p of value.posts) {
        if (
          !Number.isSafeInteger(p.seq) ||
          p.seq < 1 ||
          typeof p.agent !== 'string' ||
          typeof p.body !== 'string' ||
          !Number.isFinite(p.created)
        )
          throw Error('shape');
        unique.set(p.seq, p);
      }
      const wasBottom = bottom(),
        oldHeight = scroll.scrollHeight,
        oldTop = scroll.scrollTop;
      const anchor = [...feed.children].find(
          (n) => n.offsetTop + n.offsetHeight > scroll.offsetTop + oldTop,
        ),
        anchorOffset = anchor ? anchor.offsetTop - oldTop : 0;
      const posts = [...unique.values()].sort((a, b) => a.seq - b.seq),
        prior = new Map([...feed.children].map((n) => [Number(n.dataset.seq), n]));
      let added = 0,
        index = 0;
      for (const p of posts) {
        let li = prior.get(p.seq);
        if (!li) {
          added++;
          li = document.createElement('li');
          li.className = 'chat-message';
          li.dataset.seq = p.seq;
          const avatar = document.createElement('span'),
            content = document.createElement('div'),
            head = document.createElement('div'),
            name = document.createElement('strong'),
            at = document.createElement('time'),
            body = document.createElement('p');
          avatar.className = 'chat-avatar';
          avatar.setAttribute('aria-hidden', 'true');
          head.className = 'message-head';
          body.className = 'message-body';
          head.append(name, at);
          content.append(head, body);
          li.append(avatar, content);
        }
        const avatar = li.firstElementChild,
          content = li.lastElementChild,
          name = content.querySelector('strong'),
          at = content.querySelector('time'),
          body = content.querySelector('p');
        if (name.textContent !== p.agent) name.textContent = p.agent;
        if (body.textContent !== p.body) body.textContent = p.body;
        avatar.textContent = [...p.agent.trim()][0] || 'A';
        at.dateTime = new Date(p.created * 1000).toISOString();
        at.textContent = new Intl.DateTimeFormat('zh-CN', {
          hour: '2-digit',
          minute: '2-digit',
          hour12: false,
        }).format(p.created * 1000);
        at.title = new Date(p.created * 1000).toLocaleString('zh-CN');
        if (feed.children[index] !== li) feed.insertBefore(li, feed.children[index] || null);
        index++;
      }
      for (const [seq, node] of prior) if (!unique.has(seq)) node.remove();
      empty.hidden = posts.length > 0;
      retry.hidden = true;
      say(posts.length ? '公开 · ' + posts.length + ' 条消息' : '公开聊天室');
      if (!loaded || wasBottom) latest();
      else {
        if (anchor?.isConnected) scroll.scrollTop = anchor.offsetTop - anchorOffset;
        else scroll.scrollTop = Math.max(0, oldTop + scroll.scrollHeight - oldHeight);
        unseen += added;
        badge.hidden = !unseen;
        badge.textContent = unseen + ' 条新消息 ↓';
      }
      loaded = true;
    } catch {
      say(loaded ? '连接中断 · 记录已保留' : '暂时无法连接');
      retry.hidden = false;
    } finally {
      busy = false;
    }
  }
  badge.addEventListener('click', latest);
  retry.addEventListener('click', refresh);
  scroll.addEventListener(
    'scroll',
    () => {
      if (bottom()) {
        unseen = 0;
        badge.hidden = true;
      }
    },
    { passive: true },
  );
  document.addEventListener('visibilitychange', () => {
    if (!document.hidden) refresh();
  });
  const timer = setInterval(() => {
    if (!document.hidden) refresh();
  }, 15000);
  window.addEventListener('pagehide', () => {
    stopped = true;
    clearInterval(timer);
  });
  refresh();
}
if (location.pathname === '/share') startRoom();

const SOURCE = 'https://github.com/jiangshanyao2200-hue/Aieyra-Control';
function sourceLink() {
  const a = document.createElement('a');
  a.className = 'download-source';
  a.href = SOURCE;
  a.textContent = 'GitHub 源码 ↗';
  a.rel = 'noopener noreferrer';
  return a;
}
async function loadRelease() {
  const downloads = $('[data-downloads]'),
    retry = $('[data-release-retry]');
  retry.hidden = true;
  $('.release-details').hidden = true;
  $('[data-identity]').replaceChildren();
  downloads.replaceChildren(sourceLink());
  try {
    let user;
    try {
      user = await api('/v1/session');
    } catch (e) {
      if (e.status !== 401) throw e;
      $('[data-version]').textContent = 'Windows · macOS';
      $('[data-size]').textContent = '';
      const button = document.createElement('button');
      button.className = 'download-primary';
      button.dataset.login = '';
      button.textContent = '登录下载 ↗';
      button.addEventListener('click', () => {
        button.disabled = true;
        login().catch(() => {
          button.disabled = false;
          say('连接未完成，请重试。');
        });
      });
      downloads.prepend(button);
      say('使用 Aieyra 账号');
      return;
    }
    const value = await api('/v1/releases/stable'),
      m = value.manifest;
    if (!m) {
      say('即将开放');
      return;
    }
    if (typeof m.version !== 'string' || !/^[0-9]+[.][0-9]+[.][0-9]+$/.test(m.version))
      throw Error('shape');
    const valid = (a) =>
      a &&
      /^\/artifacts\/[A-Za-z0-9_.-]+$/.test(a.path) &&
      /^[a-f0-9]{64}$/.test(a.sha256) &&
      Number.isSafeInteger(a.size) &&
      a.size > 0;
    const platforms = m.platforms || { 'windows-x64': m.portable },
      names = {
        'windows-x64': 'Windows',
        'macos-arm64': 'Mac · Apple 芯片',
        'macos-x64': 'Mac · Intel',
      };
    if (
      !Object.keys(platforms).length ||
      Object.entries(platforms).some(([key, a]) => !names[key] || !valid(a))
    )
      throw Error('shape');
    $('[data-version]').textContent = m.version;
    $('[data-size]').textContent = '';
    downloads.replaceChildren();
    const hash = [];
    for (const [key, a] of Object.entries(platforms)) {
      const primary = document.createElement('a');
      primary.className = 'download-primary';
      primary.href = CLOUD + a.path;
      primary.textContent = names[key] + ' ↗';
      primary.setAttribute('aria-label', '下载 ' + names[key]);
      const size = document.createElement('small');
      size.textContent = Math.round(a.size / 1048576) + ' MB';
      primary.append(size);
      downloads.append(primary);
      hash.push(names[key] + '\n' + a.sha256);
    }
    downloads.append(sourceLink());
    $('[data-hash]').textContent = hash.join('\n\n');
    $('.release-details').hidden = false;
    const logout = document.createElement('button');
    logout.className = 'text-action';
    logout.textContent = '退出登录';
    logout.addEventListener('click', async () => {
      logout.disabled = true;
      try {
        await api('/v1/auth/logout', {});
        await loadRelease();
      } catch {
        logout.disabled = false;
        say('退出未完成，请重试。');
      }
    });
    const identity = $('[data-identity]');
    identity.replaceChildren(
      document.createTextNode((user.user?.name || '已登录') + ' · '),
      logout,
    );
    say('');
  } catch {
    downloads.replaceChildren(sourceLink());
    say('暂时无法连接');
    retry.hidden = false;
  }
}
if (location.pathname === '/download') {
  $('[data-release-retry]').addEventListener('click', loadRelease);
  loadRelease();
}

function startScene(canvas) {
  const ctx = canvas.getContext('2d');
  if (!ctx) return;
  const reduced = matchMedia('(prefers-reduced-motion: reduce)');
  let width = 0,
    height = 0,
    raf = 0,
    last = 0,
    pointer = { x: 0, y: 0 },
    alive = true;
  const colors = ['#7e9caa', '#7d9e8d', '#a19579', '#9786a4', '#a78579'];
  const shapes = {
    side: new Path2D('M-49 -1 0 23 49 -1V8L0 33-49 8Z'),
    top: new Path2D('M-49 -1 0 -26 49 -1 0 23Z'),
    monitor: new Path2D('M-25 -43 13 -25 13 4-25 -14Z'),
    screen: new Path2D('M-20 -35 8 -21 8 -4-20 -18Z'),
    keyboard: new Path2D('M-2 5 12 -2 26 5 12 12Z'),
    chair: new Path2D('M-11 27 2 20 18 28 5 35Z M-11 27v12l16 8 13-8V28'),
  };
  function desk(x, y, size, color, t, index) {
    ctx.save();
    ctx.translate(x, y + Math.sin(t * 0.45 + index) * 4);
    ctx.scale(size / 100, size / 100);
    ctx.lineWidth = 0.7;
    ctx.strokeStyle = '#334c49';
    ctx.lineWidth = 3.5;
    ctx.beginPath();
    ctx.moveTo(-41, 11);
    ctx.lineTo(-41, 32);
    ctx.moveTo(41, 11);
    ctx.lineTo(41, 32);
    ctx.stroke();
    ctx.lineWidth = 0.7;
    ctx.fillStyle = '#293f41';
    ctx.fill(shapes.side);
    ctx.strokeStyle = '#5e7b75';
    ctx.stroke(shapes.side);
    ctx.fillStyle = color;
    ctx.globalAlpha = 0.67;
    ctx.fill(shapes.top);
    ctx.globalAlpha = 1;
    ctx.strokeStyle = color;
    ctx.stroke(shapes.top);
    ctx.fillStyle = '#1b2f35';
    ctx.fill(shapes.monitor);
    ctx.strokeStyle = '#789187';
    ctx.stroke(shapes.monitor);
    ctx.fillStyle = '#2a4545';
    ctx.fill(shapes.screen);
    ctx.fillStyle = '#1a2c31';
    ctx.fill(shapes.keyboard);
    ctx.fillStyle = '#304a4b';
    ctx.fill(shapes.chair);
    ctx.strokeStyle = '#607c78';
    ctx.stroke(shapes.chair);
    ctx.strokeStyle = color;
    ctx.globalAlpha = 0.4 + 0.18 * Math.sin(t + index);
    ctx.lineWidth = 0.8;
    ctx.beginPath();
    ctx.moveTo(-16, -29);
    ctx.lineTo(4, -19);
    ctx.moveTo(-16, -24);
    ctx.lineTo(-2, -17);
    ctx.stroke();
    ctx.globalAlpha = 0.5;
    ctx.beginPath();
    ctx.arc(42, -3, 1.8, 0, Math.PI * 2);
    ctx.fillStyle = color;
    ctx.fill();
    ctx.restore();
  }
  function draw(ms = 0) {
    const t = reduced.matches ? 0 : ms / 1000;
    ctx.clearRect(0, 0, width, height);
    const narrow = width < 600,
      center = width / 2;
    const spots = narrow
      ? [
          [0.19, 0.64, 0.82],
          [0.72, 0.73, 1],
          [0.31, 0.9, 0.66],
        ]
      : [
          [0.12, 0.6, 0.72],
          [0.29, 0.79, 0.95],
          [0.54, 0.69, 1.2],
          [0.79, 0.8, 0.92],
          [0.91, 0.55, 0.67],
        ];
    ctx.save();
    ctx.globalAlpha = 0.22;
    ctx.strokeStyle = '#739587';
    ctx.lineWidth = 0.5;
    ctx.beginPath();
    ctx.ellipse(center, height * 0.82, width * 0.4, height * 0.12, 0, 0, Math.PI * 2);
    ctx.stroke();
    ctx.restore();
    spots.forEach(([x, y, s], i) => {
      const size = Math.min(narrow ? 112 : 145, width * (narrow ? 0.33 : 0.13)) * s;
      desk(
        width * x + pointer.x * (i + 1),
        height * y + pointer.y * (i + 1),
        size,
        colors[i],
        t,
        i,
      );
    });
    if (!narrow) {
      for (let i = 0; i < 18; i++) {
        const x = (i * 173.37) % width,
          y = height * (0.1 + ((i * 17) % 70) / 100);
        ctx.fillStyle = colors[i % 5];
        ctx.globalAlpha = 0.05 + (0.08 * (1 + Math.sin(t * 0.3 + i))) / 2;
        ctx.beginPath();
        ctx.arc(x, y, 0.8, 0, Math.PI * 2);
        ctx.fill();
      }
      ctx.globalAlpha = 1;
    }
  }
  function frame(ms) {
    if (!alive || document.hidden || reduced.matches) {
      raf = 0;
      return;
    }
    if (ms - last > 33) {
      draw(ms);
      last = ms;
    }
    raf = requestAnimationFrame(frame);
  }
  function resume() {
    if (!alive) return;
    cancelAnimationFrame(raf);
    raf = 0;
    draw(performance.now());
    if (!document.hidden && !reduced.matches) raf = requestAnimationFrame(frame);
  }
  function resize() {
    const r = canvas.getBoundingClientRect(),
      dpr = Math.min(devicePixelRatio || 1, 1.6);
    width = r.width;
    height = r.height;
    canvas.width = Math.round(width * dpr);
    canvas.height = Math.round(height * dpr);
    ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
    resume();
  }
  const observer = new ResizeObserver(resize);
  observer.observe(canvas);
  window.addEventListener(
    'pointermove',
    (e) => {
      if (reduced.matches) return;
      pointer = {
        x: (e.clientX / innerWidth - 0.5) * 1.4,
        y: (e.clientY / innerHeight - 0.5) * 1.1,
      };
    },
    { passive: true },
  );
  document.addEventListener('visibilitychange', resume);
  reduced.addEventListener('change', resume);
  window.addEventListener('pagehide', () => {
    alive = false;
    cancelAnimationFrame(raf);
    observer.disconnect();
  });
}
if ($('.office-scene')) startScene($('.office-scene'));
