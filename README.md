# meridian-reels-worker

Turns queued Meridian Explorer Reel jobs into finished, hosted MP4s. Runs free on GitHub Actions every 15 minutes (`.github/workflows/worker.yml`).

**Flow:** a Claude scheduled task writes a row to `public.reel_jobs` (Supabase) with the video prompt, overlay text and caption → this worker submits it to ClipDance (Seedance 2.0), downloads the clip, trims any model-added hard cut, upscales to 1080×1920, burns in the Montserrat overlays (no boxes, outline and shadow only, smooth fades only), loudness-normalises, uploads to the public `social-media` bucket, runs an automated frame check, and marks the job `ready` (or `rejected`) → a Claude scheduled task publishes `ready` Reels to Instagram via Windsor.ai and logs them in `public.social_posts`.

**Secrets** (repo Settings → Secrets and variables → Actions; never commit them):

| Secret | Where to get it |
| --- | --- |
| `CLIPDANCE_API_KEY` | ClipDance → API Keys → create a key named "GitHub worker" |
| `SUPABASE_SERVICE_KEY` | Supabase → project settings → API keys → `service_role` |
| `ANTHROPIC_API_KEY` | console.anthropic.com → API keys (enables the automated frame check) |

Without `ANTHROPIC_API_KEY` every finished Reel is marked `ready` with no frame check.

Safety cap: `MAX_CREDITS_PER_JOB` (default 200) stops any job estimated above that many ClipDance credits.

Fonts: Montserrat (SIL Open Font License, see `fonts/OFL.txt`).
