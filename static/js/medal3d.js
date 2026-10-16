/* GemDrop real 3D medal: renders /static/models/medal.glb with a tiny own WebGL1 renderer (no libraries).
   GDMedal3D.mount(host, {img, back:{name,collection,rarity,percent,status,mine}, w, h, onfail}) -> handle | null */
(function () {
  'use strict';
  var VS = 'attribute vec3 aP;attribute vec3 aN;attribute vec2 aUV;attribute vec3 aC;attribute vec3 aK;' +
    'uniform mat4 uMVP;uniform float uT;varying vec3 vN;varying vec3 vP;varying vec2 vUV;varying vec3 vC;varying vec3 vK;' +
    'void main(){vec3 p=aP;if(aK.z>2.5&&aK.z<3.5){float s=smoothstep(.052,.08,p.y);p.x+=sin(uT*1.5+p.y*90.0)*.0011*s;p.z+=cos(uT*1.2+p.y*70.0)*.0008*s;}' +
    'vN=aN;vP=aP;vUV=aUV;vC=aC;vK=aK;gl_Position=uMVP*vec4(p,1.0);}';
  var FS = 'precision highp float;varying vec3 vN;varying vec3 vP;varying vec2 vUV;varying vec3 vC;varying vec3 vK;' +
    'uniform mat3 uR;uniform sampler2D uFront;uniform sampler2D uBack;uniform float uT;uniform float uHas;' +
    'vec3 env(vec3 r,float rough){float h=r.y*.5+.5;vec3 c=mix(vec3(.04,.04,.07),vec3(.55,.6,.78),pow(h,1.3));' +
    'float sw=sin(uT*.55);vec2 a=r.xy-vec2(-.5+.5*sw,.6);float w=7.0-rough*5.0;c+=vec3(1.0,.97,.92)*2.6*exp(-dot(a,a)*w)*step(0.0,r.z+.4);' +
    'vec2 b=r.xy-vec2(.72-.2*sw,-.15);c+=vec3(.55,.72,1.0)*1.3*exp(-dot(b,b)*(6.0-rough*4.0))*step(0.0,r.z+.4);' +
    'c+=vec3(1.0,.78,.6)*.5*smoothstep(.9,.15,abs(r.y+.6))*smoothstep(-.1,.7,r.z);return mix(c,vec3(.34,.37,.5),rough*.6);}' +
    'void main(){vec3 No=normalize(vN);float kind=vK.z;float met=vK.x,rough=vK.y;' +
    'vec3 N=normalize(uR*No);if(!gl_FrontFacing)N=-N;vec3 V=vec3(0.0,0.0,1.0);vec3 R=reflect(-V,N);' +
    'float nv=clamp(dot(N,V),0.0,1.0);float fr=pow(1.0-nv,4.0);vec3 L=normalize(vec3(-.45,.7,.75));float dif=clamp(dot(N,L),0.0,1.0);' +
    'vec3 H=normalize(L+V);float sp=pow(clamp(dot(N,H),0.0,1.0),mix(160.0,16.0,rough));vec3 col;' +
    'if(kind<.5||kind>4.5){vec3 base=vC;vec3 f0=mix(vec3(.05),base,met);vec3 e=env(R,rough);' +
    'col=base*(1.0-met)*(.28+.8*dif)+f0*e*(1.0+fr*1.2)+vec3(1.0)*sp*(.35+met*.6);' +
    'if(kind>4.5){float ang=atan(vP.y,vP.x);col+=vec3(1.0,.85,.8)*.18*(.5+.5*sin(ang*3.0-uT*2.2));}}' +
    'else if(kind<1.5){vec3 t=uHas>.5?texture2D(uFront,vUV).rgb:vec3(.2);col=t*(.62+.42*dif)+env(R,.25)*.1*(.3+fr*2.0)+vec3(1.0)*sp*.22;}' +
    'else if(kind<2.5){vec3 t=texture2D(uBack,vUV).rgb;col=t*(.7+.35*dif)+env(R,.3)*.07*(.3+fr*2.0)+vec3(1.0)*sp*.14;}' +
    'else{float u=vUV.x,l=vUV.y;float stripe=smoothstep(.27,.3,u)*smoothstep(.73,.7,u);float wv=.5+.5*sin(l*340.0)*sin(u*120.0);' +
    'vec3 base=mix(vec3(.02,.024,.036),vec3(.075,.082,.1),stripe);base*=.85+.3*wv;' +
    'float st=step(.5,fract(l*70.0))*(smoothstep(.055,.07,u)*smoothstep(.1,.085,u)+smoothstep(.9,.915,u)*smoothstep(.945,.93,u));base=mix(base,vec3(.48,.5,.54),st*.8);' +
    'float sat=pow(clamp(dot(N,H),0.0,1.0),10.0)*.55+pow(1.0-nv,2.0)*.18;col=base*(.45+.75*dif)+vec3(.6,.65,.8)*sat*(.5+.5*stripe)+env(R,.7)*.05;}' +
    'gl_FragColor=vec4(col,1.0);}';

  function mul(a, b) { var o = new Float32Array(16), i, j, k; for (i = 0; i < 4; i++) for (j = 0; j < 4; j++) { var s = 0; for (k = 0; k < 4; k++) s += a[k * 4 + j] * b[i * 4 + k]; o[i * 4 + j] = s; } return o; }
  function persp(f, a, n, fa) { var t = 1 / Math.tan(f / 2), o = new Float32Array(16); o[0] = t / a; o[5] = t; o[10] = (fa + n) / (n - fa); o[11] = -1; o[14] = 2 * fa * n / (n - fa); return o; }
  function rotX(a) { var c = Math.cos(a), s = Math.sin(a); return new Float32Array([1, 0, 0, 0, 0, c, s, 0, 0, -s, c, 0, 0, 0, 0, 1]); }
  function rotY(a) { var c = Math.cos(a), s = Math.sin(a); return new Float32Array([c, 0, -s, 0, 0, 1, 0, 0, s, 0, c, 0, 0, 0, 0, 1]); }
  function trans(x, y, z) { return new Float32Array([1, 0, 0, 0, 0, 1, 0, 0, 0, 0, 1, 0, x, y, z, 1]); }
  function scl(s) { return new Float32Array([s, 0, 0, 0, 0, s, 0, 0, 0, 0, s, 0, 0, 0, 0, 1]); }
  function m3(m) { return new Float32Array([m[0], m[1], m[2], m[4], m[5], m[6], m[8], m[9], m[10]]); }

  /* ---------- GLB -> one merged vertex buffer ---------- */
  var SKIP = {'Ribbon left': 1, 'Ribbon right': 1, 'Woven central stripe': 1, 'Ribbon seam': 1, 'Front reference artwork': 1};
  function parseGlb(buf) {
    var dv = new DataView(buf), off = 12, json = null, bin = null;
    while (off < buf.byteLength) { var l = dv.getUint32(off, true), t = dv.getUint32(off + 4, true); if (t === 0x4E4F534A) json = JSON.parse(new TextDecoder().decode(new Uint8Array(buf, off + 8, l))); else if (t === 0x004E4942) bin = buf.slice(off + 8, off + 8 + l); off += 8 + l; }
    return {j: json, bin: bin};
  }
  function acc(g, i, ctor, n) { var a = g.j.accessors[i], bv = g.j.bufferViews[a.bufferView]; return new ctor(g.bin, (bv.byteOffset || 0) + (a.byteOffset || 0), a.count * n); }
  function Geo() { this.v = []; this.i = []; }
  Geo.prototype.add = function (p, n, uv, c, k) { this.v.push(p[0], p[1], p[2], n[0], n[1], n[2], uv[0], uv[1], c[0], c[1], c[2], k[0], k[1], k[2]); return this.v.length / 14 - 1; };
  function addGlb(geo, g) {
    var mats = g.j.materials;
    g.j.nodes.forEach(function (nd) {
      if (SKIP[nd.name]) return;
      var prim = g.j.meshes[nd.mesh].primitives[0], P = acc(g, prim.attributes.POSITION, Float32Array, 3), N = acc(g, prim.attributes.NORMAL, Float32Array, 3), I = acc(g, prim.indices, Uint32Array, 1);
      var mt = mats[prim.material].pbrMetallicRoughness, col = mt.baseColorFactor || [1, 1, 1], kind = 0;
      var isArt = prim.material === 9;
      if (nd.name.indexOf('Rim dash') === 0) kind = 5;
      var base = geo.v.length / 14, k, rr = 0;
      if (isArt) return;
      for (k = 0; k < P.length / 3; k++) geo.add([P[k * 3], P[k * 3 + 1], P[k * 3 + 2]], [N[k * 3], N[k * 3 + 1], N[k * 3 + 2]], [0, 0], col, [mt.metallicFactor || 0, mt.roughnessFactor == null ? .5 : mt.roughnessFactor, kind]);
      for (k = 0; k < I.length; k++) geo.i.push(base + I[k]);
    });
  }
  function disc(geo, r, z, nz, mode, kind, seg) { // textured disc facing +z (nz=1) or -z (nz=-1)
    var c = geo.add([0, 0, z], [0, 0, nz], [.5, .5], [1, 1, 1], [0, .5, kind]), i, base = c + 1;
    for (i = 0; i <= seg; i++) { var a = i / seg * Math.PI * 2, x = Math.cos(a) * r, y = Math.sin(a) * r; geo.add([x, y, z], [0, 0, nz], [.5 + (mode === 2 ? -1 : 1) * x / (2 * r), .5 - y / (2 * r)], [1, 1, 1], [0, .5, kind]); }
    for (i = 0; i < seg; i++) { if (nz > 0) geo.i.push(c, base + i, base + i + 1); else geo.i.push(c, base + i + 1, base + i); }
  }
  function torus(geo, cx, cy, cz, R, r, col, met, rough) {
    var S = 36, Q = 12, i, j, base = geo.v.length / 14;
    for (i = 0; i <= S; i++) { var a = i / S * Math.PI * 2, ca = Math.cos(a), sa = Math.sin(a); for (j = 0; j <= Q; j++) { var b = j / Q * Math.PI * 2, cb = Math.cos(b), sb = Math.sin(b); geo.add([cx + (R + r * cb) * ca, cy + (R + r * cb) * sa, cz + r * sb], [cb * ca, cb * sa, sb], [0, 0], col, [met, rough, 0]); } }
    for (i = 0; i < S; i++) for (j = 0; j < Q; j++) { var a0 = base + i * (Q + 1) + j, b0 = base + (i + 1) * (Q + 1) + j; geo.i.push(a0, b0, a0 + 1, b0, b0 + 1, a0 + 1); }
  }
  function strap(geo, a, b, w) { // woven satin strap from a to b (x,y), bulged across the width, with a hem
    var S = 16, Wd = 12, i, j, base = geo.v.length / 14, dx = b[0] - a[0], dy = b[1] - a[1], l = Math.sqrt(dx * dx + dy * dy); dx /= l; dy /= l; var nx = -dy, ny = dx;
    for (i = 0; i <= S; i++) {
      var t = i / S, px = a[0] + (b[0] - a[0]) * t, py = a[1] + (b[1] - a[1]) * t, wave = Math.sin(t * 5) * .0012;
      for (j = 0; j <= Wd; j++) {
        var u = j / Wd, off = (u - .5) * w, cr = Math.cos((u - .5) * Math.PI), z = -.0019 + cr * .0005 + wave * .3, sl = Math.sin((u - .5) * .8);
        geo.add([px + nx * off, py + ny * off, z], [nx * sl, ny * sl, Math.cos((u - .5) * .8)], [u, t * (l / .02)], [1, 1, 1], [0, .8, 3]);
      }
    }
    for (i = 0; i < S; i++) for (j = 0; j < Wd; j++) { var a0 = base + i * (Wd + 1) + j, b0 = base + (i + 1) * (Wd + 1) + j; geo.i.push(a0, b0, a0 + 1, b0, b0 + 1, a0 + 1); }
  }

  /* ---------- textures ---------- */
  function wrapText(x, text, maxw) { var words = String(text).split(/\s+/), lines = [], cur = ''; words.forEach(function (w) { var t = cur ? cur + ' ' + w : w; if (x.measureText(t).width > maxw && cur) { lines.push(cur); cur = w; } else cur = t; }); if (cur) lines.push(cur); return lines; }
  function backCanvas(b) {
    b = b || {}; var S = 1024, c = document.createElement('canvas'); c.width = c.height = S; var x = c.getContext('2d'), cx = S / 2, i;
    var g = x.createRadialGradient(cx - 120, cx - 160, 40, cx, cx, S * .62); g.addColorStop(0, '#8f1230'); g.addColorStop(.55, '#5a0a1e'); g.addColorStop(1, '#2a0510'); x.fillStyle = g; x.fillRect(0, 0, S, S);
    x.save(); x.translate(cx, cx); for (i = 0; i < 90; i++) { x.rotate(Math.PI * 2 / 90); x.fillStyle = 'rgba(255,255,255,.035)'; x.fillRect(-1, -S / 2, 2, S / 2 - 120); } x.restore();
    x.strokeStyle = 'rgba(255,215,220,.75)'; x.lineWidth = 6; x.beginPath(); x.arc(cx, cx, 470, 0, 7); x.stroke();
    x.strokeStyle = 'rgba(255,215,220,.35)'; x.lineWidth = 3; x.setLineDash([3, 14]); x.beginPath(); x.arc(cx, cx, 440, 0, 7); x.stroke(); x.setLineDash([]);
    x.save(); x.translate(cx, cx); x.fillStyle = 'rgba(255,225,228,.7)'; x.font = '800 38px system-ui,sans-serif'; var ring = ('GEMDROP · ' + String(b.collection || 'MEDAL').toUpperCase() + ' · ' + String(b.rarity || '').toUpperCase() + ' · '); while (ring.length < 60) ring += ring;
    for (i = 0; i < 56; i++) { var ch = ring[i % ring.length]; x.save(); x.rotate((i / 56) * Math.PI * 2 - Math.PI / 2); x.translate(0, -402); x.textAlign = 'center'; x.fillText(ch, 0, 0); x.restore(); } x.restore();
    x.fillStyle = '#ffe9ec'; x.textAlign = 'center'; x.shadowColor = 'rgba(0,0,0,.5)'; x.shadowBlur = 10; x.shadowOffsetY = 4;
    var fs = 96, name = b.name || 'Медаль', lines; do { x.font = '900 ' + fs + 'px Georgia,serif'; lines = wrapText(x, name, 600); fs -= 6; } while ((lines.length > 3 || lines.length * fs * 1.15 > 330) && fs > 40);
    var y0 = 300 - (lines.length - 1) * fs * .55; lines.forEach(function (l, k) { x.fillText(l, cx, y0 + k * fs * 1.15); });
    x.shadowColor = 'transparent'; x.strokeStyle = 'rgba(255,225,228,.6)'; x.lineWidth = 3; x.beginPath(); x.moveTo(cx - 90, 500); x.lineTo(cx + 90, 500); x.stroke();
    var rows = [['Коллекция', b.collection], ['Редкость', b.rarity], ['Есть у игроков', b.percent != null ? (b.percent >= 10 ? Math.round(b.percent) : String(b.percent).replace('.', ',')) + '%' : null], ['Статус', b.status]].filter(function (r) { return r[1]; });
    rows.forEach(function (r, k) { var y = 570 + k * 70; x.textAlign = 'left'; x.fillStyle = 'rgba(255,225,228,.62)'; x.font = '600 34px system-ui,sans-serif'; x.fillText(r[0], 250, y); x.textAlign = 'right'; x.fillStyle = '#fff'; x.font = '800 38px system-ui,sans-serif'; x.fillText(String(r[1]), S - 250, y); });
    if (!rows.length) { x.textAlign = 'center'; x.fillStyle = 'rgba(255,225,228,.7)'; x.font = '700 36px system-ui,sans-serif'; x.fillText('Загрузка информации…', cx, 620); }
    if (b.mine) { x.textAlign = 'center'; x.fillStyle = '#6dffa4'; x.font = '800 36px system-ui,sans-serif'; x.fillText('✓ Есть у вас', cx, 570 + rows.length * 70 + 20); }
    x.textAlign = 'center'; x.fillStyle = 'rgba(255,225,228,.5)'; x.font = '800 30px system-ui,sans-serif'; x.fillText('G E M D R O P', cx, 900);
    return c;
  }

  function shader(gl, type, src) { var s = gl.createShader(type); gl.shaderSource(s, src); gl.compileShader(s); if (!gl.getShaderParameter(s, gl.COMPILE_STATUS)) { if (window.console) console.warn(gl.getShaderInfoLog(s)); return null; } return s; }
  function mkTex(gl, unit, src, size) {
    var t = gl.createTexture(); gl.activeTexture(gl.TEXTURE0 + unit); gl.bindTexture(gl.TEXTURE_2D, t);
    if (src) { up(gl, t, unit, src); } else gl.texImage2D(gl.TEXTURE_2D, 0, gl.RGBA, 1, 1, 0, gl.RGBA, gl.UNSIGNED_BYTE, new Uint8Array([40, 20, 24, 255]));
    return t;
  }
  function up(gl, t, unit, src) {
    gl.activeTexture(gl.TEXTURE0 + unit); gl.bindTexture(gl.TEXTURE_2D, t); gl.texImage2D(gl.TEXTURE_2D, 0, gl.RGBA, gl.RGBA, gl.UNSIGNED_BYTE, src); gl.generateMipmap(gl.TEXTURE_2D);
    gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_MIN_FILTER, gl.LINEAR_MIPMAP_LINEAR); gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_MAG_FILTER, gl.LINEAR); gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_WRAP_S, gl.CLAMP_TO_EDGE); gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_WRAP_T, gl.CLAMP_TO_EDGE);
    var e = gl.getExtension('EXT_texture_filter_anisotropic'); if (e) gl.texParameterf(gl.TEXTURE_2D, e.TEXTURE_MAX_ANISOTROPY_EXT, Math.min(8, gl.getParameter(e.MAX_TEXTURE_MAX_ANISOTROPY_EXT)));
  }
  var modelP = null;
  function loadModel() { return modelP || (modelP = fetch('/static/models/medal.glb', {credentials: 'same-origin'}).then(function (r) { if (!r.ok) throw new Error('glb'); return r.arrayBuffer(); }).then(parseGlb)); }

  function mount(host, o) {
    o = o || {};
    var W = o.w || 270, H = o.h || 310, dpr = Math.min(2, window.devicePixelRatio || 1);
    var cv = document.createElement('canvas'); cv.width = Math.round(W * dpr); cv.height = Math.round(H * dpr); cv.style.cssText = 'width:' + W + 'px;height:' + H + 'px;display:block;touch-action:none;cursor:grab';
    var gl = cv.getContext('webgl', {antialias: true, alpha: true, premultipliedAlpha: false}) || cv.getContext('experimental-webgl');
    if (!gl) return null;
    var vs = shader(gl, gl.VERTEX_SHADER, VS), fs = shader(gl, gl.FRAGMENT_SHADER, FS); if (!vs || !fs) return null;
    var pr = gl.createProgram(); gl.attachShader(pr, vs); gl.attachShader(pr, fs); gl.linkProgram(pr); if (!gl.getProgramParameter(pr, gl.LINK_STATUS)) return null; gl.useProgram(pr);
    host.innerHTML = ''; host.style.width = W + 'px'; host.style.height = H + 'px'; host.appendChild(cv);
    var U = function (n) { return gl.getUniformLocation(pr, n); };
    gl.uniform1i(U('uFront'), 0); gl.uniform1i(U('uBack'), 1); gl.uniform1f(U('uHas'), 0);
    var tf = mkTex(gl, 0, null), tb = mkTex(gl, 1, backCanvas(o.back));
    var count = 0, alive = true, handle = {
      setBack: function (b) { try { up(gl, tb, 1, backCanvas(b)); } catch (e) {} },
      destroy: function () { alive = false; },
      rot: function (a, b) { ry = a; rx = b; auto = false; }
    };
    if (o.img) { var im = new Image(); im.onload = function () { var c = document.createElement('canvas'); c.width = c.height = 1024; var x = c.getContext('2d'); x.imageSmoothingQuality = 'high'; x.drawImage(im, 0, 0, 1024, 1024); up(gl, tf, 0, c); gl.useProgram(pr); gl.uniform1f(U('uHas'), 1); }; im.src = o.img; }
    gl.enable(gl.DEPTH_TEST); gl.clearColor(0, 0, 0, 0);
    loadModel().then(function (g) {
      var geo = new Geo(); addGlb(geo, g);
      // portrait disc (our own artwork, uv from position so the whole picture fills the circle)
      var pr0 = .0373 * 1, seg = 96, c0 = geo.add([0, 0, .0043], [0, 0, 1], [.5, .5], [1, 1, 1], [0, .5, 1]), i;
      for (i = 0; i <= seg; i++) { var a = i / seg * Math.PI * 2, x = Math.cos(a) * pr0, y = Math.sin(a) * pr0; geo.add([x, y, .0043], [0, 0, 1], [.5 + x / (2 * pr0), .5 - y / (2 * pr0)], [1, 1, 1], [0, .5, 1]); }
      for (i = 0; i < seg; i++) geo.i.push(c0, c0 + 1 + i, c0 + 2 + i);
      disc(geo, .0455, -.0042, -1, 2, 2, 96);
      torus(geo, 0, 0, -.0043, .0462, .0007, [1, .7, .71], .65, .22);
      torus(geo, 0, .0538, -.0006, .0066, .0024, [.92, .62, .66], .85, .2);
      strap(geo, [-.0347, .0784], [-.0035, .0568], .0205); strap(geo, [.0347, .0784], [.0035, .0568], .0205);
      var vb = gl.createBuffer(), ib = gl.createBuffer();
      gl.useProgram(pr); gl.bindBuffer(gl.ARRAY_BUFFER, vb); gl.bufferData(gl.ARRAY_BUFFER, new Float32Array(geo.v), gl.STATIC_DRAW);
      gl.bindBuffer(gl.ELEMENT_ARRAY_BUFFER, ib); gl.bufferData(gl.ELEMENT_ARRAY_BUFFER, new Uint16Array(geo.i), gl.STATIC_DRAW);
      [['aP', 3, 0], ['aN', 3, 3], ['aUV', 2, 6], ['aC', 3, 8], ['aK', 3, 11]].forEach(function (a) { var l = gl.getAttribLocation(pr, a[0]); gl.enableVertexAttribArray(l); gl.vertexAttribPointer(l, a[1], gl.FLOAT, false, 56, a[2] * 4); });
      count = geo.i.length; start();
    }).catch(function () { alive = false; if (o.onfail) o.onfail(); });

    var P = persp(30 * Math.PI / 180, W / H, .5, 30), ry = -.55, rx = .1, vy = 0, vx = 0, down = false, lx = 0, ly = 0, t0 = performance.now();
    var auto = !(window.matchMedia && matchMedia('(prefers-reduced-motion:reduce)').matches);
    function start() {
      function frame(now) {
        if (!alive || !host.isConnected) return;
        var T = (now - t0) / 1000;
        if (!down) { if (Math.abs(vy) > .0003 || Math.abs(vx) > .0003) { ry += vy; rx = Math.max(-.9, Math.min(.9, rx + vx)); vy *= .94; vx *= .9; } else if (auto) { ry += .006; } if (Math.abs(vx) < .0003) rx += (.1 + (auto ? .035 * Math.sin(T * .9) : 0) - rx) * .04; }
        var M = mul(rotX(rx), rotY(ry)), mvp = mul(P, mul(trans(0, -.2, -5.9), mul(M, scl(20))));
        gl.viewport(0, 0, cv.width, cv.height); gl.clear(gl.COLOR_BUFFER_BIT | gl.DEPTH_BUFFER_BIT);
        gl.uniformMatrix4fv(U('uMVP'), false, mvp); gl.uniformMatrix3fv(U('uR'), false, m3(M)); gl.uniform1f(U('uT'), T);
        gl.drawElements(gl.TRIANGLES, count, gl.UNSIGNED_SHORT, 0);
        requestAnimationFrame(frame);
      }
      requestAnimationFrame(frame);
    }
    cv.addEventListener('pointerdown', function (e) { down = true; auto = false; lx = e.clientX; ly = e.clientY; vy = vx = 0; cv.style.cursor = 'grabbing'; try { cv.setPointerCapture(e.pointerId); } catch (x) {} });
    cv.addEventListener('pointermove', function (e) { if (!down) return; vy = (e.clientX - lx) * .0095; vx = (e.clientY - ly) * .006; ry += vy; rx = Math.max(-.9, Math.min(.9, rx + vx)); lx = e.clientX; ly = e.clientY; });
    function upf() { down = false; cv.style.cursor = 'grab'; } cv.addEventListener('pointerup', upf); cv.addEventListener('pointercancel', upf);
    cv.addEventListener('dblclick', function () { vy = .24; });
    return handle;
  }
  window.GDMedal3D = {mount: mount};
})();
