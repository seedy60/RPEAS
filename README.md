# AliEAS: Software-Defined EAS Discord Relay
Version: 2.0 (RP Manual Mode)

AliEAS is a boutique, highly authentic Software-Defined ENDEC terminal built for Discord. It was engineered to capture the exact aesthetic and functionality of a real analog NOAA Weather Radio system, complete with legacy 32-bit voice engines, mathematically generated SAME bursts, and a professional web-based dashboard.

Official Repository: [https://github.com/averlice/AliEAS](https://github.com/averlice/AliEAS)

## License
Distributed under the **MIT License**. See `LICENSE` for more information.

## Features
*   **Authentic Audio:** Uses `EASGen` to dynamically generate authentic SAME AFSK data bursts (Headers and EOM).
*   **Selectable Voice Engine:** Uses Windows SAPI voices and lets each server choose any installed system voice (`fco!voices`, `fco!setvoice`).
*   **Radio Atmosphere:** Layers a dynamic 60Hz electronic hum, white noise, and mic key-up/key-down clicks underneath the voice to simulate a real analog radio broadcast.
*   **RP-First Manual Alerts:** Trigger custom roleplay alerts on demand with your own event and message text (no automatic live alert polling).
*   **UK Forecasts Anywhere:** Fetches and speaks a real 7-day forecast for any UK location using live geocoding + forecast APIs, with optional pre/post forecast sounds.
*   **Zero-Trust Web Dashboard:** A built-in `aiohttp` web server (Port 2424) that provides a screen-reader friendly ENDEC control panel. Secured via **Discord OAuth2** identity verification (only the Bot Owner can access the controls).
*   **Permanent Audio Archive:** Automatically logs and permanently saves every generated broadcast to a dedicated archive folder with a built-in web playback library.
*   **Global Manual Overrides:** The `fco!pipe` command allows administrators to upload any `.mp3` or `.m4a` file and instantly broadcast it globally with full EAS tones across all configured servers.

## Installation Requirements

### 1. Python Dependencies
Ensure you have Python installed, then run:
```bash
pip install -r requirements.txt
```

### 2. External Dependencies
*   **FFmpeg:** You must have `ffmpeg` installed and added to your system PATH for `discord.py` and `pydub` to process audio.
*   **Windows SAPI Voice(s):** Install any SAPI voice on your Windows host. The bot can list and use any installed voice.

### 3. Environment Variables (.env)
Create a `.env` file in the root directory with the following variables:
```env
DISCORD_TOKEN=your_bot_token_here
DISCORD_CLIENT_ID=your_oauth2_client_id
DISCORD_CLIENT_SECRET=your_oauth2_client_secret
BOT_OWNER_IDS=123456789012345678,987654321098765432
REDIRECT_URI=http://localhost:2424/callback
```
*(Note: Change the `REDIRECT_URI` if you are hosting the dashboard behind a reverse proxy like Cloudflare).*

`BOT_OWNER_IDS` is a comma-separated list of Discord user IDs that are allowed to run owner-only commands and access the web ENDEC owner dashboard.

## Running the Bot
Run the bot manually from your terminal:
```bash
python bot.py
```
The bot will start its Discord connection and launch the Web ENDEC on `http://localhost:2424`. 

If you place valid `cert.pem` and `key.pem` files in the root directory, the Web ENDEC will automatically detect them and launch in secure `HTTPS` mode.

## Discord Commands

### General Commands
*   `fco!join` - Joins your current voice channel.
*   `fco!leave` - Leaves the voice channel.
*   `fco!active` - Shows the most recent manual RP alert in this server.
*   `fco!history` - Shows the last 5 manual alerts in this server.
*   `fco!weather [UK location] [--sounds]` - Reads a real UK 7-day forecast for any location, optionally with pre/post sounds.
*   `fco!weathersounds` - Shows current weather intro/outro sound configuration.
*   `fco!voices` - Lists installed system voices.
*   `fco!voice` - Shows the currently configured voice for this server.
*   `fco!prefix` - Shows the current command prefix for this server.
*   `fco!windunit` - Shows current wind speed unit (kph or mph).
*   `fco!stop` or `fco!silence` - Instantly stops audio playback.
*   `fco!status` - Shows bot health, latency, configured voice, and default UK location.

### Admin Commands (Server Admins)
*   `fco!setup [default UK location]` - Sets voice/text channels and optional default UK forecast location.
*   `fco!setprefix <new prefix>` - Changes the command prefix for this server.
*   `fco!setwindunit <mph|kph>` - Changes wind speed unit for weather forecasts.
*   `fco!setvoice <voice name>` - Sets the server voice to any installed SAPI voice.
*   `fco!setweatherintro` - Upload an audio attachment to play before weather speech when `--sounds` is used.
*   `fco!setweatheroutro` - Upload an audio attachment to play after weather speech when `--sounds` is used.
*   `fco!clearweathersounds` - Clears custom intro/outro and falls back to default tones.
*   `fco!customalert <event | message | area(optional) | severity(optional)>` - Sends a custom roleplay EAS alert.
*   `fco!test` - Broadcasts a test EAS alert in this server only.

## Example Command Usage

Default prefix is `fco!`.

```text
fco!setup London
fco!voices
fco!setvoice Microsoft Hazel Desktop
fco!setwindunit mph
fco!setweatherintro   (attach intro.mp3)
fco!setweatheroutro   (attach outro.mp3)
fco!weather Manchester --sounds
fco!customalert Flood Warning | River levels are rising rapidly | York | Severe
```

Changing to a custom prefix (`!`) and using it:

```text
fco!setprefix !
!help
!prefix
!weather Glasgow
!customalert Power Outage | Widespread outages reported across the city
```

### Owner Commands
*   `fco!testglobal` or `fco!testg` - Tests all configured servers globally.
*   `fco!pipe` - Broadcasts an uploaded audio file (.mp3, .wav, .m4a) with full EAS tones.
*   `fco!serverslist` - Lists all configured servers.
*   `fco!freshpull` - Forces an immediate API pull.
*   `fco!restart` - Restarts the bot process.
*   `fco!shutdown` - Shuts down the bot.
*   `fco!getlogs` - Uploads the background log files to Discord.

## Credits & Thanks
This project leverages `EASGen` for SAME burst generation and `pydub` for audio manipulation. The 32-bit PowerShell bridge is a custom workaround for modern Windows environments. 
