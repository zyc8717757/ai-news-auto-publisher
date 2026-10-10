const test = require('node:test');
const assert = require('node:assert/strict');
const { weight, shorten } = require('./text.cjs');
test('official Chinese, URL and grapheme counting', () => {
  assert.equal(weight('中文'), 4);
  assert.equal(weight('https://example.com/a/very/long/url'), 23);
  assert.equal(weight('👨‍👩‍👧‍👦'), 2);
  assert.equal(weight('a'.repeat(280)), 280);
  const result = shorten('中文'.repeat(100) + '👨‍👩‍👧‍👦', 50);
  assert.ok(weight(result) <= 50);
  assert.ok(result.endsWith('…'));
});
