/* GemDrop real 3D medal: renders /static/models/medal.glb with a tiny own WebGL1 renderer (no libraries).
   GDMedal3D.mount(host, {img, back:{name,collection,rarity,percent,status,mine}, w, h, onfail}) -> handle | null */
(function () {
  'use strict';
  var VS = 'attribute vec3 aP;attribute vec3 aN;attribute vec2 aUV;attribute vec3 aC;attribute vec3 aK;' +
    'uniform mat4 uMVP;uniform float uT;varying vec3 vN;varying vec3 vP;varying vec2 vUV;varying vec3 vC;varying vec3 vK;' +
    'uniform float uTw;void main(){vec3 p=aP;vec3 nn=aN;if(aK.z>2.5&&aK.z<3.5){float w=smoothstep(.0784,.0585,p.y);float a=uTw*w;float c=cos(a),s=sin(a);p.xz=vec2(c*p.x+s*p.z,-s*p.x+c*p.z);nn.xz=vec2(c*nn.x+s*nn.z,-s*nn.x+c*nn.z);}' +
    'vN=nn;vP=aP;vUV=aUV;vC=aC;vK=aK;gl_Position=uMVP*vec4(p,1.0);}';
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
    'float al=(kind>2.5&&kind<3.5)?smoothstep(.12,.093,vP.y):1.0;gl_FragColor=vec4(col,al);}';

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
  function hx(h) { return [parseInt(h.substr(1, 2), 16) / 255, parseInt(h.substr(3, 2), 16) / 255, parseInt(h.substr(5, 2), 16) / 255]; }
  function mixc(a, b, t) { return [a[0] + (b[0] - a[0]) * t, a[1] + (b[1] - a[1]) * t, a[2] + (b[2] - a[2]) * t]; }
  function addGlb(geo, g, o) {
    o = o || {}; var pl = o.pal, T = null;
    if (pl) { var p = pl.map(hx); T = {body: mixc(p[2], p[1], .3), bevel: p[1], hi: p[0], outer: mixc(p[1], p[2], .25), acc: p[3]}; }
    var mats = g.j.materials;
    g.j.nodes.forEach(function (nd) {
      if (SKIP[nd.name]) return;
      var prim = g.j.meshes[nd.mesh].primitives[0], P = acc(g, prim.attributes.POSITION, Float32Array, 3), N = acc(g, prim.attributes.NORMAL, Float32Array, 3), I = acc(g, prim.indices, Uint32Array, 1);
      var mt = mats[prim.material].pbrMetallicRoughness, col = mt.baseColorFactor || [1, 1, 1], kind = 0;
      var isArt = prim.material === 9;
      if (nd.name.indexOf('Rim dash') === 0) kind = 5;
      var base = geo.v.length / 14, k, rr = 0, mf = mt.metallicFactor || 0, rf = mt.roughnessFactor == null ? .5 : mt.roughnessFactor;
      if (T) {
        var nm = nd.name;
        if (nm === 'Solid medal body' || nm === 'Portrait bezel') { col = T.body; mf = .8; rf = .22; }
        else if (nm === 'Polished outer bevel') { col = T.bevel; mf = .85; rf = .2; }
        else if (nm === 'Outer bright highlight') { col = T.hi; mf = .9; rf = .16; }
        else if (nm === 'Red outer bezel') { col = T.outer; mf = .8; rf = .2; }
        else if (nm === 'Portrait bright inner rim' || nm === 'White circular accent' || nm.indexOf('Rim dash') === 0) { col = T.acc; mf = .5; rf = .25; }
      }
      if (!o.badge && /^(Badge|Play)/.test(nd.name)) return;
      if (nd.name === 'Badge silver surround') { col = [.96, .97, 1]; mf = .95; rf = .1; }
      if (nd.name === 'Badge red enamel') { col = [.95, .015, .02]; mf = .2; rf = .2; }
      if (nd.name === 'Play triangle') { col = [1, 1, 1]; mf = 0; rf = .3; }
      if (isArt) return;
      for (k = 0; k < P.length / 3; k++) geo.add([P[k * 3], P[k * 3 + 1], P[k * 3 + 2]], [N[k * 3], N[k * 3 + 1], N[k * 3 + 2]], [0, 0], col, [mf, rf, kind]);
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
  function crom(P, t) { // Catmull-Rom through points P at t in [0, n-1]
    var n = P.length, i = Math.min(n - 2, Math.floor(t)), u = t - i, p0 = P[Math.max(0, i - 1)], p1 = P[i], p2 = P[i + 1], p3 = P[Math.min(n - 1, i + 2)], o = [], k;
    for (k = 0; k < 3; k++) o.push(.5 * ((2 * p1[k]) + (-p0[k] + p2[k]) * u + (2 * p0[k] - 5 * p1[k] + 4 * p2[k] - p3[k]) * u * u + (-p0[k] + 3 * p1[k] - 3 * p2[k] + p3[k]) * u * u * u));
    return o;
  }
  function nrm(v) { var l = Math.sqrt(v[0] * v[0] + v[1] * v[1] + v[2] * v[2]) || 1; return [v[0] / l, v[1] / l, v[2] / l]; }
  function crs(a, b) { return [a[1] * b[2] - a[2] * b[1], a[2] * b[0] - a[0] * b[2], a[0] * b[1] - a[1] * b[0]]; }
  // one satin band: comes down behind the ring, passes THROUGH its hole and goes back up in front (gathered narrow at the hole)
  function ribbonLoop(geo, hy) {
    var P = [[-.075, .122, -.0050], [-.0347, .0784, -.0046], [-.0170, .0690, -.0044], [-.0058, .0625, -.0041], [-.0019, hy + .0017, -.0033], [0, hy, -.0011], [0, hy, .0011], [.0019, hy + .0017, .0033], [.0058, .0625, .0041], [.0170, .0690, .0044], [.0347, .0784, .0046], [.075, .122, .0050]];
    var N = 150, Wd = 12, i, j, base = geo.v.length / 14, pts = [], T = [], Wv = [], prev = null, w0 = null;
    for (i = 0; i <= N; i++) pts.push(crom(P, i / N * (P.length - 1)));
    for (i = 0; i <= N; i++) { var q = pts[Math.min(N, i + 1)], o = pts[Math.max(0, i - 1)]; T.push(nrm([q[0] - o[0], q[1] - o[1], q[2] - o[2]])); }
    var w = nrm(crs(T[0], [0, 0, 1]));
    for (i = 0; i <= N; i++) { var d = w[0] * T[i][0] + w[1] * T[i][1] + w[2] * T[i][2]; w = nrm([w[0] - T[i][0] * d, w[1] - T[i][1] * d, w[2] - T[i][2] * d]); Wv.push(w); }
    var len = 0;
    for (i = 0; i <= N; i++) {
      if (i) { var dx = pts[i][0] - pts[i - 1][0], dy = pts[i][1] - pts[i - 1][1], dz = pts[i][2] - pts[i - 1][2]; len += Math.sqrt(dx * dx + dy * dy + dz * dz); }
      var mid = Math.abs(i / N - .5) * 2, wd = .0078 + (.0205 - .0078) * Math.pow(Math.min(1, mid / .62), 1.4), nn = crs(T[i], Wv[i]);
      for (j = 0; j <= Wd; j++) {
        var u = j / Wd, off = (u - .5) * wd, cr = Math.cos((u - .5) * Math.PI), sl = Math.sin((u - .5) * 1.1);
        var px = pts[i][0] + Wv[i][0] * off + nn[0] * cr * .0006, py = pts[i][1] + Wv[i][1] * off + nn[1] * cr * .0006, pz = pts[i][2] + Wv[i][2] * off + nn[2] * cr * .0006;
        var nx = nn[0] * Math.cos((u - .5) * 1.1) + Wv[i][0] * sl, ny = nn[1] * Math.cos((u - .5) * 1.1) + Wv[i][1] * sl, nz = nn[2] * Math.cos((u - .5) * 1.1) + Wv[i][2] * sl;
        geo.add([px, py, pz], [nx, ny, nz], [u, len / .02], [1, 1, 1], [0, .8, 3]);
      }
    }
    for (i = 0; i < N; i++) for (j = 0; j < Wd; j++) { var a0 = base + i * (Wd + 1) + j, b0 = base + (i + 1) * (Wd + 1) + j; geo.i.push(a0, b0, a0 + 1, b0, b0 + 1, a0 + 1); }
  }

  /* ---------- decorations (per-medal 3D props) ---------- */
  function xf(p, o) { // ellipsoid-local -> world: pitch (x), roll (z), translate
    var cx = Math.cos(o.pit || 0), sx = Math.sin(o.pit || 0), cz = Math.cos(o.rol || 0), sz2 = Math.sin(o.rol || 0);
    var y1 = p[1] * cx - p[2] * sx, z1 = p[1] * sx + p[2] * cx, x2 = p[0] * cz - y1 * sz2, y2 = p[0] * sz2 + y1 * cz;
    return [x2 + o.c[0], y2 + o.c[1], z1 + o.c[2]];
  }
  function ell(geo, o, col, mf, rf) { // ellipsoid; o = {c, r:[a,b,c], pit, rol}
    var A = 14, B = 20, i, j, base = geo.v.length / 14, r = o.r;
    for (i = 0; i <= A; i++) for (j = 0; j <= B; j++) {
      var th = i / A * Math.PI, ph = j / B * Math.PI * 2, ux = Math.sin(th) * Math.cos(ph), uy = Math.sin(th) * Math.sin(ph), uz = Math.cos(th);
      var n = nrm([ux / r[0], uy / r[1], uz / r[2]]), nn = xf(n, {pit: o.pit, rol: o.rol, c: [0, 0, 0]});
      geo.add(xf([ux * r[0], uy * r[1], uz * r[2]], o), nn, [0, 0], col, [mf, rf, 0]);
    }
    for (i = 0; i < A; i++) for (j = 0; j < B; j++) { var a = base + i * (B + 1) + j, b = a + B + 1; geo.i.push(a, b, a + 1, b, b + 1, a + 1); }
  }
  function tube(geo, P, rad, col, mf, rf, col2) { // swept tube along Catmull-Rom path; rad(t) radius, col2 optional tip colour
    var N = 40, S = 10, i, j, base = geo.v.length / 14, pts = [], T = [], up = [0, 0, 1];
    for (i = 0; i <= N; i++) pts.push(crom(P, i / N * (P.length - 1)));
    for (i = 0; i <= N; i++) { var q = pts[Math.min(N, i + 1)], o = pts[Math.max(0, i - 1)]; T.push(nrm([q[0] - o[0], q[1] - o[1], q[2] - o[2]])); }
    for (i = 0; i <= N; i++) {
      var t = i / N, side = nrm(crs(T[i], up)), up2 = crs(side, T[i]), rr = rad(t), cc = col2 && (t < .08 || t > .93) ? col2 : col;
      for (j = 0; j <= S; j++) { var a = j / S * Math.PI * 2, nx = Math.cos(a), ny = Math.sin(a), n = [side[0] * nx + up2[0] * ny, side[1] * nx + up2[1] * ny, side[2] * nx + up2[2] * ny];
        geo.add([pts[i][0] + n[0] * rr, pts[i][1] + n[1] * rr, pts[i][2] + n[2] * rr], n, [0, 0], cc, [mf, rf, 0]); }
    }
    for (i = 0; i < N; i++) for (j = 0; j < S; j++) { var a0 = base + i * (S + 1) + j, b0 = a0 + S + 1; geo.i.push(a0, b0, a0 + 1, b0, b0 + 1, a0 + 1); }
  }
  function cone(geo, c, d, r, h, col, mf, rf) { // spike pointing along d
    var S = 12, base = geo.v.length / 14, w = nrm(crs(d, Math.abs(d[2]) > .9 ? [1, 0, 0] : [0, 0, 1])), v = crs(d, w), j, k = r / h;
    for (j = 0; j <= S; j++) { var a = j / S * Math.PI * 2, ca = Math.cos(a), sa = Math.sin(a), rad = [w[0] * ca + v[0] * sa, w[1] * ca + v[1] * sa, w[2] * ca + v[2] * sa], n = nrm([rad[0] + d[0] * k, rad[1] + d[1] * k, rad[2] + d[2] * k]);
      geo.add([c[0] + rad[0] * r, c[1] + rad[1] * r, c[2] + rad[2] * r], n, [0, 0], col, [mf, rf, 0]);
      geo.add([c[0] + d[0] * h, c[1] + d[1] * h, c[2] + d[2] * h], n, [0, 0], col, [mf, rf, 0]); }
    for (j = 0; j < S; j++) { var a1 = base + j * 2; geo.i.push(a1, a1 + 1, a1 + 2, a1 + 2, a1 + 1, a1 + 3); }
  }
  function decorate(geo, kind, pal) {
    var i, c1 = pal ? hx(pal[3]) : [.9, .9, .95], c2 = pal ? hx(pal[1]) : [.8, .8, .8];
    if (kind === 'spikes') {
      var n = 20; for (i = 0; i < n; i++) { var a = i / n * Math.PI * 2 + .08, lg = i % 2 ? .8 : 1.15; cone(geo, [Math.cos(a) * .0485, Math.sin(a) * .0485, -.0006], nrm([Math.cos(a), Math.sin(a), .12]), .0042, .0125 * lg, [.7, .73, .82], .95, .18); }
    }
    if (kind === 'cap') { // Kashtan: flat cap on the upper-left + gold chain along the bottom rim
      var K = [.50, .53, .40], gold = [1, .78, .2];
      ell(geo, {c: [-.0262, .0372, .0120], r: [.0215, .0135, .0078], rol: .62, pit: -.1}, K, 0, .92);
      ell(geo, {c: [-.0268, .0262, .0150], r: [.0150, .0042, .0068], rol: .62, pit: -.2}, [.30, .32, .24], 0, .8);
      ell(geo, {c: [-.0210, .0425, .0200], r: [.0030, .0030, .0026], rol: 0, pit: 0}, [.18, .19, .16], 0, .6);
      for (i = 0; i < 17; i++) { var a = Math.PI * (1.12 + i * .0455), cx = Math.cos(a) * .0505, cy = Math.sin(a) * .0505; ell(geo, {c: [cx, cy, .0035], r: [.0046, .0028, .0028], rol: a + Math.PI / 2, pit: i % 2 ? 1.5 : 0}, gold, .95, .16); }
      ell(geo, {c: [0, -.0585, .0050], r: [.0050, .0066, .0030], rol: 0, pit: 0}, gold, .95, .14);
      ell(geo, {c: [0, -.0585, .0072], r: [.0016, .0050, .0012], rol: 0, pit: 0}, [1, .95, .7], .9, .2);
      ell(geo, {c: [0, -.0585, .0072], r: [.0040, .0015, .0012], rol: 0, pit: 0}, [1, .95, .7], .9, .2);
    }
    if (kind === 'pop') { // Warhol: ring of pop-art studs + banana peeking from behind
      var PC = [[1, .1, .55], [1, .86, .05], [.05, .8, .95], [1, .45, .05]];
      for (i = 0; i < 16; i++) { var b2 = i / 16 * Math.PI * 2 + .1; ell(geo, {c: [Math.cos(b2) * .0515, Math.sin(b2) * .0515, .0035], r: [.0058, .0058, .0058], rol: 0, pit: 0}, PC[i % 4], .05, .3); }
    }
    if (kind === 'quill') { // Poets: quill feather + two notes, gold
      var G = [1, .8, .3], F = [.96, .94, .88];
      tube(geo, [[-.041, -.052, .0125], [-.020, -.030, .0135], [.002, -.006, .0145], [.026, .018, .0155]], function (t) { return .0015 * (1 - t * .75); }, G, .9, .2);
      ell(geo, {c: [-.009, -.019, .0125], r: [.0235, .0060, .0016], rol: .83, pit: 0}, F, 0, .6);
      ell(geo, {c: [-.003, -.0125, .0108], r: [.0140, .0038, .0013], rol: .83, pit: 0}, [.82, .78, .66], 0, .7);
      [[.025, -.043], [.045, -.036]].forEach(function (p) {
        ell(geo, {c: [p[0], p[1], .0125], r: [.0066, .0047, .0034], rol: .42, pit: 0}, G, .9, .2);
        tube(geo, [[p[0] + .0056, p[1] + .001, .0125], [p[0] + .0056, p[1] + .015, .0125], [p[0] + .0056, p[1] + .030, .0125]], function () { return .0016; }, G, .9, .2);
      });
      tube(geo, [[.0306, -.013, .0125], [.0396, -.0112, .0125], [.0506, -.0062, .0125]], function () { return .0021; }, G, .9, .2);
    }
  }

  /* ---------- textures ---------- */
  function wrapText(x, text, maxw) { var words = String(text).split(/\s+/), lines = [], cur = ''; words.forEach(function (w) { var t = cur ? cur + ' ' + w : w; if (x.measureText(t).width > maxw && cur) { lines.push(cur); cur = w; } else cur = t; }); if (cur) lines.push(cur); return lines; }
  function backCanvas(b) {
    b = b || {}; var S = 1024, c = document.createElement('canvas'); c.width = c.height = S; var x = c.getContext('2d'), cx = S / 2, i;
    var g = x.createRadialGradient(cx - 120, cx - 160, 40, cx, cx, S * .62); var bp = b.pal ? b.pal.map(hx) : null, sh = function (c, k) { return 'rgb(' + c.map(function (v) { return Math.round(Math.min(1, v * k) * 255); }).join(',') + ')'; }; g.addColorStop(0, bp ? sh(bp[1], .9) : '#9a1534'); g.addColorStop(.55, bp ? sh(bp[1], .5) : '#5e0b20'); g.addColorStop(1, bp ? sh(bp[2], .8) : '#2a0510'); x.fillStyle = g; x.fillRect(0, 0, S, S);
    x.save(); x.translate(cx, cx); for (i = 0; i < 120; i++) { x.rotate(Math.PI * 2 / 120); x.fillStyle = 'rgba(255,255,255,.03)'; x.fillRect(-1, -S / 2, 2, S / 2 - 150); } x.restore();
    x.strokeStyle = 'rgba(255,215,220,.7)'; x.lineWidth = 6; x.beginPath(); x.arc(cx, cx, 470, 0, 7); x.stroke();
    x.strokeStyle = 'rgba(255,215,220,.3)'; x.lineWidth = 3; x.setLineDash([3, 14]); x.beginPath(); x.arc(cx, cx, 438, 0, 7); x.stroke(); x.setLineDash([]);
    x.fillStyle = 'rgba(255,225,230,.9)'; x.beginPath(); for (i = 0; i < 10; i++) { var a = -Math.PI / 2 + i * Math.PI / 5, r = i % 2 ? 26 : 62; x.lineTo(cx + Math.cos(a) * r, 250 + Math.sin(a) * r); } x.closePath(); x.fill();
    x.fillStyle = '#fff1f3'; x.textAlign = 'center'; x.shadowColor = 'rgba(0,0,0,.55)'; x.shadowBlur = 12; x.shadowOffsetY = 5;
    var fs = 110, name = b.name || 'Медаль', lines; do { x.font = '900 ' + fs + 'px Georgia,serif'; lines = wrapText(x, name, 620); fs -= 6; } while ((lines.length > 3 || lines.length * fs * 1.12 > 320) && fs > 44);
    var y0 = 500 - (lines.length - 1) * fs * .55; lines.forEach(function (l, k) { x.fillText(l, cx, y0 + k * fs * 1.12); });
    x.shadowColor = 'transparent'; x.fillStyle = 'rgba(255,225,230,.55)'; x.font = '800 34px system-ui,sans-serif'; x.fillText('G E M D R O P', cx, 800);
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
    var count = 0, medalCount = 0, alive = true, RC = .0545, RR = .0075, Rr = .0026, HY = RC + RR - Rr - .0004, PV = [0, HY, 0], handle = {
      setBack: function (b) { try { up(gl, tb, 1, backCanvas(b)); } catch (e) {} },
      destroy: function () { alive = false; },
      rot: function (a, b, c) { ry = a; rx = b; if (c != null) { sz = c; vsz = 0; } auto = false; }
    };
    if (o.img) { var im = new Image(); im.onload = function () { var c = document.createElement('canvas'); c.width = c.height = 1024; var x = c.getContext('2d'); x.imageSmoothingQuality = 'high'; x.drawImage(im, 0, 0, 1024, 1024); up(gl, tf, 0, c); gl.useProgram(pr); gl.uniform1f(U('uHas'), 1); }; im.src = o.img; }
    gl.enable(gl.DEPTH_TEST); gl.enable(gl.BLEND); gl.blendFunc(gl.SRC_ALPHA, gl.ONE_MINUS_SRC_ALPHA); gl.clearColor(0, 0, 0, 0);
    loadModel().then(function (g) {
      var geo = new Geo(); addGlb(geo, g, o);
      // portrait disc (our own artwork, uv from position so the whole picture fills the circle)
      var pr0 = .0373 * 1, seg = 96, c0 = geo.add([0, 0, .0043], [0, 0, 1], [.5, .5], [1, 1, 1], [0, .5, 1]), i;
      for (i = 0; i <= seg; i++) { var a = i / seg * Math.PI * 2, x = Math.cos(a) * pr0, y = Math.sin(a) * pr0; geo.add([x, y, .0043], [0, 0, 1], [.5 + x / (2 * pr0), .5 - y / (2 * pr0)], [1, 1, 1], [0, .5, 1]); }
      for (i = 0; i < seg; i++) geo.i.push(c0, c0 + 1 + i, c0 + 2 + i);
      disc(geo, .0455, -.0042, -1, 2, 2, 96);
      torus(geo, 0, 0, -.0043, .0462, .0007, [1, .7, .71], .65, .22);
      torus(geo, 0, RC, 0, RR, Rr, [.93, .86, .88], .95, .16);
      if (o.deco) decorate(geo, o.deco, o.pal);
      medalCount = geo.i.length;
      ribbonLoop(geo, HY);
      var vb = gl.createBuffer(), ib = gl.createBuffer();
      gl.useProgram(pr); gl.bindBuffer(gl.ARRAY_BUFFER, vb); gl.bufferData(gl.ARRAY_BUFFER, new Float32Array(geo.v), gl.STATIC_DRAW);
      gl.bindBuffer(gl.ELEMENT_ARRAY_BUFFER, ib); gl.bufferData(gl.ELEMENT_ARRAY_BUFFER, new Uint16Array(geo.i), gl.STATIC_DRAW);
      [['aP', 3, 0], ['aN', 3, 3], ['aUV', 2, 6], ['aC', 3, 8], ['aK', 3, 11]].forEach(function (a) { var l = gl.getAttribLocation(pr, a[0]); gl.enableVertexAttribArray(l); gl.vertexAttribPointer(l, a[1], gl.FLOAT, false, 56, a[2] * 4); });
      count = geo.i.length; start();
    }).catch(function () { alive = false; if (o.onfail) o.onfail(); });

    var P = persp(30 * Math.PI / 180, W / H, .5, 30), lt = 0, ry = -.55, rx = .08, vy = 0, vx = 0, down = false, lx = 0, ly = 0, t0 = performance.now(), tp = t0;
    var sz = 0, vsz = 1.7, sx = 0, vsx = .5;       // pendulum swing around the ring (z = side to side, x = front/back)
    var auto = !(window.matchMedia && matchMedia('(prefers-reduced-motion:reduce)').matches);
    function rotZ(a) { var c = Math.cos(a), s2 = Math.sin(a); return new Float32Array([c, s2, 0, 0, -s2, c, 0, 0, 0, 0, 1, 0, 0, 0, 0, 1]); }
    function start() {
      function frame(now) {
        if (!alive || !host.isConnected) return;
        var T = (now - t0) / 1000, dt = Math.min(.04, (now - tp) / 1000); tp = now;
        // twist spring: the ribbon winds up as the medal turns, so it gets harder to turn and unwinds back (with overshoot)
        if (!down) { var tgt = auto ? .55 * Math.sin(T * .55) : 0; vy += (-7 * (ry - tgt) - 2.0 * vy) * dt; ry += vy * dt; rx += (.08 + (auto ? .02 * Math.sin(T * .8) : 0) - rx) * .05; if (Math.abs(vy) > 2) vsx += Math.sin(T * 40) * Math.abs(vy) * dt * .05; }
        ry = Math.max(-9.4, Math.min(9.4, ry));
        // damped pendulum: ring hangs on the ribbon; spinning and dragging kick it
        vsz += (-11 * sz - 1.1 * vsz + (auto ? .5 * Math.sin(T * 1.25) : 0)) * dt; sz += vsz * dt;
        vsx += (-11 * sx - 1.3 * vsx + (auto ? .25 * Math.sin(T * .9 + 1) : 0)) * dt; sx += vsx * dt;
        sz = Math.max(-.7, Math.min(.7, sz)); sx = Math.max(-.5, Math.min(.5, sx));
        var Rm = mul(rotZ(sz), mul(rotX(sx), rotY(ry))), Mm = mul(trans(PV[0], PV[1], PV[2]), mul(Rm, trans(-PV[0], -PV[1], -PV[2])));
        var W0 = mul(trans(0, -.2, -5.9), mul(rotX(rx), scl(o.deco ? 17.5 : 20)));
        gl.viewport(0, 0, cv.width, cv.height); gl.clear(gl.COLOR_BUFFER_BIT | gl.DEPTH_BUFFER_BIT); gl.uniform1f(U('uT'), T); gl.uniform1f(U('uTw'), ry);
        gl.uniformMatrix4fv(U('uMVP'), false, mul(P, mul(W0, Mm))); gl.uniformMatrix3fv(U('uR'), false, m3(mul(rotX(rx), Rm)));
        gl.drawElements(gl.TRIANGLES, medalCount, gl.UNSIGNED_SHORT, 0);
        gl.uniformMatrix4fv(U('uMVP'), false, mul(P, W0)); gl.uniformMatrix3fv(U('uR'), false, m3(rotX(rx)));
        gl.drawElements(gl.TRIANGLES, count - medalCount, gl.UNSIGNED_SHORT, medalCount * 2);
        requestAnimationFrame(frame);
      }
      requestAnimationFrame(frame);
    }
    cv.addEventListener('pointerdown', function (e) { down = true; auto = false; lx = e.clientX; ly = e.clientY; vy = vx = 0; cv.style.cursor = 'grabbing'; try { cv.setPointerCapture(e.pointerId); } catch (x) {} });
    cv.addEventListener('pointermove', function (e) { if (!down) return; var dx = e.clientX - lx, dy = e.clientY - ly, res = 1 / (1 + Math.pow(ry / 4.2, 2)), d = dx * .0095 * res; ry += d; vy = d / Math.max(.008, (e.timeStamp - lt) / 1000); lt = e.timeStamp; vsz += dx * .03; vsx += dy * .02; rx = Math.max(-.35, Math.min(.35, rx + dy * .003)); lx = e.clientX; ly = e.clientY; });
    function upf() { down = false; cv.style.cursor = 'grab'; vy = Math.max(-14, Math.min(14, vy)); } cv.addEventListener('pointerup', upf); cv.addEventListener('pointercancel', upf); cv.addEventListener('lostpointercapture', upf); window.addEventListener('blur', upf);
    cv.addEventListener('dblclick', function () { vy = 9; vsz += 1.6; });
    return handle;
  }
  window.GDMedal3D = {mount: mount};
})();
