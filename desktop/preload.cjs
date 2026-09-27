'use strict';
const { contextBridge, ipcRenderer } = require('electron');
contextBridge.exposeInMainWorld(
  'controlPlatform',
  Object.freeze({
    status: () => ipcRenderer.invoke('control:status'),
    openLogin: (url) => ipcRenderer.invoke('control:open-login', url),
    retry: () => ipcRenderer.invoke('control:retry'),
    background: (enabled) => ipcRenderer.invoke('control:background', Boolean(enabled)),
    quit: () => ipcRenderer.invoke('control:quit'),
    humanRequests: Object.freeze({
      status: () => ipcRenderer.invoke('control:human-status'),
      snooze: (minutes) => ipcRenderer.invoke('control:human-snooze', minutes),
      enabled: (value) => ipcRenderer.invoke('control:human-enabled', value),
      open: (id) => ipcRenderer.invoke('control:human-open', id),
    }),
    onHumanRequestOpen: (handler) => {
      if (typeof handler !== 'function') throw new TypeError('A callback is required');
      const listener = (_event, reference) => {
        Promise.resolve()
          .then(() => handler(reference))
          .then(() => ipcRenderer.invoke('control:human-opened', reference.navigation_id))
          .catch(() => {});
      };
      ipcRenderer.on('control:human-open-request', listener);
      void ipcRenderer.invoke('control:human-ready');
      return () => ipcRenderer.removeListener('control:human-open-request', listener);
    },
  }),
);
