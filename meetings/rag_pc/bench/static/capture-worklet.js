// AudioWorklet: 2 input channels (L = microphone, R = meeting) -> 16 kHz Int16 interleaved, 100 ms per message.
// If the AudioContext does not run at 16 kHz, samples are averaged down (box filter), which is enough for speech.
// Rounded (not truncated) to Int16; an empty input block is recorded as silence so the timeline never shrinks.
class CaptureProcessor extends AudioWorkletProcessor {
  constructor() {
    super();
    this.ratio = sampleRate / 16000;
    this.frame = 1600;
    this.out = new Int16Array(this.frame * 2);
    this.n = 0;
    this.accL = 0; this.accR = 0; this.cnt = 0; this.phase = 0;
    this.peakL = 0; this.peakR = 0;
    this.empty = 0;
    this.running = true;
    this.zeros = new Float32Array(128);
    this.port.onmessage = (e) => { if (e.data === 'stop') this.running = false; };
  }

  push(l, r) {
    const al = Math.abs(l), ar = Math.abs(r);
    if (al > this.peakL) this.peakL = al;
    if (ar > this.peakR) this.peakR = ar;
    const i = this.n * 2;
    this.out[i] = Math.round(Math.max(-1, Math.min(1, l)) * 32767);
    this.out[i + 1] = Math.round(Math.max(-1, Math.min(1, r)) * 32767);
    this.n++;
    if (this.n === this.frame) {
      const buf = this.out.buffer.slice(0);
      this.port.postMessage({pcm: buf, levels: [this.peakL, this.peakR], empty: this.empty}, [buf]);
      this.n = 0; this.peakL = 0; this.peakR = 0; this.empty = 0;
    }
  }

  process(inputs) {
    if (!this.running) return false;
    const input = inputs[0];
    let L, R;
    if (!input || !input.length) {
      this.empty++;
      L = R = this.zeros;
    } else {
      L = input[0];
      R = input[1] || input[0];
    }
    if (this.ratio === 1) {
      for (let i = 0; i < L.length; i++) this.push(L[i], R[i]);
      return true;
    }
    for (let i = 0; i < L.length; i++) {
      this.accL += L[i]; this.accR += R[i]; this.cnt++; this.phase += 1;
      if (this.phase >= this.ratio) {
        this.push(this.accL / this.cnt, this.accR / this.cnt);
        this.accL = 0; this.accR = 0; this.cnt = 0; this.phase -= this.ratio;
      }
    }
    return true;
  }
}
registerProcessor('capture', CaptureProcessor);
