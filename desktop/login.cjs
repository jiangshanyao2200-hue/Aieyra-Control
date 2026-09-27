'use strict';

function validLoginUrl(value) {
  try {
    const url = new URL(value);
    return (
      url.origin === 'https://api.aieyra.cn' &&
      !url.username &&
      !url.password &&
      !url.hash &&
      url.pathname === '/aieyra/control/authorize' &&
      [...url.searchParams.keys()].join() === 'flow' &&
      /^[A-Za-z0-9_-]{43}$/.test(url.searchParams.get('flow'))
    );
  } catch {
    return false;
  }
}

async function openLogin(url, shell) {
  if (!validLoginUrl(url)) return false;
  try {
    await shell.openExternal(url);
    return true;
  } catch {
    return false;
  }
}

module.exports = { validLoginUrl, openLogin };
