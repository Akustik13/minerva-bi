'use strict';
// npm test — правила навігації програми (src/nav.js), без Electron.
const test = require('node:test');
const assert = require('node:assert/strict');
const nav = require('../src/nav');

const S = 'https://sevskiy.minerva-bi.com';

test('normalizeServer: address only, https by default', () => {
  assert.equal(nav.normalizeServer('sevskiy.minerva-bi.com'), S);
  assert.equal(nav.normalizeServer(' https://sevskiy.minerva-bi.com/email/?x=1 '), S);
  assert.equal(nav.normalizeServer('http://192.168.2.123:8000/admin/'), 'http://192.168.2.123:8000');
  assert.equal(nav.normalizeServer('ftp://x.example'), null);
  assert.equal(nav.normalizeServer('https://user:pw@x.example'), null);
  assert.equal(nav.normalizeServer(''), null);
  assert.equal(nav.normalizeServer('javascript:alert(1)'), null);
});

test('classify: mail and login stay in the app, the rest goes to the browser', () => {
  for (const p of ['/email/', '/email/?msg=5&solo=1', '/email/compose/?to=a@b.c', '/email/message/7/attachment/0/',
    '/email/app/pair/?code=x', '/admin/login/?next=/email/', '/admin/logout/', '/admin/password_reset/',
    '/admin/reset/MQ/abc/', '/media/orders/digikey/1/a.pdf', '/rag/knowledge/12/', '/i18n/setlang/']) {
    assert.equal(nav.classify(S + p, S), 'app', p);
  }
  for (const p of ['/admin/sales/salesorder/', '/admin/crm/customer/3/change/', '/offers/quick/?thread=1',
    '/calendar/', '/guide/mail/inbox/', '/emailx/', '/rag/api/x/']) {
    assert.equal(nav.classify(S + p, S), 'browser', p);
  }
  assert.equal(nav.classify(S + '/', S), 'home');
  assert.equal(nav.classify(S + '/admin/', S), 'home');
  assert.equal(nav.classify('https://evil.example/email/', S), 'browser');
  assert.equal(nav.classify('http://sevskiy.minerva-bi.com/email/', S), 'browser');   // інша схема — інший сайт
  assert.equal(nav.classify('mailto:a@b.c', S), 'mailto');
  assert.equal(nav.classify('minerva-mail://pair?url=x&code=y', S), 'pair');
  assert.equal(nav.classify('file:///C:/Windows/', S), 'block');
  assert.equal(nav.classify('javascript:alert(1)', S), 'block');
  assert.equal(nav.classify('about:blank', S), 'app');
  assert.equal(nav.classify(S + '/email/', null), 'browser');
});

test('mailtoToCompose', () => {
  assert.equal(nav.mailtoToCompose('mailto:anna@x.example', S), S + '/email/compose/?to=anna%40x.example');
  const u = new URL(nav.mailtoToCompose(
    'mailto:a@x.example,b@x.example?Subject=Angebot%20A-1&cc=c@x.example&body=Hallo%0AWelt&bcc=d@x.example', S));
  assert.equal(u.pathname, '/email/compose/');
  assert.equal(u.searchParams.get('to'), 'a@x.example,b@x.example');
  assert.equal(u.searchParams.get('subject'), 'Angebot A-1');
  assert.equal(u.searchParams.get('cc'), 'c@x.example');
  assert.equal(u.searchParams.get('body'), 'Hallo\nWelt');
  assert.equal(u.searchParams.get('bcc'), null);
  assert.equal(nav.mailtoToCompose('mailto:', S), S + '/email/compose/');
  assert.equal(nav.mailtoToCompose('mailto:?to=z@x.example', S), S + '/email/compose/?to=z%40x.example');
  assert.equal(nav.mailtoToCompose('nonsense', S), S + '/email/compose/');
});

test('parsePair', () => {
  const code = 'AbC_12-xyz';
  const link = 'minerva-mail://pair?url=' + encodeURIComponent(S + '/') + '&code=' + code;
  assert.deepEqual(nav.parsePair(link), { server: S, code });
  assert.deepEqual(nav.parsePair('minerva-mail://pair/?url=sevskiy.minerva-bi.com&code=' + code), { server: S, code });
  assert.equal(nav.parsePair('minerva-mail://other?url=x&code=y'), null);
  assert.equal(nav.parsePair('minerva-mail://pair?url=ftp://x&code=y'), null);
  assert.equal(nav.parsePair('minerva-mail://pair?url=' + S + '&code=a%20b'), null);
  assert.equal(nav.parsePair('https://x.example/?url=a&code=b'), null);
  assert.equal(nav.pairUrl(S, code), S + '/email/app/pair/?code=' + code);
});

test('findArg', () => {
  assert.equal(nav.findArg(['C:\\app.exe', '--hidden']), null);
  assert.equal(nav.findArg(['app.exe', 'mailto:a@b.c']), 'mailto:a@b.c');
  assert.equal(nav.findArg(['app.exe', '--', 'minerva-mail://pair?x']), 'minerva-mail://pair?x');
});
