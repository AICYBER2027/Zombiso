# Direct-feed (second OBS output) test — 2026-11-17

Isolated test stack on the owner's laptop (i5-7300HQ, GTX 1050 4 GB, 8 GB RAM): its own Postgres, the
MediaMTX 1.9.3 receiver, a worker running this branch's code. No ports opened to the internet; the real
database and running stack were not touched. Live publishing off, LLM mock, Whisper off.

Simulated OBS: FFmpeg sending a 1080p60, ~6 Mbps H.264 + AAC stream over RTMP with loud moments at
75 s, 150 s and 215 s.

## Results

| Check | Result |
|---|---|
| Wrong stream key / unknown feed name | refused (MediaMTX -> worker `/feed-auth` -> 401) |
| Correct key | accepted; 6 s buffer chunks written; audio listener connected |
| Audio spike detection | all 3 loud moments detected (+16 to +21 dB over normal) |
| Buffer cut (20 s before + 60 s after) | 80.0 s file, 15 chunks, ready 62 s after detection, no re-encode |
| EN + BR edit (720x1280, 1 encode per language, NVENC) | 51 s; moment ready 113 s after detection |
| Moments 2 and 3 | skipped as duplicates (identical test pattern) — correct behaviour |
| OBS drops 10 s and reconnects | session stays LIVE, reconnect counted |
| OBS gone > 30 s | session ENDED; database refuses new moments |

## Resources (1 feed)

| Part | CPU | RAM |
|---|---|---|
| MediaMTX receiver | ~9 % of one core | 76 MB |
| Simulated OBS sender | ~5 % | 16 MB |
| Worker | ~48 % average, 277 % peak while editing | 510 MB peak |
| GTX 1050 | encoder 8-23 % | 99 MB VRAM |
| Host free RAM | min 308 MB | |

## Not done

The three-feed run was not completed: generating the three test streams ran the PC out of memory and was
stopped. To run after the RAM upgrade (or with other programs closed).
