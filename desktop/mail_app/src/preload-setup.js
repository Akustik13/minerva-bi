'use strict';
// Лише для вікна «Підключення до Minerva» (src/setup.html, file://). Сторінки Minerva цього не отримують.
const { contextBridge, ipcRenderer } = require('electron');

if (location.protocol === 'file:') {
  contextBridge.exposeInMainWorld('minervaSetup', {
    init: () => ipcRenderer.invoke('setup:init'),
    connect: (data) => ipcRenderer.invoke('setup:connect', data),
    cancel: () => ipcRenderer.invoke('setup:cancel'),
  });
}
