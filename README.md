# Clipper

Watches streamers on **Kick, Twitch and YouTube**, grabs new VODs, finds the hype moments and renders
**1080×1920 shorts with burned-in captions**, ready for TikTok / Reels / Shorts and Whop Content Rewards.

## How it works

1. **Monitor:** every 30 min, lists each streamer's latest VODs on every platform in `config.yaml`.
2. **Audio first:** downloads audio only (small), measures loudness for every second, and finds spikes above the streamer's normal level.
3. **Transcribe only the candidates:** Whisper runs on the top moments only, not the whole 5-hour stream. Moments are re-scored by hype words ("no way", "let's go", "clip that"…).
4. **Download only what's needed:** fetches just those video sections, not the full VOD.
5. **Render:** vertical layout (gameplay over a blurred background), word-by-word captions, streamer credit, loudness-normalised audio.
6. Writes `output/<date>/<clip>.mp4` plus a `.txt` with the source link, timestamp and a suggested caption.

## Install (Ubuntu server)

```bash
git clone git@github.com:forgeframehq/clipper.git ~/clipper
cd ~/clipper && bash install.sh
```

## Use

```bash
cd ~/clipper && source venv/bin/activate
nano config.yaml                                  # streamers, filters
python run.py check                               # what's new (downloads nothing)
python run.py process "<VOD URL>" --name "Adin Ross"   # clip one video now
python run.py once                                # check everyone + clip new VODs
systemctl enable --now clipper.timer              # run automatically every 30 min
journalctl -u clipper -f                          # watch it work
```

Get clips onto your phone: `scp -r root@SERVER:~/clipper/output ./` from Termux, or browse them with any SFTP app.

## Tips

- **GTA 6 launch:** set `filters.title_keywords: ["gta", "grand theft auto"]` so only GTA streams get clipped.
- **Faster, no captions:** `python run.py once --no-captions`.
- **Only clip streamers with a campaign or permission.** Campaigns usually require credit, so `show_credit` stays on.
- Check the logs (`logs/clipper.log`) if a platform stops working, then `pip install -U yt-dlp` (sites change often).
