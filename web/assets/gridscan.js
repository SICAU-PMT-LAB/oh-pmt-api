/* 透视网格与光波。
   把 frontend/src/components/bits/GridScan.vue 的片元着色器原样搬过来，
   去掉 three.js 与后处理管线：一个全屏 quad + 一个程序，直出。

   与 Vue 版的差别只有三处，都不改变观感：
     1. 只用 WebGL1，移动端兼容面更大；
     2. 泛光本来就在着色器里用解析高斯算，不需要 EffectComposer；
     3. 参数从 JS 常量传，不读 props。

   性能守卫：设备像素比封顶 2；页面隐藏或滚动出视口时停掉 rAF；
   用户偏好减弱动态时只渲染一帧静态画面。
*/

// GLSL ES 1.00 / 3.00 两版。差别只是 attribute→in、varying→in、gl_FragColor→out，
// 因为 WebGL2 只对 #version 300 es 的着色器才把 fwidth 视为内置函数。
const VERT_1 = `
attribute vec2 aPos;
varying vec2 vUv;
void main(){
  vUv = aPos * 0.5 + 0.5;
  gl_Position = vec4(aPos, 0.0, 1.0);
}`;

const VERT_3 = `#version 300 es
in vec2 aPos;
out vec2 vUv;
void main(){
  vUv = aPos * 0.5 + 0.5;
  gl_Position = vec4(aPos, 0.0, 1.0);
}`;

const FRAG_BODY = `
precision highp float;
uniform vec3  iResolution;
uniform float iTime;
uniform vec2  uSkew;
uniform float uTilt;
uniform float uYaw;
uniform float uLineThickness;
uniform vec3  uLinesColor;
uniform vec3  uScanColor;
uniform float uGridScale;
uniform float uLineStyle;
uniform float uLineJitter;
uniform float uScanOpacity;
uniform float uScanDirection;
uniform float uNoise;
uniform float uBloomOpacity;
uniform float uScanGlow;
uniform float uScanSoftness;
uniform float uScanDuration;
uniform float uScanDelay;
uniform vec2  uRipplePos;
uniform float uRippleAmp;
uniform float uRippleRadius;

vec2 rippleOffset(vec2 fragCoord) {
  if (uRippleAmp <= 0.0001) return vec2(0.0);
  vec2 sp = fragCoord / iResolution.xy;
  vec2 d = sp - uRipplePos;
  d.x *= iResolution.x / iResolution.y;
  float r2 = dot(d, d);
  float sigma = max(0.01, uRippleRadius);
  float falloff = exp(-r2 / (2.0 * sigma * sigma));
  float len = sqrt(r2) + 1e-5;
  return (d / len) * (uRippleAmp * falloff);
}

float smoother01(float a, float b, float x){
  float t = clamp((x - a) / max(1e-5, (b - a)), 0.0, 1.0);
  return t * t * t * (t * (t * 6.0 - 15.0) + 10.0);
}

void mainImage(out vec4 fragColor, in vec2 fragCoord)
{
  vec2 p = (2.0 * fragCoord - iResolution.xy) / iResolution.y;

  vec3 ro = vec3(0.0);
  vec3 rd = normalize(vec3(p, 2.0));

  float cR = cos(uTilt), sR = sin(uTilt);
  rd.xy = mat2(cR, -sR, sR, cR) * rd.xy;

  float cY = cos(uYaw), sY = sin(uYaw);
  rd.xz = mat2(cY, -sY, sY, cY) * rd.xz;

  vec2 skew = clamp(uSkew, vec2(-0.7), vec2(0.7));
  rd.xy += skew * rd.z;

  float minT = 1e20;
  float gridScale = max(1e-5, uGridScale);
  vec2 gridUV = vec2(0.0);
  float hitIsY = 1.0;

  for (int i = 0; i < 4; i++)
  {
    float isY = float(i < 2);
    float pos = mix(-0.2, 0.2, float(i)) * isY + mix(-0.5, 0.5, float(i - 2)) * (1.0 - isY);
    float num = pos - (isY * ro.y + (1.0 - isY) * ro.x);
    float den = isY * rd.y + (1.0 - isY) * rd.x;
    float t = num / den;
    vec3 h = ro + rd * t;

    float depthBoost = smoothstep(0.0, 3.0, h.z);
    h.xy += skew * 0.15 * depthBoost;

    bool use = t > 0.0 && t < minT;
    gridUV = use ? mix(h.zy, h.xz, isY) / gridScale : gridUV;
    minT = use ? t : minT;
    hitIsY = use ? isY : hitIsY;
  }

  vec3 hit = ro + rd * minT;
  float dist = length(hit - ro);

  float jitterAmt = clamp(uLineJitter, 0.0, 1.0);
  if (jitterAmt > 0.0) {
    vec2 j = vec2(
      sin(gridUV.y * 2.7 + iTime * 1.8),
      cos(gridUV.x * 2.3 - iTime * 1.6)
    ) * (0.15 * jitterAmt);
    gridUV += j;
  }
  gridUV += rippleOffset(fragCoord);
  float fx = fract(gridUV.x);
  float fy = fract(gridUV.y);
  float ax = min(fx, 1.0 - fx);
  float ay = min(fy, 1.0 - fy);
  float wx = fwidth(gridUV.x);
  float wy = fwidth(gridUV.y);
  float halfPx = max(0.0, uLineThickness) * 0.5;

  float tx = halfPx * wx;
  float ty = halfPx * wy;
  float aax = wx;
  float aay = wy;

  float lineX = 1.0 - smoothstep(tx, tx + aax, ax);
  float lineY = 1.0 - smoothstep(ty, ty + aay, ay);
  if (uLineStyle > 0.5) {
    float dashRepeat = 4.0;
    float dashDuty = 0.5;
    float vy = fract(gridUV.y * dashRepeat);
    float vx = fract(gridUV.x * dashRepeat);
    float dashMaskY = step(vy, dashDuty);
    float dashMaskX = step(vx, dashDuty);
    if (uLineStyle < 1.5) {
      lineX *= dashMaskY;
      lineY *= dashMaskX;
    } else {
      float dotRepeat = 6.0;
      float dotWidth = 0.18;
      float cy = abs(fract(gridUV.y * dotRepeat) - 0.5);
      float cx = abs(fract(gridUV.x * dotRepeat) - 0.5);
      float dotMaskY = 1.0 - smoothstep(dotWidth, dotWidth + fwidth(gridUV.y * dotRepeat), cy);
      float dotMaskX = 1.0 - smoothstep(dotWidth, dotWidth + fwidth(gridUV.x * dotRepeat), cx);
      lineX *= dotMaskY;
      lineY *= dotMaskX;
    }
  }
  float primaryMask = max(lineX, lineY);

  vec2 gridUV2 = (hitIsY > 0.5 ? hit.xz : hit.zy) / gridScale;
  if (jitterAmt > 0.0) {
    vec2 j2 = vec2(
      cos(gridUV2.y * 2.1 - iTime * 1.4),
      sin(gridUV2.x * 2.5 + iTime * 1.7)
    ) * (0.15 * jitterAmt);
    gridUV2 += j2;
  }
  gridUV2 += rippleOffset(fragCoord);
  float fx2 = fract(gridUV2.x);
  float fy2 = fract(gridUV2.y);
  float ax2 = min(fx2, 1.0 - fx2);
  float ay2 = min(fy2, 1.0 - fy2);
  float wx2 = fwidth(gridUV2.x);
  float wy2 = fwidth(gridUV2.y);
  float tx2 = halfPx * wx2;
  float ty2 = halfPx * wy2;
  float aax2 = wx2;
  float aay2 = wy2;
  float lineX2 = 1.0 - smoothstep(tx2, tx2 + aax2, ax2);
  float lineY2 = 1.0 - smoothstep(ty2, ty2 + aay2, ay2);
  if (uLineStyle > 0.5) {
    float dashRepeat2 = 4.0;
    float dashDuty2 = 0.5;
    float vy2m = fract(gridUV2.y * dashRepeat2);
    float vx2m = fract(gridUV2.x * dashRepeat2);
    float dashMaskY2 = step(vy2m, dashDuty2);
    float dashMaskX2 = step(vx2m, dashDuty2);
    if (uLineStyle < 1.5) {
      lineX2 *= dashMaskY2;
      lineY2 *= dashMaskX2;
    } else {
      float dotRepeat2 = 6.0;
      float dotWidth2 = 0.18;
      float cy2 = abs(fract(gridUV2.y * dotRepeat2) - 0.5);
      float cx2 = abs(fract(gridUV2.x * dotRepeat2) - 0.5);
      float dotMaskY2 = 1.0 - smoothstep(dotWidth2, dotWidth2 + fwidth(gridUV2.y * dotRepeat2), cy2);
      float dotMaskX2 = 1.0 - smoothstep(dotWidth2, dotWidth2 + fwidth(gridUV2.x * dotRepeat2), cx2);
      lineX2 *= dotMaskY2;
      lineY2 *= dotMaskX2;
    }
  }
  float altMask = max(lineX2, lineY2);

  float edgeDistX = min(abs(hit.x - (-0.5)), abs(hit.x - 0.5));
  float edgeDistY = min(abs(hit.y - (-0.2)), abs(hit.y - 0.2));
  float edgeDist = mix(edgeDistY, edgeDistX, hitIsY);
  float edgeGate = 1.0 - smoothstep(gridScale * 0.5, gridScale * 2.0, edgeDist);
  altMask *= edgeGate;

  float lineMask = max(primaryMask, altMask);
  float fade = exp(-dist * 1.35);

  float dur = max(0.05, uScanDuration);
  float del = max(0.0, uScanDelay);
  float scanZMax = 2.0;
  float widthScale = max(0.1, uScanGlow);
  float sigma = max(0.001, 0.14 * widthScale * uScanSoftness);
  float sigmaFront = sigma * 0.48;
  float sigmaTail = sigma * 1.45;
  float sigmaA = sigma * 2.2;
  float combinedPulse = 0.0;
  float combinedAura = 0.0;

  float cycle = dur + del;
  float tCycle = mod(iTime + del * 0.85, cycle);
  float scanPhase = clamp((tCycle - del) / dur, 0.0, 1.0);
  float phase = scanPhase;
  float dirSign = 1.0;
  if (uScanDirection > 0.5 && uScanDirection < 1.5) {
    phase = 1.0 - phase;
    dirSign = -1.0;
  } else if (uScanDirection > 1.5) {
    float t2 = mod(max(0.0, iTime - del), 2.0 * dur);
    dirSign = (t2 < dur) ? 1.0 : -1.0;
    phase = (t2 < dur) ? (t2 / dur) : (1.0 - (t2 - dur) / dur);
  }
  float scanZ = phase * scanZMax;
  float signedDz = (hit.z - scanZ) * dirSign;
  float activeSigma = (signedDz >= 0.0) ? sigmaFront : sigmaTail;
  float lineBand = exp(-0.5 * (signedDz * signedDz) / (activeSigma * activeSigma));
  float headW = 0.12;
  float tailW = 0.32;
  float headFade = smoother01(0.0, headW, phase);
  float tailFade = 1.0 - smoother01(1.0 - tailW, 1.0, phase);
  float phaseWindow = (scanPhase > 0.0 && scanPhase < 1.0) ? (headFade * tailFade) : 0.0;
  combinedPulse += lineBand * phaseWindow * clamp(uScanOpacity, 0.0, 1.0);
  float auraBand = exp(-0.5 * (signedDz * signedDz) / (sigmaA * sigmaA));
  combinedAura += (auraBand * 0.32) * phaseWindow * clamp(uScanOpacity, 0.0, 1.0);

  float lineVis = lineMask;
  float gx = 1.0 - smoothstep(tx * 2.2, tx * 2.2 + aax * 3.5, ax);
  float gy = 1.0 - smoothstep(ty * 2.2, ty * 2.2 + aay * 3.5, ay);
  float haloMask = max(gx, gy);
  float nodeMask = lineX * lineY;

  float bloomBoost = clamp(uBloomOpacity, 0.0, 1.0) * 0.45;
  float gridAlpha = clamp((lineVis + haloMask * bloomBoost) * fade, 0.0, 1.0);

  float scanDistFade = exp(-dist * 0.45);
  float scanOnGrid = (lineVis * 1.35 + haloMask * 0.72 + nodeMask * 0.9) * combinedPulse;
  float scanVolumetric = (combinedPulse * 0.11 + combinedAura * 0.38) * fade;
  float scanAlpha = clamp((scanOnGrid + scanVolumetric) * scanDistFade, 0.0, 1.0);

  float alpha = clamp(max(gridAlpha, scanAlpha), 0.0, 1.0);
  float scanMix = clamp(scanAlpha / max(alpha, 1e-4), 0.0, 1.0);
  vec3 color = mix(uLinesColor, uScanColor, scanMix);
  color += uScanColor * (combinedPulse * (lineVis * 0.4 + nodeMask * 0.65) * scanDistFade);

  float n = fract(sin(dot(gl_FragCoord.xy + vec2(iTime * 123.4), vec2(12.9898, 78.233))) * 43758.5453123);
  color += (n - 0.5) * uNoise;
  color = clamp(color, 0.0, 1.0);
  fragColor = vec4(color, alpha);
}

void main(){
  vec4 c;
  mainImage(c, vUv * iResolution.xy);
  outColor = c;
}`;

const FRAG_1 = `${FRAG_BODY.replace('outColor', 'gl_FragColor')}`.replace(
  'precision highp float;',
  '#extension GL_OES_standard_derivatives : enable\nprecision highp float;\nvarying vec2 vUv;');

const FRAG_3 = `#version 300 es\n${FRAG_BODY}`.replace(
  'precision highp float;',
  'precision highp float;\nin vec2 vUv;\nout vec4 outColor;');

function hexToRgb(hex) {
  const s = hex.replace('#', '');
  const full = s.length === 3 ? s.split('').map((c) => c + c).join('') : s;
  const n = parseInt(full, 16);
  return [((n >> 16) & 255) / 255, ((n >> 8) & 255) / 255, (n & 255) / 255];
}

function smoothDamp(current, target, vel, smoothTime, maxSpeed, dt) {
  smoothTime = Math.max(0.0001, smoothTime);
  const omega = 2 / smoothTime;
  const x = omega * dt;
  const exp = 1 / (1 + x + 0.48 * x * x + 0.235 * x * x * x);
  let change = current - target;
  const originalTo = target;
  const maxChange = maxSpeed * smoothTime;
  change = Math.sign(change) * Math.min(Math.abs(change), maxChange);
  const t = current - change;
  const temp = (vel.v + omega * change) * dt;
  vel.v = (vel.v - omega * temp) * exp;
  let out = t + (change + temp) * exp;
  const origMinusCurrent = originalTo - current;
  const outMinusOrig = out - originalTo;
  if (origMinusCurrent * outMinusOrig > 0) {
    out = originalTo;
    vel.v = 0;
  }
  return out;
}

export function initGridScan(canvas, opts = {}) {
  const options = {
    sensitivity: 0.55,
    lineThickness: 1,
    linesColor: '#b5b0a3',
    scanColor: '#1c1c1a',
    scanOpacity: 0.78,
    gridScale: 0.12,
    lineStyle: 'solid',
    lineJitter: 0.035,
    noiseIntensity: 0.012,
    scanGlow: 0.78,
    scanSoftness: 1.9,
    scanDuration: 2.6,
    scanDelay: 0.8,
    scanDirection: 'forward',
    bloomIntensity: 0.28,
    rippleAmp: 0.55,
    rippleRadius: 0.16,
    snapBackDelay: 250,
    ...opts,
  };

  // 优先 WebGL2：fwidth 在 GLSL ES 3.00 里是内置函数，不用扩展。
  // 退回 WebGL1 时必须显式打开 OES_standard_derivatives，否则片元着色器
  // 编译不过（fwidth 无处可寻），整块背景会空白。
  const attrs = {
    alpha: true, antialias: false, premultipliedAlpha: false,
    powerPreference: 'low-power',
  };
  let gl = canvas.getContext('webgl2', attrs);
  let vertSrc = VERT_3;
  let fragSrc = FRAG_3;
  if (!gl) {
    gl = canvas.getContext('webgl', attrs) || canvas.getContext('experimental-webgl', attrs);
    if (gl && !gl.getExtension('OES_standard_derivatives')) {
      // 没有导数扩展就画不出抗锯齿的网格线，直接放弃背景，别留一块黑
      return { destroy() {}, setTheme() {} };
    }
    vertSrc = VERT_1;
    fragSrc = FRAG_1;
  }
  if (!gl) return { destroy() {}, setTheme() {} };

  const compile = (type, src) => {
    const sh = gl.createShader(type);
    gl.shaderSource(sh, src);
    gl.compileShader(sh);
    if (!gl.getShaderParameter(sh, gl.COMPILE_STATUS)) {
      console.error('shader:', gl.getShaderInfoLog(sh));
      return null;
    }
    return sh;
  };

  const vs = compile(gl.VERTEX_SHADER, vertSrc);
  const fs = compile(gl.FRAGMENT_SHADER, fragSrc);
  if (!vs || !fs) return { destroy() {}, setTheme() {} };
  const prog = gl.createProgram();
  gl.attachShader(prog, vs);
  gl.attachShader(prog, fs);
  gl.linkProgram(prog);
  if (!gl.getProgramParameter(prog, gl.LINK_STATUS)) {
    console.error('link:', gl.getProgramInfoLog(prog));
    return { destroy() {}, setTheme() {} };
  }
  gl.useProgram(prog);

  const buf = gl.createBuffer();
  gl.bindBuffer(gl.ARRAY_BUFFER, buf);
  gl.bufferData(gl.ARRAY_BUFFER, new Float32Array([
    -1, -1, 1, -1, -1, 1, -1, 1, 1, -1, 1, 1,
  ]), gl.STATIC_DRAW);
  const loc = gl.getAttribLocation(prog, 'aPos');
  gl.enableVertexAttribArray(loc);
  gl.vertexAttribPointer(loc, 2, gl.FLOAT, false, 0, 0);

  const U = {};
  ['iResolution', 'iTime', 'uSkew', 'uTilt', 'uYaw', 'uLineThickness',
   'uLinesColor', 'uScanColor', 'uGridScale', 'uLineStyle', 'uLineJitter',
   'uScanOpacity', 'uScanDirection', 'uNoise', 'uBloomOpacity', 'uScanGlow',
   'uScanSoftness', 'uScanDuration', 'uScanDelay', 'uRipplePos', 'uRippleAmp',
   'uRippleRadius'].forEach((name) => {
    U[name] = gl.getUniformLocation(prog, name);
  });

  const state = {
    lookTarget: [0, 0],
    lookCur: [0, 0],
    lookVel: [0, 0],
    tilt: 0, tiltVel: { v: 0 },
    yaw: 0, yawVel: { v: 0 },
    rippleTarget: [0.5, 0.5],
    rippleCur: [0.5, 0.5],
    rippleAmp: 0,
    rippleAmpTarget: 0,
    seeded: false,
  };

  let width = 0, height = 0, dpr = 1;
  const resize = () => {
    dpr = Math.min(window.devicePixelRatio || 1, 2);
    // 画布是 position:fixed 全屏层，尺寸必须取视口。用 clientHeight 会在
    // 页面加载早期拿到整页高度，画布被拉高后网格被压扁、只覆盖首屏。
    width = canvas.clientWidth || window.innerWidth;
    height = window.innerHeight;
    canvas.width = Math.max(1, Math.round(width * dpr));
    canvas.height = Math.max(1, Math.round(height * dpr));
    gl.viewport(0, 0, canvas.width, canvas.height);
  };
  resize();
  requestAnimationFrame(resize);

  gl.enable(gl.BLEND);
  gl.blendFuncSeparate(gl.SRC_ALPHA, gl.ONE_MINUS_SRC_ALPHA, gl.ONE, gl.ONE_MINUS_SRC_ALPHA);
  gl.clearColor(0, 0, 0, 0);

  let linesColor = hexToRgb(options.linesColor);
  let scanColor = hexToRgb(options.scanColor);

  // 传入 linesColorDark/linesColorLight 时以调用方为准：站点要黑金/白金线条，
  // 不能被这里的默认灰盖掉。
  const setTheme = (isDark) => {
    const line = isDark
      ? (options.linesColorDark || '#706c61')
      : (options.linesColorLight || '#b5b0a3');
    const scan = isDark
      ? (options.scanColorDark || '#fff3d1')
      : (options.scanColorLight || '#1c1c1a');
    linesColor = hexToRgb(line);
    scanColor = hexToRgb(scan);
    options.scanOpacity = isDark ? 0.95 : 0.78;
    options.scanGlow = isDark ? 0.72 : 0.78;
    options.scanSoftness = isDark ? 1.6 : 1.9;
    options.bloomIntensity = isDark ? 0.55 : 0.28;
  };

  let leaveTimer = null;
  const onPointer = (e) => {
    if (leaveTimer) { clearTimeout(leaveTimer); leaveTimer = null; }
    const rect = canvas.getBoundingClientRect();
    const nx = ((e.clientX - rect.left) / rect.width) * 2 - 1;
    const ny = -(((e.clientY - rect.top) / rect.height) * 2 - 1);
    state.lookTarget = [nx, ny];
    state.rippleTarget = [(nx + 1) * 0.5, (ny + 1) * 0.5];
    if (!state.seeded) {
      state.rippleCur = [...state.rippleTarget];
      state.seeded = true;
    }
    state.rippleAmpTarget = 1;
  };
  const onLeave = () => {
    if (leaveTimer) clearTimeout(leaveTimer);
    leaveTimer = setTimeout(() => {
      state.lookTarget = [0, 0];
      state.rippleAmpTarget = 0;
    }, options.snapBackDelay);
  };

  window.addEventListener('pointermove', onPointer, { passive: true });
  window.addEventListener('pointerleave', onLeave);
  window.addEventListener('resize', resize);
  window.addEventListener('orientationchange', resize);
  // 滚动条出现/消失会改变可用宽度，不触发 resize
  if (window.ResizeObserver) {
    const ro = new ResizeObserver(resize);
    ro.observe(document.documentElement);
    var resizeObserver = ro;
  }

  const motionQuery = window.matchMedia('(prefers-reduced-motion: reduce)');
  let rafId = null;
  let last = performance.now();

  const frame = (now) => {
    rafId = requestAnimationFrame(frame);
    const dt = Math.max(0, Math.min(0.1, (now - last) / 1000));
    last = now;

    const s = Math.min(1, Math.max(0, options.sensitivity));
    const skewScale = 0.06 + (0.2 - 0.06) * s;
    const smoothTime = 0.45 + (0.12 - 0.45) * s;
    const yBoost = 1.2 + (1.6 - 1.2) * s;

    const lvx = { v: state.lookVel[0] };
    const nx = smoothDamp(state.lookCur[0], state.lookTarget[0], lvx, smoothTime, Infinity, dt);
    state.lookVel[0] = lvx.v;
    const lvy = { v: state.lookVel[1] };
    const ny = smoothDamp(state.lookCur[1], state.lookTarget[1], lvy, smoothTime, Infinity, dt);
    state.lookVel[1] = lvy.v;
    state.lookCur = [nx, ny];

    state.tilt = smoothDamp(state.tilt, 0, state.tiltVel, smoothTime, Infinity, dt);
    state.yaw = smoothDamp(state.yaw, 0, state.yawVel, smoothTime, Infinity, dt);

    const lerpK = 1 - Math.pow(0.001, dt);
    state.rippleCur[0] += (state.rippleTarget[0] - state.rippleCur[0]) * lerpK;
    state.rippleCur[1] += (state.rippleTarget[1] - state.rippleCur[1]) * lerpK;
    state.rippleAmp += (state.rippleAmpTarget - state.rippleAmp) * (1 - Math.pow(0.02, dt));

    gl.uniform3f(U.iResolution, canvas.width, canvas.height, dpr);
    gl.uniform1f(U.iTime, now / 1000);
    gl.uniform2f(U.uSkew, nx * skewScale, -ny * yBoost * skewScale);
    gl.uniform1f(U.uTilt, state.tilt);
    gl.uniform1f(U.uYaw, state.yaw);
    gl.uniform1f(U.uLineThickness, options.lineThickness);
    gl.uniform3fv(U.uLinesColor, linesColor);
    gl.uniform3fv(U.uScanColor, scanColor);
    gl.uniform1f(U.uGridScale, options.gridScale);
    gl.uniform1f(U.uLineStyle, options.lineStyle === 'dashed' ? 1 : options.lineStyle === 'dotted' ? 2 : 0);
    gl.uniform1f(U.uLineJitter, options.lineJitter);
    gl.uniform1f(U.uScanOpacity, options.scanOpacity);
    gl.uniform1f(U.uScanDirection, options.scanDirection === 'backward' ? 1 : options.scanDirection === 'pingpong' ? 2 : 0);
    gl.uniform1f(U.uNoise, options.noiseIntensity);
    gl.uniform1f(U.uBloomOpacity, options.bloomIntensity);
    gl.uniform1f(U.uScanGlow, options.scanGlow);
    gl.uniform1f(U.uScanSoftness, options.scanSoftness);
    gl.uniform1f(U.uScanDuration, options.scanDuration);
    gl.uniform1f(U.uScanDelay, options.scanDelay);
    gl.uniform2f(U.uRipplePos, state.rippleCur[0], state.rippleCur[1]);
    gl.uniform1f(U.uRippleAmp, options.rippleAmp * state.rippleAmp);
    gl.uniform1f(U.uRippleRadius, options.rippleRadius);

    gl.clear(gl.COLOR_BUFFER_BIT);
    gl.drawArrays(gl.TRIANGLES, 0, 6);
  };

  const renderOnce = () => {
    last = performance.now();
    const now = last;
    gl.uniform3f(U.iResolution, canvas.width, canvas.height, dpr);
    gl.uniform1f(U.iTime, now / 1000);
    gl.uniform2f(U.uSkew, 0, 0);
    gl.uniform1f(U.uTilt, 0);
    gl.uniform1f(U.uYaw, 0);
    gl.uniform1f(U.uLineThickness, options.lineThickness);
    gl.uniform3fv(U.uLinesColor, linesColor);
    gl.uniform3fv(U.uScanColor, scanColor);
    gl.uniform1f(U.uGridScale, options.gridScale);
    gl.uniform1f(U.uLineStyle, options.lineStyle === 'dashed' ? 1 : options.lineStyle === 'dotted' ? 2 : 0);
    gl.uniform1f(U.uLineJitter, options.lineJitter);
    gl.uniform1f(U.uScanOpacity, options.scanOpacity);
    gl.uniform1f(U.uScanDirection, options.scanDirection === 'backward' ? 1 : options.scanDirection === 'pingpong' ? 2 : 0);
    gl.uniform1f(U.uNoise, options.noiseIntensity);
    gl.uniform1f(U.uBloomOpacity, options.bloomIntensity);
    gl.uniform1f(U.uScanGlow, options.scanGlow);
    gl.uniform1f(U.uScanSoftness, options.scanSoftness);
    gl.uniform1f(U.uScanDuration, options.scanDuration);
    gl.uniform1f(U.uScanDelay, options.scanDelay);
    gl.uniform2f(U.uRipplePos, 0.5, 0.5);
    gl.uniform1f(U.uRippleAmp, 0);
    gl.uniform1f(U.uRippleRadius, options.rippleRadius);
    gl.clear(gl.COLOR_BUFFER_BIT);
    gl.drawArrays(gl.TRIANGLES, 0, 6);
  };

  const start = () => {
    if (rafId === null && !motionQuery.matches && !document.hidden) {
      last = performance.now();
      rafId = requestAnimationFrame(frame);
    }
  };
  const stop = () => {
    if (rafId !== null) { cancelAnimationFrame(rafId); rafId = null; }
  };

  if (motionQuery.matches) renderOnce();
  else start();

  const onVisibility = () => (document.hidden ? stop() : start());
  document.addEventListener('visibilitychange', onVisibility);
  motionQuery.addEventListener?.('change', () => {
    if (motionQuery.matches) { stop(); renderOnce(); } else start();
  });

  return {
    setTheme,
    destroy() {
      stop();
      window.removeEventListener('pointermove', onPointer);
      window.removeEventListener('pointerleave', onLeave);
      window.removeEventListener('resize', resize);
      window.removeEventListener('orientationchange', resize);
      if (typeof resizeObserver !== 'undefined') resizeObserver.disconnect();
      document.removeEventListener('visibilitychange', onVisibility);
      if (leaveTimer) clearTimeout(leaveTimer);
      gl.deleteProgram(prog);
      gl.deleteBuffer(buf);
    },
  };
}
