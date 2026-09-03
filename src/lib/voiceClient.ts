/**
 * Live voice client for the Gemini bridge in `backend/`.
 *
 * Wire format, taken from `voice_pipeline` in test_realtime_gemini.py:
 *
 *   → first message   {type:"config", scenario, language, ...}
 *   → then            binary PCM16 mono @ 24 kHz, as the mic produces it
 *   ← {type:"audio"}       base64 PCM16 mono @ 24 kHz to play
 *   ← {type:"transcript"}  role "user" | "assistant"
 *   ← {type:"status"}      "listening" | "speaking"
 *   ← {type:"clear"}       barge-in: drop everything still queued
 *   ← {type:"rate"}        playback rate hint
 *   ← {type:"error"}
 *
 * Both directions are 24 kHz here. Gemini's own output is 24 kHz, so what it
 * speaks reaches the browser untouched; the uplink is downsampled to 16 kHz on
 * the bridge, which is what the Live API accepts.
 *
 * Hearing the agent and being heard are separate concerns here: the call
 * connects and plays audio first, then tries for the microphone. A blocked mic
 * degrades to listen-only rather than killing the call.
 */

const SAMPLE_RATE = 24000;
/** ~40 ms of audio per frame. Small enough to keep barge-in responsive. */
const FRAME_SAMPLES = 960;

export type VoiceEvents = {
  onTranscript: (role: "user" | "assistant", text: string) => void;
  onStatus: (status: "listening" | "speaking") => void;
  /** Playback amplitude 0..1, sampled per frame — drives the orb. */
  onLevel: (level: number) => void;
  onError: (message: string) => void;
  onClose: () => void;
};

export type VoiceConfig = {
  url: string;
  scenario: string;
  /** Backend value: english | hindi | tamil | telugu */
  language: string;
  /** Backend value, where the agent branches on it. */
  accent?: "indian" | "american";
};

/** Worklet that ships mono Float32 mic blocks to the main thread. */
const CAPTURE_WORKLET = `
class Capture extends AudioWorkletProcessor {
  process(inputs) {
    const ch = inputs[0] && inputs[0][0];
    if (ch) this.port.postMessage(ch.slice(0));
    return true;
  }
}
registerProcessor('appello-capture', Capture);
`;

function floatToPcm16(input: Float32Array): ArrayBuffer {
  const out = new Int16Array(input.length);
  for (let i = 0; i < input.length; i++) {
    const s = Math.max(-1, Math.min(1, input[i]));
    out[i] = s < 0 ? s * 0x8000 : s * 0x7fff;
  }
  return out.buffer;
}

function pcm16ToFloat(buf: ArrayBufferLike): Float32Array<ArrayBuffer> {
  const view = new Int16Array(buf);
  const out = new Float32Array(new ArrayBuffer(view.length * 4));
  for (let i = 0; i < view.length; i++) out[i] = view[i] / 0x8000;
  return out;
}

export class VoiceCall {
  private ws: WebSocket | null = null;
  private ctx: AudioContext | null = null;
  private mic: MediaStream | null = null;
  private node: AudioWorkletNode | null = null;
  private source: MediaStreamAudioSourceNode | null = null;

  /** Wall-clock position the next chunk of agent audio should start at. */
  private playhead = 0;
  private scheduled = new Set<AudioBufferSourceNode>();
  private rate = 1;
  private pending: Float32Array[] = [];
  private closed = false;

  /** Set once the microphone is actually feeding the socket. */
  micLive = false;
  /** Chunks of agent audio played — used to verify the path end to end. */
  chunksPlayed = 0;

  constructor(
    private cfg: VoiceConfig,
    private ev: VoiceEvents,
  ) {}

  /**
   * Connect and start playing. Resolves once the socket is open and the audio
   * graph is running — before the microphone is requested, so a denied prompt
   * cannot stop you hearing the agent.
   */
  async connect() {
    this.ctx = new AudioContext({ sampleRate: SAMPLE_RATE });
    // Called from a click handler, so this is allowed; without it Chrome
    // leaves the context suspended and nothing is ever audible.
    await this.ctx.resume();
    await this.openSocket();
  }

  /** Ask for the microphone. Safe to fail — the call carries on listen-only. */
  async enableMic() {
    if (!this.ctx) throw new Error("connect() first");

    this.mic = await navigator.mediaDevices.getUserMedia({
      audio: {
        channelCount: 1,
        echoCancellation: true,
        noiseSuppression: true,
        autoGainControl: true,
      },
    });

    const blob = new Blob([CAPTURE_WORKLET], {
      type: "application/javascript",
    });
    const url = URL.createObjectURL(blob);
    await this.ctx.audioWorklet.addModule(url);
    URL.revokeObjectURL(url);

    this.source = this.ctx.createMediaStreamSource(this.mic);
    this.node = new AudioWorkletNode(this.ctx, "appello-capture");
    this.node.port.onmessage = (e) => this.onMicBlock(e.data as Float32Array);
    this.source.connect(this.node);
    // A worklet with no destination is not pulled in some browsers. Route it
    // into a silent gain so the graph keeps running without echoing the mic.
    const mute = this.ctx.createGain();
    mute.gain.value = 0;
    this.node.connect(mute).connect(this.ctx.destination);
    this.micLive = true;
  }

  private openSocket() {
    return new Promise<void>((resolve, reject) => {
      let settled = false;
      const ws = new WebSocket(this.cfg.url);
      ws.binaryType = "arraybuffer";
      this.ws = ws;

      // A refused connection fires error then close with no useful detail, so
      // the timeout is what turns "hanging" into a real failure. It is generous
      // because the hosted bridge is an App Service container that may be cold:
      // a first handshake of several seconds is normal and should not be
      // reported to the caller as an outage.
      const guard = setTimeout(() => {
        if (!settled) {
          settled = true;
          try {
            ws.close();
          } catch {
            /* already closing */
          }
          reject(new Error("The voice bridge did not respond"));
        }
      }, 15000);

      ws.onopen = () => {
        clearTimeout(guard);
        ws.send(
          JSON.stringify({
            type: "config",
            scenario: this.cfg.scenario,
            language: this.cfg.language,
            ...(this.cfg.accent ? { accent: this.cfg.accent } : {}),
          }),
        );
        settled = true;
        resolve();
      };
      ws.onerror = () => {
        clearTimeout(guard);
        if (!settled) {
          settled = true;
          reject(new Error("Could not reach the voice bridge"));
        }
      };
      ws.onclose = () => {
        clearTimeout(guard);
        if (settled && !this.closed) this.ev.onClose();
      };
      ws.onmessage = (e) => this.onServerMessage(e);
    });
  }

  private onServerMessage(e: MessageEvent) {
    if (typeof e.data !== "string") return;
    let msg: Record<string, unknown>;
    try {
      msg = JSON.parse(e.data);
    } catch {
      return;
    }

    switch (msg.type) {
      case "audio":
        this.enqueue(msg.data as string);
        break;
      case "transcript":
        this.ev.onTranscript(
          msg.role as "user" | "assistant",
          msg.text as string,
        );
        break;
      case "status":
        this.ev.onStatus(msg.status as "listening" | "speaking");
        break;
      case "clear":
        this.flush();
        break;
      case "rate":
        this.rate = Number(msg.value) || 1;
        break;
      case "error":
        this.ev.onError(String(msg.message ?? "Bridge error"));
        break;
    }
  }

  private onMicBlock(block: Float32Array) {
    if (!this.ws || this.ws.readyState !== WebSocket.OPEN) return;

    // The worklet hands over 128-sample blocks; batch them so we are not
    // sending hundreds of tiny frames a second.
    this.pending.push(block);
    let total = this.pending.reduce((n, b) => n + b.length, 0);
    while (total >= FRAME_SAMPLES) {
      const frame = new Float32Array(FRAME_SAMPLES);
      let filled = 0;
      while (filled < FRAME_SAMPLES) {
        const head = this.pending[0];
        const take = Math.min(head.length, FRAME_SAMPLES - filled);
        frame.set(head.subarray(0, take), filled);
        filled += take;
        if (take === head.length) this.pending.shift();
        else this.pending[0] = head.subarray(take);
      }
      this.ws.send(floatToPcm16(frame));
      total -= FRAME_SAMPLES;
    }
  }

  /** Schedule one chunk of agent audio back-to-back with whatever precedes it. */
  private enqueue(b64: string) {
    const ctx = this.ctx;
    if (!ctx) return;

    const bin = atob(b64);
    const bytes = new Uint8Array(bin.length);
    for (let i = 0; i < bin.length; i++) bytes[i] = bin.charCodeAt(i);
    const samples = pcm16ToFloat(bytes.buffer);
    if (!samples.length) return;

    const buffer = ctx.createBuffer(1, samples.length, SAMPLE_RATE);
    buffer.copyToChannel(samples, 0);

    const src = ctx.createBufferSource();
    src.buffer = buffer;
    src.playbackRate.value = this.rate;
    src.connect(ctx.destination);

    // A small lead keeps chunks from under-running into each other on a
    // jittery connection.
    const now = ctx.currentTime;
    if (this.playhead < now) this.playhead = now + 0.06;
    src.start(this.playhead);
    this.playhead += buffer.duration / this.rate;

    this.scheduled.add(src);
    src.onended = () => this.scheduled.delete(src);
    this.chunksPlayed++;

    // Peak of this chunk, used as the orb's loudness for its duration.
    let peak = 0;
    for (let i = 0; i < samples.length; i += 16) {
      const a = Math.abs(samples[i]);
      if (a > peak) peak = a;
    }
    this.ev.onLevel(Math.min(1, peak * 1.6));
  }

  /** Barge-in: the agent was interrupted, so drop audio it has not played yet. */
  private flush() {
    for (const src of this.scheduled) {
      try {
        src.stop();
      } catch {
        // Already finished; nothing to stop.
      }
    }
    this.scheduled.clear();
    this.playhead = 0;
    this.ev.onLevel(0);
  }

  async stop() {
    this.closed = true;
    this.flush();
    this.node?.port.close();
    this.node?.disconnect();
    this.source?.disconnect();
    this.mic?.getTracks().forEach((t) => t.stop());
    try {
      this.ws?.close();
    } catch {
      // Already closing.
    }
    await this.ctx?.close();
    this.ws = null;
    this.ctx = null;
    this.mic = null;
    this.node = null;
    this.source = null;
  }
}

/**
 * Where the bridge lives.
 *
 * The deployed bridge runs `main.py`, which is the only process Azure App
 * Service exposes; it mounts the Gemini Live pipeline at /ws/voice-gemini and
 * delegates straight into test_realtime_gemini.voice_pipeline. Running
 * test_realtime_gemini.py on its own serves the same handler at /ws/voice, so
 * both paths are tried before the panel gives up.
 *
 * NEXT_PUBLIC_VOICE_BRIDGE_URL overrides the origin — point it at
 * ws://localhost:8000 to develop against a bridge on this machine. Unset, the
 * page uses the hosted one, so a Vercel deploy needs no configuration.
 */
const HOSTED_BRIDGE =
  "wss://voicera-bridge-dke5c6b4c6fba3e5.swedencentral-01.azurewebsites.net";

function bridgeOrigin(): string {
  const configured = process.env.NEXT_PUBLIC_VOICE_BRIDGE_URL;
  return configured ? configured.replace(/\/+$/, "") : HOSTED_BRIDGE;
}

/** Set by checkBridge once it knows which of the two paths this bridge serves. */
let voicePath = "/ws/voice-gemini";

export function bridgeUrl(): string | null {
  return `${bridgeOrigin()}${voicePath}`;
}

/**
 * Is the bridge up? Decides what the panel offers.
 *
 * main.py answers {status:"ok"}; test_realtime_gemini.py run directly answers
 * {status:"healthy", has_api_key}. Only the second can vouch for the Gemini key,
 * so the first is taken at its word — and the reply is also what selects the
 * websocket path, since the two processes mount the pipeline differently.
 */
export async function checkBridge(): Promise<boolean> {
  const url = `${bridgeOrigin().replace(/^ws/, "http")}/health`;
  try {
    // Same reasoning as the socket guard: a cold container answers slowly.
    const res = await fetch(url, { signal: AbortSignal.timeout(10000) });
    if (!res.ok) return false;
    const body = await res.json();
    if (body?.service === "gemini-live-bridge") {
      voicePath = "/ws/voice";
      return Boolean(body.has_api_key);
    }
    voicePath = "/ws/voice-gemini";
    return body?.status === "ok" || body?.status === "healthy";
  } catch {
    return false;
  }
}
