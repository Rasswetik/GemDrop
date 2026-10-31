/* One bundled artwork per page load. Keep the inline crystal until it is ready. */
(function () {
  'use strict';
  var root = document.getElementById('loader');
  var img = document.getElementById('loaderImage');
  if (!root || !img) return;

  var base = '/static/gifs/loader/';
  var artworks = [
    {name: 'calendar', file: 'calendar.png', still: true},
    {name: 'wallet', file: 'wallet.webp', poster: 'wallet-poster.png'},
    {name: 'crown', file: 'crown.png', still: true},
    {name: 'cup', file: 'cup.png', still: true},
    {name: 'fire', file: 'fire.png', still: true},
    {name: 'gift', file: 'gift.png', still: true},
    {name: 'logo', file: 'logo.gif', poster: 'logo-poster.png'}
  ];
  var selected = artworks[Math.floor(Math.random() * artworks.length)];
  var request = 0, current = '';

  function set(path) {
    var bundled = !path || path === 'builtin';
    var art = bundled ? selected : {name: 'custom', file: path};
    var lite = document.documentElement.classList.contains('perf-lite') ||
      (window.matchMedia && window.matchMedia('(prefers-reduced-motion: reduce)').matches);
    var src = bundled ? base + (lite && art.poster ? art.poster : art.file) : path;
    // Settings can refresh during boot; do not restart the selected animation.
    if (src === current) return;
    current = src;
    var id = ++request, next = new Image();
    next.decoding = 'async';
    next.onload = function () {
      if (id !== request) return;
      img.src = src;
      img.hidden = false;
      img.classList.toggle('ld-drift', !!art.still && !lite);
      root.dataset.loaderArt = art.name;
      root.classList.add('custom-img');
    };
    next.onerror = function () {
      if (id !== request) return;
      current = '';
      img.hidden = true;
      img.classList.remove('ld-drift');
      root.classList.remove('custom-img');
      delete root.dataset.loaderArt;
    };
    next.src = src;
  }

  window.GDLoader = {set: set};
  set('builtin');
})();
