"""Meridian Reel worker.

Runs on a schedule (GitHub Actions, every 15 minutes). Each run advances every
open job in public.reel_jobs (Supabase) by one step:

  queued      -> submit text-to-video to Runway (Kling 3.0 Pro by default)  -> generating
  generating  -> poll; on success download, trim any model-added hard cut,
                 burn in overlays, loudness-normalise, upload to Supabase   -> ready
                 (or rejected if automated frame QC fails; on a provider
                 failure: retry once, then failed)

Publishing is NOT done here: a Claude scheduled task publishes 'ready' jobs to
Instagram via Windsor.ai and marks them 'published'.

Environment variables (repository secrets, never committed):
  SUPABASE_URL            e.g. https://vnoblhczrxfdtumqnkpt.supabase.co
  SUPABASE_SERVICE_KEY    service-role key (server-side only)
  RUNWAYML_API_SECRET     Runway developer API secret (dev.runwayml.com)
  ANTHROPIC_API_KEY       optional: enables automated frame QC with Claude vision
  STORAGE_BUCKET          default: social-media
  MAX_CREDITS_PER_JOB     default: 400 (safety cap, Runway credits)
"""

from __future__ import annotations

import base64
import json
import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path

import requests

SUPABASE_URL = os.environ["SUPABASE_URL"].rstrip("/")
SERVICE_KEY = os.environ["SUPABASE_SERVICE_KEY"]
RUNWAY_KEY = os.environ["RUNWAYML_API_SECRET"]
ANTHROPIC_KEY = os.environ.get("ANTHROPIC_API_KEY", "")
BUCKET = os.environ.get("STORAGE_BUCKET", "social-media")
MAX_CREDITS = int(os.environ.get("MAX_CREDITS_PER_JOB", "400"))

RUNWAY = "https://api.dev.runwayml.com/v1"
# reel_jobs.model value -> (Runway model id, credits per second at the ratio used, output ratio)
# Seedance 2.0's provider moderation blocked real-destination landscapes (SAFETY.OUTPUT.THIRD_PARTY),
# so Kling 3.0 Pro is the default for new jobs.
RUNWAY_MODELS = {
    "kling-3.0-pro": ("kling3.0_pro", 12, "1080:1920"),
    "seedance-2.0": ("seedance2", 40, "1080:1920"),
    "seedance-2.0-fast": ("seedance2_fast", 24, "720:1280"),
}
FONTS = Path(__file__).parent / "fonts"
EXTRABOLD = FONTS / "Montserrat-ExtraBold.ttf"
SEMIBOLD = FONTS / "Montserrat-SemiBold.ttf"

DB_HEADERS = {
    "apikey": SERVICE_KEY,
    "Authorization": f"Bearer {SERVICE_KEY}",
    "Content-Type": "application/json",
}


def log(msg: str) -> None:
    print(time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()), msg, flush=True)


# ---------------------------------------------------------------- database
def jobs(status: str) -> list[dict]:
    r = requests.get(
        f"{SUPABASE_URL}/rest/v1/reel_jobs",
        headers=DB_HEADERS,
        params={"status": f"eq.{status}", "order": "created_at.asc"},
        timeout=30,
    )
    r.raise_for_status()
    return r.json()


def update(job_id: str, **fields) -> None:
    fields["updated_at"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    r = requests.patch(
        f"{SUPABASE_URL}/rest/v1/reel_jobs",
        headers=DB_HEADERS,
        params={"id": f"eq.{job_id}"},
        data=json.dumps(fields),
        timeout=30,
    )
    r.raise_for_status()


def upload(path: Path, name: str, content_type: str) -> str:
    with open(path, "rb") as fh:
        r = requests.post(
            f"{SUPABASE_URL}/storage/v1/object/{BUCKET}/{name}",
            headers={
                "apikey": SERVICE_KEY,
                "Authorization": f"Bearer {SERVICE_KEY}",
                "Content-Type": content_type,
                "x-upsert": "true",
            },
            data=fh,
            timeout=300,
        )
    r.raise_for_status()
    return f"{SUPABASE_URL}/storage/v1/object/public/{BUCKET}/{name}"


# ---------------------------------------------------------------- runway
def rw_headers() -> dict:
    return {
        "Authorization": f"Bearer {RUNWAY_KEY}",
        "X-Runway-Version": "2024-11-06",
        "Content-Type": "application/json",
    }


def submit(job: dict) -> None:
    if job["model"] not in RUNWAY_MODELS:
        update(job["id"], status="failed", error=f"unknown model {job['model']!r} (expected one of {sorted(RUNWAY_MODELS)})")
        return
    model, rate, ratio = RUNWAY_MODELS[job["model"]]
    est = rate * job["duration_s"]
    if est > MAX_CREDITS:
        update(job["id"], status="failed", error=f"estimated {est} credits exceeds cap {MAX_CREDITS}")
        return
    body = {
        "model": model,
        "promptText": job["video_prompt"][:2500 if model.startswith("kling") else 3500],
        "duration": job["duration_s"],
        "ratio": ratio,
        "audio": True,
    }
    r = requests.post(f"{RUNWAY}/text_to_video", headers=rw_headers(), json=body, timeout=60)
    if r.status_code >= 400:
        detail = r.text[:500]
        if "credit" in detail.lower():
            update(job["id"], status="failed", error=f"Runway: insufficient credits ({detail})")
            return
        if r.status_code in (400, 401):
            update(job["id"], status="failed", error=f"Runway HTTP {r.status_code}: {detail}")
            return
        r.raise_for_status()
    data = r.json()
    update(
        job["id"],
        status="generating",
        provider_task_id=data["id"],
        credits_charged=est,
        attempts=job["attempts"] + 1,
        error=None,
    )
    log(f"{job['id']}: submitted Runway task {data['id']} (~{est} credits)")


def poll(job: dict) -> None:
    r = requests.get(f"{RUNWAY}/tasks/{job['provider_task_id']}", headers=rw_headers(), timeout=60)
    r.raise_for_status()
    data = r.json()
    status = data.get("status")
    if status in ("PENDING", "THROTTLED", "RUNNING"):
        log(f"{job['id']}: still {status}")
        return
    if status != "SUCCEEDED" or not data.get("output"):
        err = f"Runway task {status}: {data.get('failureCode')} {data.get('failure') or ''}".strip()
        # Content moderation is not worth retrying with the same prompt.
        moderated = "SAFETY" in str(data.get("failureCode", "")).upper()
        if job["attempts"] < 2 and not moderated:
            update(job["id"], status="queued", provider_task_id=None, error=err)
        else:
            update(job["id"], status="failed", error=err)
        log(f"{job['id']}: {err}")
        return
    video_url = data["output"][0]
    update(job["id"], status="rendering", raw_url=video_url)
    render(job | {"raw_url": video_url})


# ---------------------------------------------------------------- rendering
def run(cmd: list[str]) -> str:
    p = subprocess.run(cmd, capture_output=True, text=True)
    if p.returncode != 0:
        raise RuntimeError(f"{cmd[0]} failed: {p.stderr[-800:]}")
    return p.stdout + p.stderr


def probe_duration(path: Path) -> float:
    out = run(["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0", str(path)])
    return float(out.strip().splitlines()[0])


def first_hard_cut(path: Path, after: float = 1.0) -> float | None:
    """Return the time of the first hard cut (scene score > 0.25) after `after` seconds, if any."""
    out = run(["ffmpeg", "-hide_banner", "-i", str(path), "-vf", "select='gt(scene,0.25)',showinfo", "-f", "null", "-"])
    for line in out.splitlines():
        if "pts_time:" in line:
            t = float(line.split("pts_time:")[1].split()[0])
            if t > after:
                return t
    return None


def esc(text: str) -> str:
    # Overlay text is written to files and read with textfile=, so no shell/filter escaping is needed.
    return text.replace("'", "’")  # apostrophes are typographic only


def overlay_filter(job: dict, workdir: Path, end: float) -> str:
    """Upscale to 1080x1920, then draw the overlays directly on the footage.

    House rules: no background boxes (legibility comes from outline + shadow
    only), and only smooth fades in and out.
    """
    lines = [
        ("t1.txt", job["overlay_title"].upper(), EXTRABOLD, 104, 300, "title"),
        ("t2.txt", job["overlay_subtitle"].upper(), SEMIBOLD, 54, 430, "title"),
        ("t3.txt", job["overlay_hook_1"], SEMIBOLD, 52, 540, "hook"),
    ]
    if job.get("overlay_hook_2"):
        lines.append(("t4.txt", job["overlay_hook_2"], SEMIBOLD, 52, 610, "hook"))
    fade_out_start, fade_out_end = end - 0.75, end - 0.25

    def alpha(start: float) -> str:
        return (
            f"if(lt(t\\,{start})\\,0\\,if(lt(t\\,{start + 0.5})\\,(t-{start})/0.5\\,"
            f"if(lt(t\\,{fade_out_start})\\,1\\,if(lt(t\\,{fade_out_end})\\,({fade_out_end}-t)/0.5\\,0))))"
        )

    parts = []
    for fname, text, font, size, y, kind in lines:
        (workdir / fname).write_text(esc(text), encoding="utf-8")
        border = "borderw=3:bordercolor=black@0.85:shadowx=5:shadowy=5" if kind == "title" and size > 60 else \
                 "borderw=2:bordercolor=black@0.6:shadowx=4:shadowy=4"
        start = 0.5 if kind == "title" else 0.9
        parts.append(
            f"drawtext=fontfile={font}:textfile={workdir / fname}:fontsize={size}:fontcolor=white:"
            f"{border}:shadowcolor=black@0.7:x=(w-text_w)/2:y={y}:alpha='{alpha(start)}'"
        )
    # Source (1080p or 720p) -> 1080x1920 with high-quality scaling, then text on top (text renders at full resolution).
    return "scale=1080:1920:flags=lanczos," + ",".join(parts)


def render(job: dict) -> None:
    with tempfile.TemporaryDirectory() as tmp:
        work = Path(tmp)
        raw = work / "raw.mp4"
        with requests.get(job["raw_url"], stream=True, timeout=300) as r:
            r.raise_for_status()
            with open(raw, "wb") as fh:
                for chunk in r.iter_content(1 << 20):
                    fh.write(chunk)

        dur = probe_duration(raw)
        cut = first_hard_cut(raw)
        end = min(dur, cut - 0.04) if cut else dur
        notes = [f"source {dur:.2f}s"] + ([f"trimmed model-added cut at {cut:.2f}s"] if cut else [])
        if end < 8:
            raise RuntimeError(f"usable continuous footage only {end:.1f}s (cut at {cut})")

        final = work / "final.mp4"
        vf = overlay_filter(job, work, end)
        af = (f"afade=t=in:st=0:d=0.3,afade=t=out:st={end - 0.5:.2f}:d=0.5,"
              "loudnorm=I=-16:TP=-1.5:LRA=11")
        run([
            "ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-i", str(raw), "-t", f"{end:.3f}",
            "-vf", vf, "-af", af,
            "-c:v", "libx264", "-preset", "slow", "-crf", "20", "-maxrate", "8M", "-bufsize", "16M",
            "-pix_fmt", "yuv420p", "-c:a", "aac", "-b:a", "160k", "-ar", "48000",
            "-movflags", "+faststart", str(final),
        ])

        # Silence check
        sil = run(["ffmpeg", "-hide_banner", "-i", str(final), "-af", "silencedetect=n=-45dB:d=1.5", "-vn", "-f", "null", "-"])
        if "silence_start" in sil:
            notes.append("WARNING: long silence detected")

        # QC strip: 6 frames across the clip
        strip = work / "qc.jpg"
        times = [end * f for f in (0.08, 0.25, 0.45, 0.6, 0.8, 0.95)]
        frames = []
        for i, t in enumerate(times):
            f = work / f"f{i}.jpg"
            run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-ss", f"{t:.2f}", "-i", str(final),
                 "-frames:v", "1", "-vf", "scale=360:-1", str(f)])
            frames.append(str(f))
        run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-y"] + sum([["-i", f] for f in frames], []) +
            ["-filter_complex", f"hstack=inputs={len(frames)}", str(strip)])

        slug = job["article_slug"]
        day = job.get("publish_on") or time.strftime("%Y-%m-%d", time.gmtime())
        final_name = f"reels/{day}-{slug}.mp4"
        final_url = upload(final, final_name, "video/mp4")
        strip_url = upload(strip, f"reels/{day}-{slug}-qc.jpg", "image/jpeg")

        qc = vision_qc(strip, job) if ANTHROPIC_KEY else None
        if qc is not None:
            notes.append(f"vision QC: {qc['verdict']} - {qc['notes']}")
        status = "ready" if qc is None or qc["verdict"] == "pass" else "rejected"

        update(job["id"], status=status, final_path=final_name, final_url=final_url,
               qc_strip_url=strip_url, qc_notes="; ".join(notes), error=None)
        log(f"{job['id']}: {status} -> {final_url}")


# ---------------------------------------------------------------- optional vision QC
def vision_qc(strip: Path, job: dict) -> dict:
    img = base64.b64encode(strip.read_bytes()).decode()
    prompt = (
        "These are 6 frames, left to right, from a travel Reel about "
        f"{job['destination']}. Check strictly for: extra or malformed limbs or anatomy, "
        "out-of-place or melted objects, garbled AI text or logos/watermarks in the footage, "
        "the on-screen overlay text being misspelled, clipped or illegible, and anything that "
        "does not look like real footage of the destination. Overlay text should read: "
        f"'{job['overlay_title']}', '{job['overlay_subtitle']}', '{job['overlay_hook_1']}'"
        + (f", '{job['overlay_hook_2']}'" if job.get("overlay_hook_2") else "")
        + '. Reply with JSON only: {"verdict":"pass"|"fail","notes":"<one sentence>"}'
    )
    r = requests.post(
        "https://api.anthropic.com/v1/messages",
        headers={"x-api-key": ANTHROPIC_KEY, "anthropic-version": "2023-06-01", "content-type": "application/json"},
        json={
            "model": os.environ.get("QC_MODEL", "claude-sonnet-5-5"),
            "max_tokens": 200,
            "messages": [{"role": "user", "content": [
                {"type": "image", "source": {"type": "base64", "media_type": "image/jpeg", "data": img}},
                {"type": "text", "text": prompt},
            ]}],
        },
        timeout=120,
    )
    r.raise_for_status()
    text = "".join(b.get("text", "") for b in r.json()["content"])
    try:
        return json.loads(text[text.index("{"): text.rindex("}") + 1])
    except ValueError:
        return {"verdict": "fail", "notes": f"unparseable QC reply: {text[:200]}"}


# ---------------------------------------------------------------- main
def main() -> int:
    errors = 0
    for job in jobs("generating"):
        try:
            poll(job)
        except Exception as e:  # noqa: BLE001
            errors += 1
            update(job["id"], status="failed", error=str(e)[:900])
            log(f"{job['id']}: error {e}")
    for job in jobs("queued"):
        try:
            submit(job)
        except Exception as e:  # noqa: BLE001
            errors += 1
            update(job["id"], error=str(e)[:900])
            log(f"{job['id']}: submit error {e}")
    return 1 if errors else 0


if __name__ == "__main__":
    sys.exit(main())
