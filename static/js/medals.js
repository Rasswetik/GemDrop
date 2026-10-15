/* GemDrop medals: animated SVG medals shared by the player app and the admin panel. window.GDMedals */
(function () {
  var N = 0;
  var PAL = {
    gold: ['#fff3b0', '#ffc933', '#b87a08', '#ffe58a'],
    silver: ['#ffffff', '#c3ccdb', '#6f7b90', '#e6ecf5'],
    bronze: ['#ffd2a8', '#e0894a', '#8a4a1c', '#f4b07c'],
    violet: ['#e6d0ff', '#a066ff', '#4a208f', '#c9a3ff'],
    teal: ['#c9fff0', '#38d6a8', '#117a63', '#8cf0d2']
  };
  var EMB = {
    pumpkin: '<path d="M-6 -17 L-3 -24 L0 -18 L3 -24 L6 -17Z" fill="#ffd34d" stroke="#a8741a" stroke-width="1"/><ellipse cx="0" cy="3" rx="21" ry="16" fill="#ff8a1f"/><path d="M-9 -12 Q-14 3 -9 17 M9 -12 Q14 3 9 17 M0 -13 V19" stroke="#c75a00" stroke-width="1.6" fill="none" opacity=".7"/><path d="M-10 -1 l5 -5 l1 6Z M10 -1 l-5 -5 l-1 6Z" fill="#3a1500"/><path d="M-8 9 L-4 6 L0 9 L4 6 L8 9 L4 13 L0 10 L-4 13Z" fill="#3a1500"/>',
    ghost: '<path d="M-17 17 V-3 a17 17 0 0 1 34 0 V17 l-6 -5 -5.5 5 -5.5 -5 -5.5 5 -5.500 -5Z" fill="#fff"/><ellipse cx="-6" cy="-2" rx="3" ry="4.200" fill="#2b1750"/><ellipse cx="6" cy="-2" rx="3" ry="4.200" fill="#2b1750"/><ellipse cx="0" cy="8" rx="3.500" ry="4.500" fill="#2b1750"/><circle cx="-11" cy="4" r="2.500" fill="#ffb3d1" opacity=".7"/><circle cx="11" cy="4" r="2.500" fill="#ffb3d1" opacity=".7"/>',
    bat: '<path d="M-27 -10 Q-17 -17 -10 -7 Q-5 -14 0 -5 Q5 -14 10 -7 Q17 -17 27 -10 Q24 5 13 7 Q9 2 5 9 L0 17 L-5 9 Q-9 2 -13 7 Q-24 5 -27 -10Z" fill="#2a1450"/><path d="M-5 -8 L-7 -15 L-2 -9Z M5 -8 L7 -15 L2 -9Z" fill="#2a1450"/><circle cx="-3.500" cy="-3" r="2" fill="#ffd34d"/><circle cx="3.500" cy="-3" r="2" fill="#ffd34d"/>',
    hat: '<ellipse cx="0" cy="13" rx="25" ry="5.500" fill="#2b1359"/><path d="M-13 13 Q-7 -9 7 -25 Q4 -8 13 13Z" fill="#3d1d82"/><path d="M-12 9 Q0 12 12 9 L13 13 Q0 16 -13 13Z" fill="#ff8a1f"/><rect x="-3.500" y="8.500" width="7" height="6" rx="1" fill="none" stroke="#ffd34d" stroke-width="1.600"/><path d="M8 -20 l1.800 -4 l1.800 4 l4 1.800 l-4 1.800 l-1.800 4 l-1.800 -4 l-4 -1.800Z" fill="#ffe58a"/>',
    skull: '<path d="M-14 -2 a14 14 0 0 1 28 0 v8 q0 4 -4 5 v5 h-20 v-5 q-4 -1 -4 -5Z" fill="#f2efe6"/><circle cx="-6" cy="0" r="4.500" fill="#2b1750"/><circle cx="6" cy="0" r="4.500" fill="#2b1750"/><path d="M0 5 l-2.500 5 h5Z" fill="#2b1750"/><path d="M-6 13 v5 M-2 13 v5 M2 13 v5 M6 13 v5" stroke="#2b1750" stroke-width="1.400"/>',
    star: '<path d="M0 -20 L5.900 -6.500 L20.500 -5.500 L9.300 4 L12.800 18.500 L0 10.800 L-12.800 18.500 L-9.300 4 L-20.500 -5.500 L-5.900 -6.500Z" fill="#fff6cf" stroke="rgba(0,0,0,.25)" stroke-width="1"/>'
  };
  var LIST = [
    {key: 'hw1', name: 'Король Хэллоуина', place: 1, pal: 'gold', emb: 'pumpkin', set: 'Хэллоуин'},
    {key: 'hw2', name: 'Призрачный мастер', place: 2, pal: 'silver', emb: 'ghost', set: 'Хэллоуин'},
    {key: 'hw3', name: 'Повелитель теней', place: 3, pal: 'bronze', emb: 'bat', set: 'Хэллоуин'},
    {key: 'hw4', name: 'Ведьмин избранник', place: 4, pal: 'violet', emb: 'hat', set: 'Хэллоуин'},
    {key: 'hw5', name: 'Хранитель костей', place: 5, pal: 'teal', emb: 'skull', set: 'Хэллоуин'},
    {key: 'c1', name: 'Золотая медаль', place: 1, pal: 'gold', emb: 'star', set: 'Классика'},
    {key: 'c2', name: 'Серебряная медаль', place: 2, pal: 'silver', emb: 'star', set: 'Классика'},
    {key: 'c3', name: 'Бронзовая медаль', place: 3, pal: 'bronze', emb: 'star', set: 'Классика'}
  ];
  var BY = {}; LIST.forEach(function (m) { BY[m.key] = m; });

  function css() {
    if (document.getElementById('gdMedalCss')) return;
    var s = document.createElement('style'); s.id = 'gdMedalCss';
    s.textContent = '.gdm{display:inline-block;vertical-align:middle;overflow:visible}' +
      '.gdm .gm-glow{animation:gmGlow 2.8s ease-in-out infinite;transform-origin:48px 62px}' +
      '.gdm .gm-emb{animation:gmBob 3s ease-in-out infinite;transform-origin:0 0}' +
      '.gdm .gm-shine{animation:gmShine 3.4s ease-in-out infinite}' +
      '.gdm .gm-sp{animation:gmTw 2.2s ease-in-out infinite;transform-origin:center;transform-box:fill-box}' +
      '.gdm .gm-sp.b{animation-delay:.7s}.gdm .gm-sp.c{animation-delay:1.4s}' +
      '.gdm .gm-rb{animation:gmSway 4s ease-in-out infinite;transform-origin:48px 14px}' +
      '@keyframes gmGlow{0%,100%{opacity:.35;transform:scale(1)}50%{opacity:.75;transform:scale(1.08)}}' +
      '@keyframes gmBob{0%,100%{transform:translateY(0)}50%{transform:translateY(-1.8px)}}' +
      '@keyframes gmShine{0%,55%{transform:translateX(-70px)}100%{transform:translateX(70px)}}' +
      '@keyframes gmTw{0%,100%{opacity:0;transform:scale(.3)}50%{opacity:1;transform:scale(1)}}' +
      '@keyframes gmSway{0%,100%{transform:rotate(-1.2deg)}50%{transform:rotate(1.2deg)}}' +
      '@media(prefers-reduced-motion:reduce){.gdm *{animation:none!important}}html.hw-still .gdm *,html.perf-lite .gdm *{animation:none!important}';
    document.head.appendChild(s);
  }

  function svg(key, size, still) {
    css();
    var m = BY[key] || LIST[0], p = PAL[m.pal], id = 'gm' + (++N), w = size || 64, h = Math.round(w * 1.25);
    var rb = m.pal === 'violet' ? ['#ff8a1f', '#c75a00'] : m.pal === 'teal' ? ['#ff8a1f', '#c75a00'] : ['#d6263b', '#8f1424'];
    return '<svg class="gdm" width="' + w + '" height="' + h + '" viewBox="0 0 96 120" aria-hidden="true"' + (still ? ' style="--s:1"' : '') + '>' +
      '<defs><linearGradient id="' + id + 'r" x1="0" y1="0" x2="1" y2="1"><stop offset="0" stop-color="' + p[0] + '"/><stop offset=".5" stop-color="' + p[1] + '"/><stop offset="1" stop-color="' + p[2] + '"/></linearGradient>' +
      '<radialGradient id="' + id + 'd" cx=".35" cy=".3" r=".9"><stop offset="0" stop-color="' + p[3] + '"/><stop offset="1" stop-color="' + p[1] + '"/></radialGradient>' +
      '<linearGradient id="' + id + 's" x1="0" x2="1"><stop offset="0" stop-color="#fff" stop-opacity="0"/><stop offset=".5" stop-color="#fff" stop-opacity=".65"/><stop offset="1" stop-color="#fff" stop-opacity="0"/></linearGradient>' +
      '<clipPath id="' + id + 'c"><circle cx="48" cy="62" r="31"/></clipPath></defs>' +
      '<circle class="gm-glow" cx="48" cy="62" r="40" fill="' + p[1] + '" opacity=".4"/>' +
      '<g class="gm-rb"><path d="M30 6 L48 44 L40 48 L22 12Z" fill="' + rb[0] + '"/><path d="M66 6 L48 44 L56 48 L74 12Z" fill="' + rb[1] + '"/></g>' +
      '<circle cx="48" cy="62" r="38" fill="url(#' + id + 'r)"/>' +
      '<circle cx="48" cy="62" r="33.500" fill="none" stroke="rgba(0,0,0,.28)" stroke-width="1.500"/>' +
      '<circle cx="48" cy="62" r="31" fill="url(#' + id + 'd)"/>' +
      '<circle cx="48" cy="62" r="31" fill="rgba(24,10,48,.38)"/>' +
      '<g transform="translate(48 60) scale(.92)"><g class="gm-emb">' + EMB[m.emb] + '</g></g>' +
      '<g clip-path="url(#' + id + 'c)"><rect class="gm-shine" x="10" y="26" width="26" height="72" fill="url(#' + id + 's)" transform="skewX(-18)"/></g>' +
      '<path d="M48 4 l1.600 3.600 l3.800 .5 l-2.800 2.600 l.7 3.800 l-3.300 -1.900 l-3.300 1.900 l.7 -3.800 l-2.800 -2.600 l3.800 -.5Z" fill="' + p[3] + '" opacity=".0"/>' +
      '<circle class="gm-sp" cx="20" cy="34" r="2.400" fill="#fff"/><circle class="gm-sp b" cx="78" cy="48" r="2" fill="#fff"/><circle class="gm-sp c" cx="72" cy="96" r="2.200" fill="#fff"/>' +
      '<text x="48" y="107" text-anchor="middle" font-family="system-ui,sans-serif" font-size="11" font-weight="900" fill="' + p[3] + '" stroke="rgba(0,0,0,.45)" stroke-width="2.200" paint-order="stroke">' + m.place + '</text>' +
      '</svg>';
  }
  window.GDMedals = {LIST: LIST, by: function (k) { return BY[k] || null; }, svg: svg, name: function (k) { return (BY[k] || {}).name || 'Медаль'; }};
})();
