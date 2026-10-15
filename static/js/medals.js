/* GemDrop medals: animated SVG medals shared by the player app and the admin panel. window.GDMedals */
(function () {
  var N = 0;
  // palette: [light, mid, dark, accent, ribbonA, ribbonB, innerShade]
  var PAL = {
    gold: ['#fff3b0', '#ffc933', '#a86a05', '#ffe58a', '#d6263b', '#8f1424', 'rgba(90,40,0,.28)'],
    silver: ['#ffffff', '#c3ccdb', '#5f6b82', '#e6ecf5', '#d6263b', '#8f1424', 'rgba(20,30,60,.30)'],
    bronze: ['#ffd9b8', '#e0894a', '#7a3d14', '#f4b07c', '#d6263b', '#8f1424', 'rgba(70,25,0,.30)'],
    violet: ['#ecd9ff', '#a066ff', '#41197f', '#c9a3ff', '#ff8a1f', '#c75a00', 'rgba(24,6,60,.34)'],
    teal: ['#d2fff3', '#38d6a8', '#0e6b57', '#8cf0d2', '#ff8a1f', '#c75a00', 'rgba(0,40,30,.30)'],
    cyan: ['#e4fbff', '#4fd2f5', '#0f6f99', '#a6eeff', '#2a7be0', '#16479a', 'rgba(0,40,80,.30)'],
    blue: ['#dcecff', '#4d97ff', '#17408f', '#9cc6ff', '#ffc933', '#b87a08', 'rgba(5,20,70,.32)'],
    green: ['#e1ffd8', '#4fd36a', '#14702f', '#9df0ae', '#ffc933', '#b87a08', 'rgba(0,45,10,.30)'],
    red: ['#ffe0e0', '#ff5d6c', '#8f1424', '#ffa3ad', '#ffc933', '#b87a08', 'rgba(70,0,10,.30)'],
    pink: ['#ffe3ef', '#ff7aaa', '#9a2457', '#ffb3d1', '#a066ff', '#4a208f', 'rgba(80,0,40,.28)'],
    orange: ['#ffe8cc', '#ff9a2e', '#9a4a00', '#ffc58a', '#2a7be0', '#16479a', 'rgba(80,30,0,.30)'],
    night: ['#d8dcff', '#6a6fe0', '#25286e', '#aab0ff', '#ffc933', '#b87a08', 'rgba(8,8,50,.38)'],
    black: ['#aeb3cc', '#2b2e3b', '#06070c', '#d4d9f2', '#9aa0c0', '#3a3d50', 'rgba(0,0,0,.04)'],
    obsidian: ['#e2e6f5', '#6b7396', '#161a30', '#9aa3cf', '#a066ff', '#4a208f', 'rgba(0,0,10,.18)']
  };
  var EMB = {
    pumpkin: '<path d="M-6 -17 L-3 -24 L0 -18 L3 -24 L6 -17Z" fill="#7bd651" stroke="#2f7a1a" stroke-width="1"/><ellipse cx="0" cy="3" rx="21" ry="16" fill="#ff8a1f" stroke="#a94700" stroke-width="1"/><path d="M-9 -12 Q-14 3 -9 17 M9 -12 Q14 3 9 17 M0 -13 V19" stroke="#c75a00" stroke-width="1.6" fill="none" opacity=".75"/><path d="M-10 -1 l5 -5 l1 6Z M10 -1 l-5 -5 l-1 6Z" fill="#3a1500"/><path d="M-8 9 L-4 6 L0 9 L4 6 L8 9 L4 13 L0 10 L-4 13Z" fill="#3a1500"/><ellipse cx="-9" cy="-4" rx="3" ry="5" fill="#fff" opacity=".22"/>',
    ghost: '<path d="M-17 17 V-3 a17 17 0 0 1 34 0 V17 l-6 -5 -5.5 5 -5.5 -5 -5.5 5 -5.500 -5Z" fill="#fff" stroke="#b8a8e0" stroke-width="1"/><ellipse cx="-6" cy="-2" rx="3" ry="4.200" fill="#2b1750"/><ellipse cx="6" cy="-2" rx="3" ry="4.200" fill="#2b1750"/><ellipse cx="0" cy="8" rx="3.500" ry="4.500" fill="#2b1750"/><circle cx="-11" cy="4" r="2.500" fill="#ffb3d1" opacity=".75"/><circle cx="11" cy="4" r="2.500" fill="#ffb3d1" opacity=".75"/>',
    bat: '<path d="M-27 -10 Q-17 -17 -10 -7 Q-5 -14 0 -5 Q5 -14 10 -7 Q17 -17 27 -10 Q24 5 13 7 Q9 2 5 9 L0 17 L-5 9 Q-9 2 -13 7 Q-24 5 -27 -10Z" fill="#2a1450" stroke="#7a4cc0" stroke-width="1"/><path d="M-5 -8 L-7 -15 L-2 -9Z M5 -8 L7 -15 L2 -9Z" fill="#2a1450"/><circle cx="-3.500" cy="-3" r="2" fill="#ffd34d"/><circle cx="3.500" cy="-3" r="2" fill="#ffd34d"/>',
    hat: '<ellipse cx="0" cy="13" rx="25" ry="5.500" fill="#2b1359"/><path d="M-13 13 Q-7 -9 7 -25 Q4 -8 13 13Z" fill="#4a24a0"/><path d="M-12 9 Q0 12 12 9 L13 13 Q0 16 -13 13Z" fill="#ff8a1f"/><rect x="-3.500" y="8.500" width="7" height="6" rx="1" fill="none" stroke="#ffd34d" stroke-width="1.600"/><path d="M8 -20 l1.800 -4 l1.800 4 l4 1.800 l-4 1.800 l-1.800 4 l-1.800 -4 l-4 -1.800Z" fill="#ffe58a"/>',
    skull: '<path d="M-14 -2 a14 14 0 0 1 28 0 v8 q0 4 -4 5 v5 h-20 v-5 q-4 -1 -4 -5Z" fill="#f6f2e8" stroke="#b9b29c" stroke-width="1"/><circle cx="-6" cy="0" r="4.500" fill="#2b1750"/><circle cx="6" cy="0" r="4.500" fill="#2b1750"/><path d="M0 5 l-2.500 5 h5Z" fill="#2b1750"/><path d="M-6 13 v5 M-2 13 v5 M2 13 v5 M6 13 v5" stroke="#2b1750" stroke-width="1.400"/>',
    star: '<path d="M0 -21 L6.100 -7 L21 -5.800 L9.600 4 L13.200 19 L0 11.200 L-13.200 19 L-9.600 4 L-21 -5.800 L-6.100 -7Z" fill="#fff6cf" stroke="rgba(0,0,0,.3)" stroke-width="1" stroke-linejoin="round"/><path d="M0 -14 L3.500 -5 L0 -2Z" fill="#fff" opacity=".7"/>',
    vip: '<g transform="translate(0 -5) scale(.9)"><path d="M-21 12 L-24 -13 L-10 -3 L0 -19 L10 -3 L24 -13 L21 12Z" fill="#ffe58a" stroke="#a8741a" stroke-width="1.3" stroke-linejoin="round"/><rect x="-21" y="12" width="42" height="6" rx="2" fill="#ffc933" stroke="#a8741a" stroke-width="1.3"/><circle cx="0" cy="-20" r="3.200" fill="#ff5d7a" stroke="#fff" stroke-width=".8"/><circle cx="-24" cy="-14" r="2.700" fill="#6be3ff" stroke="#fff" stroke-width=".8"/><circle cx="24" cy="-14" r="2.700" fill="#6be3ff" stroke="#fff" stroke-width=".8"/><circle cx="0" cy="4" r="3.800" fill="#ff5d7a" stroke="#fff" stroke-width=".9"/><path d="M-10 6 L-8 -1 M10 6 L8 -1" stroke="#fff6cf" stroke-width="1.6" stroke-linecap="round" opacity=".8"/></g><text x="0" y="22" text-anchor="middle" font-family="system-ui,sans-serif" font-size="10" font-weight="900" letter-spacing="1.5" fill="#fff" stroke="rgba(0,0,0,.5)" stroke-width="2" paint-order="stroke">VIP</text>',
    gem: '<path d="M-19 -6 L-11 -18 H11 L19 -6 L0 21Z" fill="#7fe8ff" stroke="#fff" stroke-width="1.300" stroke-linejoin="round"/><path d="M-19 -6 L-5 -6 L0 21Z" fill="#2fb4e6" opacity=".6"/><path d="M19 -6 L5 -6 L0 21Z" fill="#1b7fbf" opacity=".55"/><path d="M-11 -18 L-5 -6 H5 L11 -18Z" fill="#c8f8ff"/><path d="M-19 -6 H19 M-11 -18 L-5 -6 L0 21 L5 -6 L11 -18 M-5 -6 L0 -18 L5 -6" stroke="#fff" stroke-width="1" fill="none" opacity=".85"/><path d="M-14 -12 l2 -1" stroke="#fff" stroke-width="2" stroke-linecap="round"/>',
    ton: '<circle r="20" fill="#1a8fe8" stroke="#fff" stroke-width="1.600"/><circle r="15.500" fill="none" stroke="#fff" stroke-width=".8" opacity=".5"/><path d="M-11 -8 H11 L0 13Z" fill="none" stroke="#fff" stroke-width="3" stroke-linejoin="round"/><path d="M0 -8 V13" stroke="#fff" stroke-width="3"/>',
    clover: '<g fill="#3ddc84" stroke="#0f8a4a" stroke-width="1"><circle cx="-7" cy="-7" r="8"/><circle cx="7" cy="-7" r="8"/><circle cx="-7" cy="7" r="8"/><circle cx="7" cy="7" r="8"/></g><path d="M-7 -7 L7 7 M7 -7 L-7 7" stroke="#0f8a4a" stroke-width="1.400" opacity=".8"/><path d="M2 12 Q6 19 12 22" stroke="#0f8a4a" stroke-width="3" fill="none" stroke-linecap="round"/><circle cx="-9" cy="-10" r="2.500" fill="#fff" opacity=".35"/>',
    flame: '<path d="M0 -23 C4 -12 16 -8 15 6 C15 15 8 21 0 21 C-8 21 -15 15 -14 6 C-14 0 -9 -3 -7 -8 C-4 -4 -2 -3 0 -23Z" fill="#ff7a1a" stroke="#b83a00" stroke-width="1"/><path d="M0 -4 C3 2 9 4 8 11 C8 16 4 19 0 19 C-4 19 -8 16 -8 11 C-8 6 -3 4 0 -4Z" fill="#ffd34d"/><path d="M0 8 C2 11 4 12 3.500 15 C3 17 1.500 18 0 18 C-1.500 18 -3 17 -3.500 15 C-4 12 -1 11 0 8Z" fill="#fff6cf"/>',
    bolt: '<path d="M6 -23 L-13 3 H-1 L-5 23 L14 -4 H2Z" fill="#fff2a0" stroke="#b8860b" stroke-width="1.300" stroke-linejoin="round"/><path d="M5 -17 L-6 1 H2Z" fill="#fff" opacity=".6"/>',
    rocket: '<path d="M0 -23 C9 -15 10 -1 8 10 H-8 C-10 -1 -9 -15 0 -23Z" fill="#f1f5ff" stroke="#6f7b90" stroke-width="1.100"/><circle cx="0" cy="-6" r="4.800" fill="#3aa0ff" stroke="#6f7b90"/><path d="M-8 3 L-17 14 L-8 11Z M8 3 L17 14 L8 11Z" fill="#ff5d7a" stroke="#8f1424" stroke-width=".8"/><path d="M-4.500 12 H4.500 L0 23Z" fill="#ffb02e"/>',
    heart: '<path d="M0 20 C-25 3 -17 -17 -7 -17 C-3 -17 0 -14 0 -11 C0 -14 3 -17 7 -17 C17 -17 25 3 0 20Z" fill="#ff5d85" stroke="#fff" stroke-width="1.300" stroke-linejoin="round"/><path d="M-13 -8 Q-10 -12 -6 -11" stroke="#fff" stroke-width="2.200" fill="none" opacity=".75" stroke-linecap="round"/>',
    moon: '<path d="M8 -21 A21 21 0 1 0 8 21 A16 16 0 1 1 8 -21Z" fill="#ffe9a8" stroke="#c9a23a" stroke-width="1"/><path d="M14 -9 l1.600 3.200 3.200 1.600 -3.200 1.600 -1.600 3.200 -1.600 -3.200 -3.200 -1.600 3.200 -1.600Z" fill="#fff"/><circle cx="-2" cy="-8" r="2" fill="#e3c66d" opacity=".7"/>',
    trophy: '<path d="M-12 -19 H12 V-8 C12 2 6 8 0 8 C-6 8 -12 2 -12 -8Z" fill="#ffd34d" stroke="#a8741a" stroke-width="1.300"/><path d="M-12 -15 H-19 C-19 -4 -15 0 -10 0 M12 -15 H19 C19 -4 15 0 10 0" fill="none" stroke="#e0a21a" stroke-width="2.600"/><rect x="-3" y="8" width="6" height="7" fill="#e0a21a"/><rect x="-10" y="15" width="20" height="5" rx="2" fill="#b87a08" stroke="#7a4a05" stroke-width=".8"/><path d="M-6 -15 V-6" stroke="#fff6cf" stroke-width="2.200" stroke-linecap="round" opacity=".75"/>',
    gift: '<rect x="-17" y="-6" width="34" height="25" rx="3" fill="#ff5d7a" stroke="#fff" stroke-width="1"/><rect x="-19.500" y="-13" width="39" height="9" rx="3" fill="#ff8aa0" stroke="#fff" stroke-width="1"/><rect x="-3.200" y="-13" width="6.400" height="32" fill="#ffe58a"/><path d="M0 -13 C-6 -25 -17 -18 -9 -13 M0 -13 C6 -25 17 -18 9 -13" fill="none" stroke="#ffe58a" stroke-width="2.500" stroke-linecap="round"/>',
    dice: '<rect x="-17" y="-17" width="34" height="34" rx="8" fill="#fff" stroke="#b0b8c8" stroke-width="1.300"/><g fill="#d6263b"><circle cx="-8" cy="-8" r="3.300"/><circle cx="8" cy="-8" r="3.300"/><circle cx="0" cy="0" r="3.300"/><circle cx="-8" cy="8" r="3.300"/><circle cx="8" cy="8" r="3.300"/></g>',
    shield: '<path d="M0 -22 L17 -15 V-1 C17 11 9 19 0 23 C-9 19 -17 11 -17 -1 V-15Z" fill="#eef2f9" stroke="#5f6b82" stroke-width="1.300" stroke-linejoin="round"/><path d="M0 -22 L17 -15 V-1 C17 11 9 19 0 23Z" fill="#c3ccdb"/><path d="M-8 0 L-2 6 L9 -6" stroke="#22a05f" stroke-width="3.600" fill="none" stroke-linecap="round" stroke-linejoin="round"/>',
    mine: '<g stroke="#262c45" stroke-width="3.200" stroke-linecap="round"><path d="M0 -23 V23 M-23 0 H23 M-16 -16 L16 16 M16 -16 L-16 16"/></g><circle r="14.500" fill="#3a4262" stroke="#1a1f34" stroke-width="1"/><circle cx="-5" cy="-5" r="4.200" fill="#9aa6d0" opacity=".7"/><circle cx="3" cy="-18" r="2.400" fill="#ff6a3a"/>',
    spade: '<path d="M0 -20 C-6 -9 -19 -5 -19 4 C-19 11 -11 13 -7 8 C-8 14 -10 18 -13 20 H13 C10 18 8 14 7 8 C11 13 19 11 19 4 C19 -5 6 -9 0 -20Z" fill="#f4f6ff" stroke="#9aa3cf" stroke-width="1.100" stroke-linejoin="round"/><path d="M-9 -1 Q-10 3 -8 6" stroke="#9aa3cf" stroke-width="1.600" fill="none" stroke-linecap="round" opacity=".7"/>',
    flag: '<path d="M-12 -22 V23" stroke="#6f4a1a" stroke-width="3.200" stroke-linecap="round"/><path d="M-11 -21 H18 L11 -12 L18 -3 H-11Z" fill="#fff" stroke="#c75a00" stroke-width="1" stroke-linejoin="round"/><path d="M-11 -21 H4 L-3 -12 L4 -3 H-11Z" fill="#ff8a1f"/><circle cx="-12" cy="-23" r="2.500" fill="#ffd34d"/>',
    key: '<circle cx="-8" cy="-8" r="9.500" fill="none" stroke="#fff2a0" stroke-width="4.600"/><path d="M-1 -1 L17 17 M11 11 L16 6 M15 15 L20 10" stroke="#fff2a0" stroke-width="4.200" stroke-linecap="round" fill="none"/><circle cx="-8" cy="-8" r="3" fill="#fff" opacity=".5"/>'
  };
  var LIST = [
    {key: 'hw1', name: 'Король Хэллоуина', place: 1, pal: 'gold', emb: 'pumpkin', set: 'Хэллоуин', shape: 'seal'},
    {key: 'hw2', name: 'Призрачный мастер', place: 2, pal: 'silver', emb: 'ghost', set: 'Хэллоуин', shape: 'seal'},
    {key: 'hw3', name: 'Повелитель теней', place: 3, pal: 'bronze', emb: 'bat', set: 'Хэллоуин', shape: 'seal'},
    {key: 'hw4', name: 'Ведьмин избранник', place: 4, pal: 'violet', emb: 'hat', set: 'Хэллоуин', shape: 'hex'},
    {key: 'hw5', name: 'Хранитель костей', place: 5, pal: 'teal', emb: 'skull', set: 'Хэллоуин', shape: 'hex'},
    {key: 'c1', name: 'Золотая медаль', place: 1, pal: 'gold', emb: 'star', set: 'Классика'},
    {key: 'c2', name: 'Серебряная медаль', place: 2, pal: 'silver', emb: 'star', set: 'Классика'},
    {key: 'c3', name: 'Бронзовая медаль', place: 3, pal: 'bronze', emb: 'star', set: 'Классика'},
    {key: 'vip', name: 'VIP игрок', pal: 'violet', emb: 'vip', set: 'Особые', shape: 'seal'},
    {key: 'gem', name: 'Кристаллик', pal: 'cyan', emb: 'gem', set: 'Особые', shape: 'hex'},
    {key: 'champ', name: 'Чемпион', pal: 'gold', emb: 'trophy', set: 'Особые', shape: 'seal'},
    {key: 'pioneer', name: 'Первопроходец', pal: 'orange', emb: 'flag', set: 'Особые'},
    {key: 'insider', name: 'Инсайдер', pal: 'cyan', emb: 'key', set: 'Особые', shape: 'hex'},
    {key: 'friend', name: 'Верный друг', pal: 'pink', emb: 'heart', set: 'Особые'},
    {key: 'guard', name: 'Защитник', pal: 'silver', emb: 'shield', set: 'Особые', shape: 'hex'},
    {key: 'collector', name: 'Коллекционер', pal: 'red', emb: 'gift', set: 'Особые'},
    {key: 'ton', name: 'Миллионер TON', pal: 'blue', emb: 'ton', set: 'Достижения', shape: 'seal'},
    {key: 'lucky', name: 'Счастливчик', pal: 'green', emb: 'clover', set: 'Достижения'},
    {key: 'fire', name: 'Горячая серия', pal: 'orange', emb: 'flame', set: 'Достижения', shape: 'hex'},
    {key: 'bolt', name: 'Молния', pal: 'gold', emb: 'bolt', set: 'Достижения'},
    {key: 'rocket', name: 'Ракета Plinko', pal: 'blue', emb: 'rocket', set: 'Достижения', shape: 'hex'},
    {key: 'gambler', name: 'Азартный', pal: 'red', emb: 'dice', set: 'Достижения'},
    {key: 'miner', name: 'Сапёр', pal: 'green', emb: 'mine', set: 'Достижения', shape: 'hex'},
    {key: 'cards', name: 'Мастер карт', pal: 'obsidian', emb: 'spade', set: 'Достижения'},
    {key: 'night', name: 'Ночной игрок', pal: 'night', emb: 'moon', set: 'Достижения', shape: 'seal'}
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
      '.gdm .gm-hue{animation:gmHue 7s linear infinite}@keyframes gmHue{to{filter:hue-rotate(360deg)}}@keyframes gmGlow{0%,100%{opacity:.35;transform:scale(1)}50%{opacity:.75;transform:scale(1.08)}}' +
      '@keyframes gmBob{0%,100%{transform:translateY(0)}50%{transform:translateY(-1.8px)}}' +
      '@keyframes gmShine{0%,55%{transform:translateX(-70px)}100%{transform:translateX(70px)}}' +
      '@keyframes gmTw{0%,100%{opacity:0;transform:scale(.3)}50%{opacity:1;transform:scale(1)}}' +
      '@keyframes gmSway{0%,100%{transform:rotate(-1.2deg)}50%{transform:rotate(1.2deg)}}' +
      '@media(prefers-reduced-motion:reduce){.gdm *{animation:none!important}}html.hw-still .gdm *,html.perf-lite .gdm *{animation:none!important}';
    document.head.appendChild(s);
  }

  function shape(kind, r) {
    var cx = 48, cy = 62, d = '', i, n, a, rr;
    if (kind === 'hex') {
      for (i = 0; i < 6; i++) { a = (-90 + i * 60) * Math.PI / 180; d += (i ? 'L' : 'M') + (cx + r * 1.04 * Math.cos(a)).toFixed(1) + ' ' + (cy + r * 1.04 * Math.sin(a)).toFixed(1); }
      return d + 'Z';
    }
    if (kind === 'seal') {
      n = 32;
      for (i = 0; i < n; i++) { a = (-90 + i * 360 / n) * Math.PI / 180; rr = i % 2 ? r - 3 : r + .6; d += (i ? 'L' : 'M') + (cx + rr * Math.cos(a)).toFixed(1) + ' ' + (cy + rr * Math.sin(a)).toFixed(1); }
      return d + 'Z';
    }
    return 'M' + (cx - r) + ' ' + cy + 'a' + r + ' ' + r + ' 0 1 0 ' + (2 * r) + ' 0a' + r + ' ' + r + ' 0 1 0 ' + (-2 * r) + ' 0Z';
  }

  function svg(key, size, still) {
    css();
    var m = BY[key] || BY.c1, p = PAL[m.pal || 'gold'], id = 'gm' + (++N), w = size || 64, vh = m.place ? 120 : 110, h = Math.round(w * vh / 96), sh = m.shape || 'round', i, beads = '', a;
    for (i = 0; i < 28; i++) { a = i * Math.PI * 2 / 28; beads += '<circle cx="' + (48 + 35.4 * Math.cos(a)).toFixed(1) + '" cy="' + (62 + 35.4 * Math.sin(a)).toFixed(1) + '" r=".9" fill="' + p[0] + '" opacity=".75"/>'; }
    if (sh !== 'round') beads = '';
    var rar = m.rarity || 'common', rr = '', rs = '';
    if (rar === 'rare') rr = '<path d="' + shape(sh, 40.5) + '" fill="none" stroke="#5aa8ff" stroke-width="1.6" opacity=".7"><animate attributeName="opacity" values=".35;.85;.35" dur="3s" repeatCount="indefinite"/></path>';
    if (rar === 'epic') rr = '<path d="' + shape(sh, 40.8) + '" fill="none" stroke="#c18bff" stroke-width="2" opacity=".8"><animate attributeName="opacity" values=".4;1;.4" dur="2.4s" repeatCount="indefinite"/></path>';
    if (rar === 'legendary') {
      rr = '<path class="gm-hue" d="' + shape(sh, 41) + '" fill="none" stroke="url(#' + id + 'v)" stroke-width="3.2" opacity=".95"/><path d="' + shape(sh, 35.2) + '" fill="none" stroke="url(#' + id + 'v)" stroke-width="4.2" opacity=".78" style="mix-blend-mode:screen"/>';
      rs = '<g clip-path="url(#' + id + 'c)"><rect x="0" y="20" width="96" height="84" fill="url(#' + id + 'v)" opacity=".24" style="mix-blend-mode:screen"/></g>';
    }
    return '<svg class="gdm" width="' + w + '" height="' + h + '" viewBox="0 0 96 ' + vh + '" aria-hidden="true">' +
      '<defs><linearGradient id="' + id + 'r" x1="0" y1="0" x2="1" y2="1"><stop offset="0" stop-color="' + p[0] + '"/><stop offset=".45" stop-color="' + p[1] + '"/><stop offset="1" stop-color="' + p[2] + '"/></linearGradient>' +
      '<linearGradient id="' + id + 'q" x1="1" y1="1" x2="0" y2="0"><stop offset="0" stop-color="' + p[0] + '"/><stop offset=".5" stop-color="' + p[1] + '"/><stop offset="1" stop-color="' + p[2] + '"/></linearGradient>' +
      '<radialGradient id="' + id + 'd" cx=".38" cy=".28" r=".95"><stop offset="0" stop-color="' + p[3] + '"/><stop offset=".55" stop-color="' + p[1] + '"/><stop offset="1" stop-color="' + p[2] + '"/></radialGradient>' +
      '<linearGradient id="' + id + 's" x1="0" x2="1"><stop offset="0" stop-color="#fff" stop-opacity="0"/><stop offset=".5" stop-color="#fff" stop-opacity=".7"/><stop offset="1" stop-color="#fff" stop-opacity="0"/></linearGradient>' +
      '<linearGradient id="' + id + 'a" x1="0" y1="0" x2="0" y2="1"><stop offset="0" stop-color="' + p[4] + '"/><stop offset="1" stop-color="' + p[5] + '"/></linearGradient>' +
      '<linearGradient id="' + id + 'v" x1="0" y1="0" x2="1" y2="1" spreadMethod="repeat"><stop offset="0" stop-color="#ff6ad5"/><stop offset=".2" stop-color="#ffd36a"/><stop offset=".4" stop-color="#6aff9e"/><stop offset=".6" stop-color="#6ac8ff"/><stop offset=".8" stop-color="#b06aff"/><stop offset="1" stop-color="#ff6ad5"/><animateTransform attributeName="gradientTransform" type="translate" values="0 0;1 1" dur="3.4s" repeatCount="indefinite"/></linearGradient>' +
      '<clipPath id="' + id + 'c"><path d="' + shape(sh, 29.5) + '"/></clipPath></defs>' +
      '<circle class="gm-glow" cx="48" cy="62" r="42" fill="' + p[1] + '" opacity=".4"/>' +
      '<g class="gm-rb"><path d="M29 4 L49 46 L39 50 L19 11Z" fill="url(#' + id + 'a)"/><path d="M67 4 L47 46 L57 50 L77 11Z" fill="url(#' + id + 'a)"/><path d="M29 4 L36 4 L51 40 L47 46Z" fill="#fff" opacity=".16"/><path d="M67 4 L60 4 L45 40 L49 46Z" fill="#000" opacity=".14"/></g>' +
      '<path d="' + shape(sh, 38) + '" fill="rgba(0,0,0,.35)" transform="translate(0 2)"/>' +
      rr + '<path d="' + shape(sh, 38) + '" fill="url(#' + id + 'r)"/>' +
      '<path d="' + shape(sh, 38) + '" fill="none" stroke="' + p[0] + '" stroke-opacity=".7" stroke-width="1"/>' + beads +
      '<path d="' + shape(sh, 32.5) + '" fill="url(#' + id + 'q)" stroke="rgba(0,0,0,.35)" stroke-width="1"/>' +
      '<path d="' + shape(sh, 29.5) + '" fill="url(#' + id + 'd)"/>' +
      (m.img ? '' : '<path d="' + shape(sh, 29.5) + '" fill="' + p[6] + '"/>') +
      '<path d="' + shape(sh, 29.5) + '" fill="none" stroke="rgba(255,255,255,.45)" stroke-width="1"/>' +
      (m.img ? '<image href="' + m.img + '" x="18.5" y="31.5" width="59" height="59" preserveAspectRatio="xMidYMid slice" clip-path="url(#' + id + 'c)"/>' :
        '<g transform="translate(48 61) scale(.9)" style="filter:drop-shadow(0 2px 1.5px rgba(0,0,0,.4))"><g class="gm-emb">' + EMB[m.emb] + '</g></g>') +
      rs + '<g clip-path="url(#' + id + 'c)"><rect class="gm-shine" x="10" y="26" width="24" height="72" fill="url(#' + id + 's)" transform="skewX(-18)"/><path d="M20 50 A30 30 0 0 1 56 33" stroke="#fff" stroke-opacity=".38" stroke-width="3" fill="none" stroke-linecap="round"/></g>' +
      '<circle class="gm-sp" cx="17" cy="34" r="2.400" fill="#fff"/><circle class="gm-sp b" cx="80" cy="46" r="2" fill="#fff"/><circle class="gm-sp c" cx="74" cy="98" r="2.200" fill="#fff"/>' +
      (m.place ? '<g><rect x="35" y="95" width="26" height="16" rx="8" fill="rgba(20,10,40,.85)" stroke="' + p[3] + '" stroke-width="1.200"/><text x="48" y="107" text-anchor="middle" font-family="system-ui,sans-serif" font-size="11.500" font-weight="900" fill="' + p[0] + '">' + m.place + '</text></g>' : '') +
      '</svg>';
  }
  var BASE = {};
  LIST.forEach(function (m) { BASE[m.key] = m.name; });
  var api = {LIST: LIST, by: function (k) { return BY[k] || null; }, svg: svg, name: function (k) { return (BY[k] || {}).name || 'Медаль'; }, ready: null};
  api.load = function () {
    api.RAR = {common: 'Обычная', rare: 'Редкая', epic: 'Эпическая', legendary: 'Легендарная'};
    api.ready = fetch('/api/medals/catalog', {credentials: 'same-origin'}).then(function (r) { return r.json(); }).then(function (d) {
      for (var i = LIST.length - 1; i >= 0; i--) if (LIST[i].custom) LIST.splice(i, 1);
      LIST.forEach(function (m) { if (BASE[m.key] && d.names && d.names[m.key]) m.name = d.names[m.key]; m.hidden = (d.hidden || []).indexOf(m.key) >= 0; m.rarity = (d.rarity || {})[m.key] || 'common'; });
      (d.custom || []).forEach(function (c) { LIST.push({key: c.key, name: c.name, pal: c.pal || 'gold', img: c.img, set: 'Свои медали', shape: 'seal', custom: true, rarity: (d.rarity || {})[c.key] || 'common', manual_only: c.manual_only, hidden: (d.hidden || []).indexOf(c.key) >= 0}); });
      BY = {}; LIST.forEach(function (m) { BY[m.key] = m; });
      return d;
    }).catch(function () {});
    return api.ready;
  };
  window.GDMedals = api;
  api.load();
})();
