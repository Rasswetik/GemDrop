// Run with node --test tests/test_loader_frontend.cjs (no npm dependencies).
const {test} = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const source = fs.readFileSync(path.join(__dirname, '../static/js/loader.js'), 'utf8');

function setup(random = 0, lite = false) {
  const pending = [];
  const classes = () => {
    const values = new Set();
    return {add: x => values.add(x), remove: x => values.delete(x), contains: x => values.has(x),
      toggle: (x, on) => on ? values.add(x) : values.delete(x)};
  };
  const root = {classList: classes(), dataset: {}};
  const img = {hidden: true, classList: classes()};
  const context = vm.createContext({window: {matchMedia: () => ({matches: lite})},
    document: {getElementById: id => id === 'loader' ? root : img,
      documentElement: {classList: {contains: () => lite}}},
    Math: {random: () => random, floor: Math.floor},
    Image: function () { pending.push(this); }});
  vm.runInContext(source, context);
  return {root, img, pending, loader: context.window.GDLoader};
}

test('all seven uploaded artworks can be selected and their files exist', () => {
  const picked = new Set();
  for (let i = 0; i < 7; i++) {
    const {pending} = setup((i + .5) / 7);
    const src = pending[0].src;
    assert(fs.existsSync(path.join(__dirname, '..', src)));
    picked.add(path.basename(src));
  }
  assert.deepEqual([...picked].sort(), ['calendar.png', 'crown.png', 'cup.png', 'fire.png', 'gift.png', 'logo.gif', 'wallet.webp']);
});

test('the inline fallback stays visible until the image loads; settings do not reroll or restart it', () => {
  const {root, img, pending, loader} = setup();
  assert.equal(img.hidden, true);
  assert.equal(root.classList.contains('custom-img'), false);
  loader.set('builtin');
  assert.equal(pending.length, 1);
  pending[0].onload();
  assert.equal(img.hidden, false);
  assert.equal(root.classList.contains('custom-img'), true);
  assert.equal(root.dataset.loaderArt, 'calendar');
  loader.set('builtin');
  assert.equal(pending.length, 1);
});

test('a slow earlier artwork cannot overwrite a newer admin selection', () => {
  const {root, img, pending, loader} = setup();
  loader.set('/static/gifs/custom.gif');
  pending[1].onload();
  pending[0].onload();
  assert.equal(img.src, '/static/gifs/custom.gif');
  assert.equal(root.dataset.loaderArt, 'custom');
});

test('a failed image restores the inline crystal and can be retried', () => {
  const {root, img, pending, loader} = setup();
  pending[0].onload();
  loader.set('/static/gifs/missing.gif');
  pending[1].onerror();
  assert.equal(img.hidden, true);
  assert.equal(root.classList.contains('custom-img'), false);
  assert.equal(root.dataset.loaderArt, undefined);
  loader.set('/static/gifs/missing.gif');
  assert.equal(pending.length, 3);
});

test('reduced motion uses still frames for both animated assets', () => {
  for (const index of [1, 6]) {
    const {img, pending} = setup((index + .5) / 7, true);
    assert(pending[0].src.endsWith('-poster.png'));
    assert(fs.existsSync(path.join(__dirname, '..', pending[0].src)));
    pending[0].onload();
    assert.equal(img.classList.contains('ld-drift'), false);
  }
});
