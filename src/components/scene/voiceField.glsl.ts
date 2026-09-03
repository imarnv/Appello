/**
 * The voice field.
 *
 * One synthesised spectrogram — formant bands, a syllabic envelope, plosive
 * bursts — driven by the active language's signature, and posed two ways:
 *
 *   hero   two poses that alternate each time you come back to the top of the
 *          page — the spectrogram as topography, and the spectrogram wrapped
 *          around a line the way a call travels
 *   orb    a radial waveform in the demo panel, tilted into space
 *
 * Colour is the magma colormap, the same ramp used to plot real spectrograms.
 * The hero sits low on that ramp so it reads as texture on paper; the orb uses
 * a narrower violet-to-rose slice so it reads as one deliberate colour.
 */

export const vertexShader = /* glsl */ `
  precision highp float;

  attribute vec2 aGrid;      // (time 0..1, frequency 0..1)
  attribute float aJitter;   // per-point stable randomness
  attribute vec3 aSphere;    // evenly distributed unit direction

  uniform float uTime;
  uniform float uMode;       // 0 terrain, 1 filament
  uniform float uHeroExit;   // 0 in the hero, 1 once it has scrolled away
  uniform float uOrbIn;      // 0 before the demo panel arrives, 1 once settled
  uniform float uEnergy;     // conversational loudness, 0..1
  uniform float uTurn;       // -1 caller speaking, +1 agent speaking
  uniform vec2  uPointer;
  uniform float uSize;
  uniform vec3  uOrbCenter;  // where the panel's ring sits, in world space
  uniform float uOrbScale;

  // active (already-blended) signature
  uniform float uRate;
  uniform float uF1;
  uniform float uF2;
  uniform float uF3;
  uniform float uGlide;
  uniform float uBurst;
  uniform float uBreadth;

  varying float vEnergy;
  varying float vDepth;
  varying float vJitter;
  varying float vShow;
  varying float vOrb;

  const float TAU = 6.2831853;

  float hash11(float p) {
    p = fract(p * 0.1031);
    p *= p + 33.33;
    p *= p + p;
    return fract(p);
  }

  float vnoise(float x) {
    float i = floor(x);
    float f = fract(x);
    f = f * f * (3.0 - 2.0 * f);
    return mix(hash11(i), hash11(i + 1.0), f);
  }

  float gauss(float x, float s) {
    return exp(-(x * x) / (2.0 * s * s));
  }

  vec3 hash33(vec3 p) {
    p = vec3(dot(p, vec3(127.1, 311.7, 74.7)),
             dot(p, vec3(269.5, 183.3, 246.1)),
             dot(p, vec3(113.5, 271.9, 124.6)));
    return fract(sin(p) * 43758.5453) * 2.0 - 1.0;
  }

  float noise3(vec3 p) {
    vec3 i = floor(p);
    vec3 f = fract(p);
    vec3 u = f * f * (3.0 - 2.0 * f);
    return mix(
      mix(mix(dot(hash33(i + vec3(0,0,0)), f - vec3(0,0,0)),
              dot(hash33(i + vec3(1,0,0)), f - vec3(1,0,0)), u.x),
          mix(dot(hash33(i + vec3(0,1,0)), f - vec3(0,1,0)),
              dot(hash33(i + vec3(1,1,0)), f - vec3(1,1,0)), u.x), u.y),
      mix(mix(dot(hash33(i + vec3(0,0,1)), f - vec3(0,0,1)),
              dot(hash33(i + vec3(1,0,1)), f - vec3(1,0,1)), u.x),
          mix(dot(hash33(i + vec3(0,1,1)), f - vec3(0,1,1)),
              dot(hash33(i + vec3(1,1,1)), f - vec3(1,1,1)), u.x), u.y), u.z);
  }

  // Energy at a point on the time/frequency plane.
  float spectrum(float x, float f, float t) {
    // Syllabic envelope: a pulse train marching along the time axis, with each
    // syllable's amplitude varied so it never reads as a sine wave.
    float ph = x * uRate - t * 0.62;
    float sylIdx = floor(ph);
    float sylAmp = 0.42 + 0.58 * vnoise(sylIdx * 1.7);
    float syl = pow(max(0.0, sin(fract(ph) * 3.14159265)), 1.6) * sylAmp;

    // Formants drift within each syllable. Tonal languages glide much further.
    float glide = uGlide * 0.075 * sin(ph * TAU * 0.5 + vnoise(sylIdx) * 6.0);

    float bw = uBreadth * 0.052;
    float e = 0.0;
    e += 1.00 * gauss(f - (uF1 + glide * 0.6), bw);
    e += 0.74 * gauss(f - (uF2 + glide * 1.5), bw * 1.35);
    e += 0.44 * gauss(f - (uF3 + glide * 2.0), bw * 1.75);

    // Voiced excitation — the fine striations of the harmonic stack.
    float harm = 0.5 + 0.5 * sin(f * 148.0 + vnoise(sylIdx * 3.1) * 6.28);
    e *= mix(1.0, harm, 0.34);
    e *= syl;

    // Plosive bursts: short broadband stripes at syllable onsets, weighted to
    // high frequency the way real stops and affricates are.
    float onset = gauss(fract(ph) - 0.06, 0.026);
    e += uBurst * 0.55 * onset * sylAmp * (0.22 + 0.78 * f);

    // Breath floor, so silence still has grain.
    e += 0.018 * vnoise(f * 40.0 + x * 12.0 + t * 0.4);

    return e;
  }

  void main() {
    vJitter = aJitter;
    float t = uTime;
    vec2 g = aGrid;

    float e = spectrum(g.x, g.y, t);
    e *= 0.72 + 0.85 * uEnergy;

    // ── hero · terrain ──────────────────────────────────────────────────────
    // Low frequencies come toward the viewer — that is where the first two
    // formants sit, so the loud, structured part of speech fills the foreground
    // and the quiet upper harmonics dissolve into the horizon.
    float tEdge = smoothstep(0.5, 0.12, abs(g.x - 0.5)) *
                  smoothstep(0.54, 0.26, abs(g.y - 0.5));
    vec3 terrain = vec3(
      (g.x - 0.5) * 21.0,
      e * 2.8 * tEdge - 1.35,
      (0.5 - g.y) * 13.0
    );

    // ── hero · filament ─────────────────────────────────────────────────────
    // The same spectrogram wrapped around a line running across the page: one
    // call, in transit, bulging where the speech is loud.
    float fU = g.x;
    float fV = g.y * TAU;
    vec3 spine = vec3(
      (fU - 0.5) * 23.0,
      -0.6 + sin(fU * 5.2 + t * 0.22) * 1.15,
      cos(fU * 3.4 + t * 0.16) * 2.4
    );
    // Tangent of the spine, so the tube stays perpendicular to its own path.
    vec3 tang = normalize(vec3(
      23.0,
      cos(fU * 5.2 + t * 0.22) * 5.2 * 1.15,
      -sin(fU * 3.4 + t * 0.16) * 3.4 * 2.4
    ));
    vec3 nrm = normalize(cross(tang, vec3(0.0, 1.0, 0.0)));
    vec3 bin = cross(tang, nrm);
    float fEdge = smoothstep(0.0, 0.09, fU) * smoothstep(1.0, 0.91, fU);
    vec3 filament = spine + (nrm * cos(fV) + bin * sin(fV))
                  * (0.32 + e * 1.55) * fEdge;

    float m = clamp(uMode, 0.0, 1.0);
    vec3 hero = mix(terrain, filament, m);
    float show = mix(1.0, fEdge * 0.7 + 0.3, m);

    // Pointer parallax — the field leans toward the cursor.
    hero.y += uPointer.y * 0.5;
    hero.x += uPointer.x * 0.4;

    // ── orb · ring ──────────────────────────────────────────────────────────
    // A speech envelope. Deliberately slow: a voice modulates at syllable rate,
    // but a shape that *moves* at syllable rate just looks frantic. This runs at
    // roughly 1-3 Hz and drives how far things travel, not how fast.
    float raw   = noise3(vec3(t * 2.90, 7.1, 1.3)) * 0.5 + 0.5;
    float fastN = noise3(vec3(t * 5.40, 2.7, 9.4)) * 0.5 + 0.5;
    float envl  = pow(raw, 1.5) * 1.15 + pow(fastN, 1.6) * 0.35;
    float speech = max(uTurn, 0.0) * uEnergy;
    float voice = speech * clamp(envl, 0.0, 1.25);

    // Concentric rings whose radius is pushed by the voice.
    float RN = 26.0;
    float rT = floor(g.y * RN) / (RN - 1.0);
    float oAng = g.x * TAU;
    float baseR = 0.40 + rT * 0.60;

    // Outer rings carry more of the motion, so a loud moment reads as energy
    // radiating outward rather than the whole disc breathing at once.
    float rW = 0.32 + 0.68 * rT;

    // A pulse travelling out through the rings on every utterance. This is the
    // pop: a wave crossing the shape, rather than the shape inflating.
    float pulse = sin(rT * 8.0 - t * 7.2) * voice;

    float wf = sin(oAng * 5.0 - t * 2.10 + rT * 2.4) * 0.55
             + sin(oAng * 9.0 + t * 1.45 + rT * 1.1) * 0.30
             + pulse * 0.70;

    float rr = baseR
             * (1.0 + wf * (0.055 + voice * 0.34) * rW)
             * (1.0 + voice * 0.09);

    float yy = (sin(oAng * 3.0 + t * 1.70 + rT * 3.2) + pulse * 0.75)
             * (0.05 + voice * 0.32) * baseR;

    vec3 ringP = vec3(cos(oAng) * rr, yy, sin(oAng) * rr);
    // Tilt, so it sits in space as a disc rather than reading as a flat circle.
    ringP = vec3(ringP.x,
                 ringP.y * 0.88 - ringP.z * 0.48,
                 ringP.y * 0.48 + ringP.z * 0.88);

    vec3 orb = uOrbCenter + ringP * uOrbScale;

    // Rings crowd toward the centre; thin them so density stays even.
    float oFade = 0.35 + 0.65 * baseR;
    float orbE = clamp(0.24 + wf * 0.34 + voice * 0.62, 0.0, 1.0);

    // ── hero out, orb in ────────────────────────────────────────────────────
    // Two separate moments rather than one object travelling between them. The
    // hero field sinks and disperses as you leave the hero; later, and only
    // once the picker is in view, the orb gathers itself in place.
    float exitP = clamp(uHeroExit, 0.0, 1.0);
    hero.y -= exitP * exitP * 5.0;
    hero += aSphere * exitP * (0.8 + 2.6 * aJitter);
    float heroVis = 1.0 - smoothstep(0.0, 0.72, exitP);

    float inP = clamp(uOrbIn, 0.0, 1.0);
    vec3 orbPos = orb + normalize(ringP + vec3(0.001))
                * (1.0 - inP) * (0.35 + 1.5 * aJitter) * uOrbScale * 6.0;

    // The hero fades out fully *before* the pose switches, and the orb only
    // starts appearing after it. Both windows are pure functions of scroll
    // position, so scrolling back up replays the same fade in reverse.
    float heroV = heroVis * (1.0 - smoothstep(0.0, 0.06, inP));
    float posSwap = step(0.08, inP);
    float orbV = smoothstep(0.10, 0.55, inP);

    vec3 pos = mix(hero, orbPos, posSwap);
    vShow = mix(show * heroV, orbV * oFade, posSwap);
    vEnergy = clamp(mix(e, orbE, posSwap), 0.0, 1.6);
    vOrb = posSwap;

    vec4 mv = modelViewMatrix * vec4(pos, 1.0);
    vDepth = -mv.z;
    gl_Position = projectionMatrix * mv;

    // Points grow with energy, so loud regions read as brighter filament rather
    // than only as a taller ridge.
    float sizeE = mix(min(e, 1.0), 0.34 + orbE * 0.34, posSwap);
    float size = uSize * (0.55 + 1.2 * sizeE) * (0.72 + 0.56 * aJitter);
    gl_PointSize = max(1.0, size * (17.0 / max(vDepth, 0.6)));
  }
`;

export const fragmentShader = /* glsl */ `
  precision highp float;

  varying float vEnergy;
  varying float vDepth;
  varying float vJitter;
  varying float vShow;
  varying float vOrb;

  uniform float uFade;

  // magma colormap stops, low → high energy
  const vec3 S0 = vec3(0.788, 0.792, 0.820);  // #c9cad1 pale ink
  const vec3 S1 = vec3(0.231, 0.059, 0.439);  // #3b0f70 violet
  const vec3 S2 = vec3(0.549, 0.161, 0.506);  // #8c2981 magenta
  const vec3 S3 = vec3(0.871, 0.286, 0.408);  // #de4968 rose
  const vec3 S4 = vec3(0.996, 0.624, 0.427);  // #fe9f6d amber

  vec3 magma(float t) {
    t = clamp(t, 0.0, 1.0);
    if (t < 0.25) return mix(S0, S1, t / 0.25);
    if (t < 0.50) return mix(S1, S2, (t - 0.25) / 0.25);
    if (t < 0.75) return mix(S2, S3, (t - 0.50) / 0.25);
    return mix(S3, S4, (t - 0.75) / 0.25);
  }

  void main() {
    // Round, soft-edged points — square GL points read as noise at this density.
    vec2 c = gl_PointCoord - 0.5;
    float d = dot(c, c);
    if (d > 0.25) discard;
    float mask = smoothstep(0.25, 0.045, d);

    float e = clamp(vEnergy, 0.0, 1.0);
    vec3 col = mix(magma(e * 1.12), magma(0.30 + e * 0.44), vOrb);

    // Aerial perspective: distant points wash out into the paper.
    float fog = smoothstep(11.0, 30.0, vDepth);
    col = mix(col, vec3(1.0), fog * 0.94);

    // Quiet regions stay barely-there grain; loud regions are fully opaque.
    float alpha = mix(mix(0.05, 0.03, vOrb), mix(0.72, 1.0, vOrb),
                      pow(e, mix(0.66, 0.85, vOrb)));
    alpha *= mask * uFade * vShow * (1.0 - fog * 0.88) * (0.7 + 0.3 * vJitter);

    if (alpha < 0.004) discard;
    gl_FragColor = vec4(col, alpha);
  }
`;
