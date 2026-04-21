/**
 * Minerva Local Save — File System Access API + IndexedDB
 *
 * Дозволяє браузеру зберігати файли безпосередньо на ПК користувача
 * через window.showDirectoryPicker(). Handle зберігається в IndexedDB
 * і відновлюється між сесіями.
 *
 * Сумісність: Chrome 86+, Edge 86+ (не підтримується Firefox/Safari).
 */

(function () {
  'use strict';

  /* ─── Перевірка підтримки ─────────────────────────────────────────────── */
  var SUPPORTED = typeof window.showDirectoryPicker === 'function';

  /* ─── IndexedDB wrapper ───────────────────────────────────────────────── */
  var DB_NAME    = 'minerva_local_save';
  var DB_VERSION = 1;
  var STORE_NAME = 'handles';
  var HANDLE_KEY = 'selected_dir';

  function idbOpen() {
    return new Promise(function (resolve, reject) {
      var req = indexedDB.open(DB_NAME, DB_VERSION);
      req.onupgradeneeded = function (e) {
        e.target.result.createObjectStore(STORE_NAME);
      };
      req.onsuccess = function (e) { resolve(e.target.result); };
      req.onerror   = function ()  { reject(req.error); };
    });
  }

  function idbSave(handle) {
    return idbOpen().then(function (db) {
      return new Promise(function (resolve, reject) {
        var tx  = db.transaction(STORE_NAME, 'readwrite');
        var req = tx.objectStore(STORE_NAME).put(handle, HANDLE_KEY);
        tx.oncomplete = resolve;
        tx.onerror    = function () { reject(tx.error); };
      });
    });
  }

  function idbLoad() {
    return idbOpen().then(function (db) {
      return new Promise(function (resolve, reject) {
        var tx  = db.transaction(STORE_NAME, 'readonly');
        var req = tx.objectStore(STORE_NAME).get(HANDLE_KEY);
        req.onsuccess = function () { resolve(req.result || null); };
        req.onerror   = function () { reject(req.error); };
      });
    });
  }

  function idbClear() {
    return idbOpen().then(function (db) {
      return new Promise(function (resolve, reject) {
        var tx = db.transaction(STORE_NAME, 'readwrite');
        tx.objectStore(STORE_NAME).delete(HANDLE_KEY);
        tx.oncomplete = resolve;
        tx.onerror    = function () { reject(tx.error); };
      });
    });
  }

  /* ─── Перевірка / запит прав доступу ─────────────────────────────────── */
  /**
   * Перевіряє права на запис до handle.
   * Повертає Promise<boolean>.
   * Якщо права "prompt" — запитує у користувача (може показати діалог).
   */
  function checkPermission(handle) {
    return handle.queryPermission({ mode: 'readwrite' }).then(function (state) {
      if (state === 'granted') return true;
      if (state === 'prompt') {
        return handle.requestPermission({ mode: 'readwrite' }).then(function (s) {
          return s === 'granted';
        });
      }
      // 'denied'
      return false;
    });
  }

  /* ─── Основні публічні функції ────────────────────────────────────────── */

  /**
   * Повертає збережений handle якщо є і є доступ.
   * Якщо доступу немає або handle відсутній — повертає null.
   * Якщо handle застарів — видаляє з IndexedDB.
   */
  function getHandle() {
    if (!SUPPORTED) return Promise.resolve(null);
    return idbLoad().then(function (handle) {
      if (!handle) return null;
      return checkPermission(handle).then(function (ok) {
        if (ok) return handle;
        // Handle є, але доступ відхилено — прибираємо
        return idbClear().then(function () { return null; });
      });
    }).catch(function () { return null; });
  }

  /**
   * Відкриває picker для вибору папки.
   * Зберігає handle в IndexedDB.
   * Повертає Promise<FileSystemDirectoryHandle|null>.
   */
  function pickFolder() {
    if (!SUPPORTED) return Promise.resolve(null);
    return window.showDirectoryPicker({ mode: 'readwrite' }).then(function (handle) {
      return idbSave(handle).then(function () { return handle; });
    }).catch(function (e) {
      if (e.name === 'AbortError') return null; // користувач скасував
      throw e;
    });
  }

  /**
   * Забуває збережену папку (видаляє handle з IndexedDB).
   */
  function forgetFolder() {
    return idbClear();
  }

  /**
   * Отримує або створює вкладену ієрархію підпапок за масивом імен.
   * Якщо папка відсутня — створює її (create: true).
   * Приклад: getNestedDir(handle, ['2026-04-20', '98354383'])
   *   → handle / 2026-04-20 / 98354383 /
   */
  function getNestedDir(handle, parts) {
    return parts.reduce(function (promise, name) {
      return promise.then(function (dir) {
        return dir.getDirectoryHandle(name, { create: true });
      });
    }, Promise.resolve(handle));
  }

  /**
   * Записує Blob у файл всередині handle-директорії.
   * subfolders (опційно) — масив підпапок, які створяться автоматично.
   * Приклад: writeBlob(handle, 'file.pdf', blob, ['2026-04-20', '98354383'])
   *   → {handle}/2026-04-20/98354383/file.pdf
   */
  function writeBlob(handle, filename, blob, subfolders) {
    var dirPromise = (subfolders && subfolders.length)
      ? getNestedDir(handle, subfolders)
      : Promise.resolve(handle);

    return dirPromise.then(function (dir) {
      return dir.getFileHandle(filename, { create: true });
    }).then(function (fh) {
      return fh.createWritable();
    }).then(function (writable) {
      return writable.write(blob).then(function () {
        return writable.close();
      });
    });
  }

  /**
   * Завантажує файл за URL і зберігає в обрану папку.
   * subfolders (опційно) — масив підпапок: ['дата', 'номер_замовлення'].
   * Структура: {обрана_папка}/{subfolders[0]}/{subfolders[1]}/{filename}
   * Повертає Promise<{ok, folderName, path}> або {ok: false, reason}.
   */
  function saveUrlToFolder(url, filename, subfolders) {
    return getHandle().then(function (handle) {
      if (!handle) return { ok: false, reason: 'no_handle' };
      return fetch(url).then(function (r) {
        if (!r.ok) throw new Error('HTTP ' + r.status);
        return r.blob();
      }).then(function (blob) {
        return writeBlob(handle, filename, blob, subfolders).then(function () {
          // Формуємо читабельний шлях для відображення
          var parts = [handle.name].concat(subfolders || []).concat([filename]);
          return { ok: true, folderName: handle.name, path: parts.join('/') };
        });
      });
    }).catch(function (e) {
      return { ok: false, reason: e.message || String(e) };
    });
  }

  /* ─── Експорт у глобальний об'єкт ────────────────────────────────────── */
  window.MinervaLocalSave = {
    supported:      SUPPORTED,
    getHandle:      getHandle,
    pickFolder:     pickFolder,
    forgetFolder:   forgetFolder,
    writeBlob:      writeBlob,
    saveUrlToFolder: saveUrlToFolder,
  };

})();
