# Clipper: notes for Claude

Owner: Sudhi. Goal: earn from Whop Content Rewards (paid per 1,000 views) by posting
clips of big streamers, especially GTA 6 streams (GTA 6 launches 19 Nov 2026).
Target: about 10 shorts/day once new accounts have warmed up.

## Environment
- Contabo VPS, Ubuntu 24.04, 4 vCPU, 8 GB RAM, no GPU, 4 GB swap. Repo at ~/clipper, venv at ~/clipper/venv.
- Sudhi works from his phone (Termux over SSH). Keep commands short and copy-pasteable.
  Explain steps plainly; he likes direct, practical guidance.
- config.yaml is local (gitignored); config.example.yaml is the template.
- The Curious Archive (a separate faceless-shorts pipeline) also runs on this box. Don't touch it.

## How it works
run.py check|once|process URL|clips URL|loop
1. monitor.py: lists VODs (Twitch/YouTube via yt-dlp flat, Kick via kick.com/api/v2 with curl_cffi).
2. pipeline.py: downloads audio only, then highlights.find(), downloads only the chosen sections,
   aligns each section to the VOD audio (HLS cuts land on segment boundaries), renders with edit.py.
3. highlights.py: loudness spikes plus viewer-clip hints (signals.py); Whisper (faster-whisper small,
   int8, CPU) on candidate windows only. Audio goes to Whisper as numpy, never through PyAV (av.open broke).
4. edit.py: 1080x1920, blurred background plus zoomed gameplay, ASS word captions, streamer credit.
- systemd timer `clipper.timer` (every 30 min) is installed but NOT enabled yet.

## Known facts / gotchas
- YouTube blocks this server IP ("confirm you're not a bot"). Needs cookies.txt (run.cookies_file). IShowSpeed is disabled until then.
- Kick clips API: clips have livestream_id; vod_starts_at is often 0 (=unknown), so offsets come from created_at - duration - stream start.
  Kick start_time is UTC without a tz suffix. VODs can disappear from a channel's list.
- Twitch viewer clips need twitch.client_id/client_secret (Helix). Not configured yet.
- A 13h Kai Cenat VOD took ~1h end to end. max_duration_min (default 360) skips marathons.
- First test (Kai Cenat, loudness only): clips were "meh", captions out of sync. The sync bug is now fixed (section alignment) and viewer-clip hints were added. Both still need to be validated on a real run.

## Next up
- Validate viewer-clip picks + caption sync on a Kick VOD (N3on: `python run.py process https://kick.com/n3on --name N3on`).
- Auto-posting: plan is a spare Pixel 7 Pro (cracked screen) running Termux + uiautomator2 that pulls approved
  clips from this server and posts via the TikTok/IG/YouTube apps. Fallback: Upload-Post API (Basic $24/mo).
  YouTube API audit was rejected. Add an approve step before posting.
- Maybe: AI judging of transcripts, chat-spike signal.
- New clipping channel (name not chosen yet) separate from The Curious Archive. Warm up accounts: 2-3/day, ramp to 10.
