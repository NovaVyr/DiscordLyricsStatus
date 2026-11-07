> ⚠️ WARNING
> - Discord has no official API for changing your custom status from a program.
>   This script uses your personal account token ("self-bot"), which violates Discord's
>   Terms of Service. Your account could be limited or banned. Use at your own risk.
> - Running this script for long periods, or with very frequent updates, may get your
>   account limited or banned. Consider using a secondary account instead of your main one.
> - Changing a custom status does not send a notification or ping to other users. The
>   script sends at most one update every 3 seconds (MIN_SEND_INTERVAL). Do not set it lower.
> - Your token gives FULL access to your account. Never share it, never post it in
>   screenshots, never commit it to GitHub.

# Discord Lyrics Status

Shows the current synced lyric line of the Spotify desktop track as your Discord custom status, and restores your original status when you pause or quit.

## Requirements

- Windows 10 or 11
- Spotify desktop app (not the web player)
- Python 3.10 or newer
- Active internet connection

## Installation

1. Create and activate a virtual environment (optional but recommended):
   ```powershell
   python -m venv .venv
   .\.venv\Scripts\activate
   ```

2. Install dependencies:
   ```bash
   pip install -r requirements.txt
   ```

## Getting your Discord token

1. Open https://discord.com/app in a desktop browser (Chrome/Edge/Firefox) and log in.
2. Press F12 to open Developer Tools and go to the "Network" tab.
3. In the filter box type: api
4. Do something in Discord so requests appear (e.g. click another server or channel).
5. Click any request in the list (for example one named "library", "science" or "@me").
6. Open the "Headers" tab, scroll to "Request Headers" and find "authorization".
7. Copy its value. Do not include quotes or spaces.

The script checks for your token in the following order:
- **a. Environment variable `DISCORD_TOKEN`**:
  ```powershell
  $env:DISCORD_TOKEN="your_token_here"
  ```
- **b. File `discord_token.txt`**:
  A file named `discord_token.txt` in this folder containing only your token on a single line (copy `discord_token.example.txt` and rename it).
- **c. Interactive prompt**:
  Prompted when the script runs (input is hidden).

*If your token ever leaks: change your Discord password immediately to invalidate the old token.*

## Usage

```powershell
python status_discord.py
```

### Hotkeys
- `Ctrl+C` or `Ctrl+Shift+Q`: Quit (original status is automatically restored).
- `Ctrl+Shift+Left`: Shift lyric timing backward by 0.25s.
- `Ctrl+Shift+Right`: Shift lyric timing forward by 0.25s.

## Configuration

Key settings can be modified near the top of `status_discord.py`:

| Setting | Default | Description |
| :--- | :--- | :--- |
| `EMOJI` | `"🎵"` | Status emoji prefix (`""` for no emoji) |
| `LYRIC_LEAD` | `0.5` | Seconds to display lyrics ahead to compensate for network/Discord latency |
| `MIN_SEND_INTERVAL` | `3.0` | Minimum delay in seconds between Discord updates (do not lower) |

## Notes

- Lyrics are fetched from LRCLIB and cached in a local `lyrics_cache/` folder (git-ignored).
- Spotify desktop app is required; the script reads playback information through Windows Media Control.


## Acknowledgements
   - Developed with the help of Claude (Anthropic) and Antigravity (Google).
   

## Disclaimer

Not affiliated with Spotify or Discord. Use at your own risk.

## License

MIT License. See the [LICENSE](LICENSE) file.
