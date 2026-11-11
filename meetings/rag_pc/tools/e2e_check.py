"""End-to-end check of a running bench over HTTPS + WebSocket, the way the page does it.

  python tools/e2e_check.py https://127.0.0.1:8791 --audio test.wav [--moments MODEL] [--live-seconds N]

The WAV must be 16 kHz stereo s16 (L = you, R = meeting). Needs aiohttp (the bench venv has it).
Creates two sessions (live + upload) and deletes the uploaded one at the end.
Since 0.3.0 this is the stage-0 test bench: switch on «🧪 Тестові записи без Minerva» (standalone_enabled) first,
otherwise every request here gets 403 «Тестові записи без Minerva вимкнено».
"""
import argparse
import asyncio
import json
import time
import wave

import aiohttp

p = argparse.ArgumentParser()
p.add_argument("base")
p.add_argument("--moments", default="")
p.add_argument("--live-seconds", type=float, default=28)
p.add_argument("--audio", required=True)
args = p.parse_args()
BASE = args.base.rstrip("/")
H = {"Origin": BASE, "X-Bench": "1"}


async def wait_job(s, sid, what, limit=900):
    t = time.time()
    while time.time() - t < limit:
        async with s.get(f"{BASE}/api/sessions/{sid}", ssl=False) as r:
            d = await r.json()
        j = d.get("job") or {}
        if j.get("state") in ("done", "error"):
            print(f"  {what}: {j['state']} after {time.time() - t:.1f} s — {j.get('text')}")
            return d
        await asyncio.sleep(1.5)
    raise TimeoutError(what)


async def live(s):
    with wave.open(args.audio) as w:
        pcm = w.readframes(w.getnframes())
    frame = 1600 * 4  # 100 ms of 16 kHz stereo s16
    msgs = []
    async with s.ws_connect(f"{BASE.replace('https', 'wss')}/ws", ssl=False, headers={"Origin": BASE}) as ws:
        await ws.send_json({"type": "start", "title": "API test live", "mode": "both", "engine": "parakeet",
                            "lang_l": "en", "lang_r": "de", "consent": True})

        async def reader():
            async for m in ws:
                if m.type == aiohttp.WSMsgType.TEXT:
                    d = json.loads(m.data)
                    msgs.append((time.time(), d))
                    if d["type"] == "saved":
                        return

        rt = asyncio.create_task(reader())
        while not any(d["type"] == "ready" for _, d in msgs):
            await asyncio.sleep(0.1)
            if any(d["type"] == "error" for _, d in msgs):
                print("  live error:", msgs)
                return None
        start = time.time()
        limit = int(args.live_seconds * 10)
        for i, off in enumerate(range(0, len(pcm), frame)):
            if i >= limit:
                break
            await ws.send_bytes(pcm[off:off + frame])
            if i == 50:
                await ws.send_json({"type": "mark", "note": "test mark"})
            await asyncio.sleep(max(0, start + (i + 1) * 0.1 - time.time()))
        await ws.send_json({"type": "stop"})
        await asyncio.wait_for(rt, timeout=120)
    sid = None
    for t, d in msgs:
        if d["type"] in ("final", "partial"):
            print(f"  +{t - start:5.1f}s {d['type']:7} {d['ch']} [{d.get('t0')}] {d['text']}")
        elif d["type"] == "stats":
            last = d
        elif d["type"] in ("ready", "saved", "marked", "status", "error"):
            print(f"  {d}")
            sid = d.get("id", sid)
    print("  last stats:", last)
    return sid


async def main():
    async with aiohttp.ClientSession(headers=H) as s:
        print("1) live capture over WebSocket")
        live_sid = await live(s)

        print("2) upload + final transcript (split stereo)")
        form = aiohttp.FormData()
        for k, v in {"title": "API test upload", "lang": "auto", "split": "1", "model": "accurate",
                     "hotwords": "Teillieferung", "consent": "1", "transcribe": "1"}.items():
            form.add_field(k, v)
        form.add_field("file", open(args.audio, "rb"), filename="meeting.wav", content_type="audio/wav")
        async with s.post(f"{BASE}/api/upload", data=form, ssl=False) as r:
            up = await r.json()
            print("  upload:", r.status, up)
        d = await wait_job(s, up["id"], "transcribe")
        for seg in (d.get("transcript") or {}).get("segments", []):
            print(f"   {seg['ch']} {seg['start']:6.2f} {seg['text']}")

        if args.moments:
            print("3) key moments with", args.moments)
            async with s.post(f"{BASE}/api/sessions/{up['id']}/moments", json={"model": args.moments, "lang": "uk"}, ssl=False) as r:
                print("  ", r.status, await r.json())
            d = await wait_job(s, up["id"], "moments")
            mo = d.get("moments") or {}
            print("   metrics:", mo.get("metrics"))
            for it in mo.get("items", []):
                print(f"   {it['kind']:9} {it['start']:5.1f}s {it['text']} «{it['quote']}»")
            print("   rejected:", mo.get("rejected"))

        if live_sid:
            print("4) final transcript of the live recording")
            async with s.post(f"{BASE}/api/sessions/{live_sid}/transcribe", json={"model": "fast"}, ssl=False) as r:
                print("  ", r.status)
            d = await wait_job(s, live_sid, "transcribe live")
            print("   segments:", len((d.get("transcript") or {}).get("segments", [])), "marks:", d.get("marks"))

        print("5) downloads and guards")
        for path in (f"/api/sessions/{up['id']}/export.txt", "/api/metrics.csv", f"/api/sessions/{up['id']}/audio",
                     "/api/sessions/..%2F..%2Fdata/audio", "/api/sessions/20260101-000000-zzzz"):
            async with s.get(BASE + path, ssl=False) as r:
                body = await r.read()
                print(f"   GET {path} -> {r.status} {r.headers.get('Content-Type')} {len(body)} bytes")
        async with s.post(f"{BASE}/api/sessions/{up['id']}/delete", json={}, ssl=False) as r:
            print("   delete:", r.status, await r.json())


asyncio.run(main())
