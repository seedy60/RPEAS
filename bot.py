import discord
from discord.ext import commands
import os
import requests
import asyncio
import json
import getpass
import re
import aiohttp
from aiohttp import web
import aiohttp_session
from aiohttp_session.cookie_storage import EncryptedCookieStorage
from urllib.parse import urlencode
from dotenv import load_dotenv
from eas_audio import generate_eas_message, generate_normal_speech, list_installed_voices
from datetime import datetime
import sys

try:
    import nacl  # noqa: F401
    VOICE_RUNTIME_READY = True
    VOICE_RUNTIME_ERROR = ""
except Exception as exc:
    VOICE_RUNTIME_READY = False
    VOICE_RUNTIME_ERROR = str(exc)


def interactive_env_setup_if_missing():
    """Creates a .env file via interactive prompts when it does not exist."""
    env_path = ".env"
    if os.path.exists(env_path):
        return

    print("No .env file detected. Starting interactive setup...")
    print("Provide the required values for your Discord bot and dashboard OAuth.")

    token = getpass.getpass("DISCORD_TOKEN: ").strip()
    client_id = input("DISCORD_CLIENT_ID: ").strip()
    client_secret = getpass.getpass("DISCORD_CLIENT_SECRET: ").strip()
    owner_ids = input("BOT_OWNER_IDS (comma-separated Discord user IDs): ").strip()
    default_redirect = "http://localhost:2424/callback"
    redirect_uri = input(f"REDIRECT_URI [{default_redirect}]: ").strip() or default_redirect

    env_contents = (
        f"DISCORD_TOKEN={token}\n"
        f"DISCORD_CLIENT_ID={client_id}\n"
        f"DISCORD_CLIENT_SECRET={client_secret}\n"
        f"BOT_OWNER_IDS={owner_ids}\n"
        f"REDIRECT_URI={redirect_uri}\n"
    )

    with open(env_path, "w", encoding="utf-8") as env_file:
        env_file.write(env_contents)

    print(".env created successfully. Continuing startup...")


# Load configuration from .env
interactive_env_setup_if_missing()
load_dotenv()
TOKEN = os.getenv('DISCORD_TOKEN')
BOT_VERSION = "2.0"
DEFAULT_PREFIX = "fco!"
LEGACY_PREFIXES = ["fco!", "vco!"]


def parse_owner_id_set(raw_ids):
    owner_set = set()
    for item in (raw_ids or "").split(","):
        cleaned = item.strip()
        if cleaned.isdigit():
            owner_set.add(cleaned)
    return owner_set


# Supports one or many owners. BOT_OWNER_IDS is the canonical setting.
# Legacy BOT_OWNER_ID is still accepted as fallback.
BOT_OWNER_IDS = parse_owner_id_set(os.getenv("BOT_OWNER_IDS"))
legacy_owner = (os.getenv("BOT_OWNER_ID") or "").strip()
if legacy_owner.isdigit():
    BOT_OWNER_IDS.add(legacy_owner)


def is_configured_owner_id(user_id):
    return str(user_id) in BOT_OWNER_IDS


async def configured_owner_check(ctx):
    if is_configured_owner_id(ctx.author.id):
        return True
    raise commands.NotOwner()


def configured_owner_only():
    return commands.check(configured_owner_check)

# JSON Database Setup
DB_FILE = "servers.json"

# Define the archive directory outside of the bot folder
ARCHIVE_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "alerts_archive"))
WEATHER_SOUNDS_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), "weather_sounds"))

def load_db():
    if os.path.exists(DB_FILE):
        with open(DB_FILE, "r") as f:
            try:
                return json.load(f)
            except json.JSONDecodeError:
                return {}
    return {}

def save_db(data):
    with open(DB_FILE, "w") as f:
        json.dump(data, f, indent=4)

servers_db = load_db()


def get_guild_prefix(guild_id):
    config = servers_db.get(str(guild_id), {})
    return config.get("command_prefix", DEFAULT_PREFIX)


async def get_prefix(bot_instance, message):
    if message.guild:
        guild_config = servers_db.get(str(message.guild.id), {})
        custom_prefix = guild_config.get("command_prefix")
        if custom_prefix:
            prefixes = [custom_prefix]
        else:
            prefixes = LEGACY_PREFIXES
    else:
        prefixes = LEGACY_PREFIXES

    # Remove duplicates while preserving order.
    unique_prefixes = list(dict.fromkeys(prefixes))
    return commands.when_mentioned_or(*unique_prefixes)(bot_instance, message)

# Configure bot
intents = discord.Intents.default()
intents.message_content = True
intents.voice_states = True

bot = commands.Bot(command_prefix=get_prefix, intents=intents, help_command=None)


def get_voice_dependency_message():
    return (
        "Voice features are unavailable because the PyNaCl voice dependency is missing.\n"
        "Install it in the same Python environment used to run the bot: `pip install PyNaCl`\n"
        f"Details: {VOICE_RUNTIME_ERROR or 'PyNaCl not detected'}"
    )


async def ensure_voice_runtime(ctx):
    if VOICE_RUNTIME_READY:
        return True
    await ctx.send(get_voice_dependency_message())
    return False


async def get_or_connect_voice_client(guild, config=None, fallback_channel=None):
    """Returns a healthy connected voice client, reconnecting when stale/disconnected."""
    config = config or {}
    vc = guild.voice_client

    if vc and not vc.is_connected():
        try:
            await vc.disconnect(force=True)
        except Exception:
            pass
        vc = None

    target_channel = fallback_channel
    if not target_channel:
        vc_id = config.get("voice_channel_id")
        if vc_id:
            target_channel = guild.get_channel(vc_id)

    if not vc and not target_channel:
        return None, "No configured voice channel. Run setup while connected to voice."

    if target_channel:
        perms = target_channel.permissions_for(guild.me)
        if not perms.connect:
            return None, f"I do not have Connect permission in {target_channel.mention}."
        if not perms.speak:
            return None, f"I do not have Speak permission in {target_channel.mention}."

    try:
        if vc and target_channel and vc.channel.id != target_channel.id:
            await vc.move_to(target_channel)
        elif not vc and target_channel:
            vc = await target_channel.connect(self_deaf=True, reconnect=True, timeout=20.0)

        # If connected to a Stage Channel, unsuppress so audio can be heard.
        if vc and isinstance(vc.channel, discord.StageChannel):
            try:
                await guild.me.edit(suppress=False)
            except Exception:
                # If missing permissions, playback may still be inaudible. Caller gets normal VC.
                pass
    except Exception as e:
        return None, f"Failed to connect to voice: {e}"

    return vc, None


async def play_audio_file(vc, file_path):
    """Plays audio on an existing voice client and returns an error string on failure."""
    if not os.path.exists(file_path):
        return f"Audio file not found: {file_path}"
    if os.path.getsize(file_path) <= 0:
        return f"Audio file is empty: {file_path}"

    if vc.is_playing() or vc.is_paused():
        vc.stop()

    # FFmpegOpusAudio avoids local opus-library dependency issues that can cause silent playback.
    try:
        source = discord.FFmpegOpusAudio(file_path)
        vc.play(source, after=lambda err: print(f"Playback error: {err}") if err else None)
    except Exception as e:
        return f"Failed to start playback: {e}"

    return None

# --- State Variables ---
alert_history = {}  # Dict of guild_id -> list of alerts

UK_WEATHER_CODE_MAP = {
    0: "clear skies",
    1: "mainly clear",
    2: "partly cloudy",
    3: "overcast",
    45: "fog",
    48: "depositing rime fog",
    51: "light drizzle",
    53: "drizzle",
    55: "dense drizzle",
    56: "light freezing drizzle",
    57: "freezing drizzle",
    61: "light rain",
    63: "rain",
    65: "heavy rain",
    66: "light freezing rain",
    67: "freezing rain",
    71: "light snowfall",
    73: "snowfall",
    75: "heavy snowfall",
    77: "snow grains",
    80: "rain showers",
    81: "heavier rain showers",
    82: "violent rain showers",
    85: "snow showers",
    86: "heavy snow showers",
    95: "thunderstorm",
    96: "thunderstorm with slight hail",
    99: "thunderstorm with hail"
}


def get_wind_unit(guild_id):
    config = servers_db.get(str(guild_id), {})
    return config.get("wind_unit", "kph")


def convert_wind_speed(value_kph, unit):
    if value_kph in [None, "?"]:
        return "?"
    try:
        speed = float(value_kph)
    except (TypeError, ValueError):
        return "?"

    if unit == "mph":
        return round(speed * 0.621371, 1)
    return round(speed, 1)

# --- Web Server (ENDEC Dashboard) ---

# aiohttp_session EncryptedCookieStorage requires exactly 32 raw bytes.
WEB_SESSION_KEY = os.urandom(32)

async def discord_login(request):
    """Redirects the user to Discord's OAuth2 login page."""
    client_id = os.getenv("DISCORD_CLIENT_ID")
    redirect_uri = (os.getenv("REDIRECT_URI") or "").strip()
    if not client_id or not redirect_uri:
        return web.Response(status=500, text="OAuth2 not configured in .env file.")

    oauth_query = urlencode(
        {
            "client_id": client_id,
            "redirect_uri": redirect_uri,
            "response_type": "code",
            "scope": "identify",
        }
    )
    oauth_url = f"https://discord.com/api/oauth2/authorize?{oauth_query}"
    raise web.HTTPFound(oauth_url)

async def discord_callback(request):
    """Handles the callback from Discord, gets the user ID, and sets the session."""
    code = request.query.get("code")
    if not code:
        return web.Response(status=400, text="Missing authorization code.")
        
    client_id = os.getenv("DISCORD_CLIENT_ID")
    client_secret = os.getenv("DISCORD_CLIENT_SECRET")
    redirect_uri = (os.getenv("REDIRECT_URI") or "").strip()
    
    token_url = "https://discord.com/api/oauth2/token"
    data = {
        "client_id": client_id,
        "client_secret": client_secret,
        "grant_type": "authorization_code",
        "code": code,
        "redirect_uri": redirect_uri
    }
    headers = {"Content-Type": "application/x-www-form-urlencoded"}
    
    async with aiohttp.ClientSession() as session:
        async with session.post(token_url, data=data, headers=headers) as resp:
            if resp.status != 200:
                return web.Response(status=400, text="Failed to authenticate with Discord.")
            token_data = await resp.json()
            access_token = token_data.get("access_token")
            
        user_url = "https://discord.com/api/users/@me"
        user_headers = {"Authorization": f"Bearer {access_token}"}
        async with session.get(user_url, headers=user_headers) as resp:
            if resp.status != 200:
                return web.Response(status=400, text="Failed to fetch user data from Discord.")
            user_data = await resp.json()
            user_id = user_data.get("id")
            
    if not is_configured_owner_id(user_id):
        return web.Response(status=403, text="Forbidden: You are not the authorized Bot Owner.")
        
    web_session = await aiohttp_session.get_session(request)
    web_session["authenticated"] = True
    web_session["user_id"] = user_id
    
    raise web.HTTPFound('/')

async def require_auth(request):
    """Helper function to check authentication before rendering pages."""
    session = await aiohttp_session.get_session(request)
    if not session.get("authenticated"):
        raise web.HTTPFound('/login')

async def web_index(request):
    """Serve a simple, screen-reader friendly ENDEC control panel with an archive library."""
    await require_auth(request)
    
    archive_items = []
    if os.path.exists(ARCHIVE_DIR):
        files = sorted(os.listdir(ARCHIVE_DIR), reverse=True)
        for file in files[:20]:
            if file.endswith(".mp3"):
                archive_items.append(file)
                
    archive_html = ""
    if not archive_items:
        archive_html = "<li>No archived alerts found yet.</li>"
    else:
        for item in archive_items:
            readable_name = item.replace(".mp3", "").replace("_", " ")
            archive_html += f"""
            <li>
                <strong>{readable_name}</strong><br>
                <audio controls src="/archive/{item}" preload="none" aria-label="Playback for {readable_name}">
                    Your browser does not support the audio element.
                </audio>
                <br><a href="/archive/{item}" download style="color: #00ff00; font-size: 0.8em;">Download File</a>
            </li>
            <hr style="border: 0; border-top: 1px solid #222;">
            """

    html = f"""
    <!DOCTYPE html>
    <html lang="en">
    <head>
        <meta charset="UTF-8">
        <title>EAS ENDEC Interface</title>
        <style>
            body {{ font-family: monospace; background-color: #121212; color: #00ff00; padding: 20px; line-height: 1.5; }}
            h1, h2, h3 {{ border-bottom: 1px solid #00ff00; padding-bottom: 5px; }}
            .btn {{ background-color: #004400; color: #00ff00; border: 1px solid #00ff00; padding: 12px 24px; cursor: pointer; margin: 10px 5px; font-family: monospace; font-size: 1.1em; }}
            .btn:hover {{ background-color: #00ff00; color: #121212; }}
            .panel {{ border: 1px solid #00ff00; padding: 15px; margin-top: 20px; }}
            ul {{ list-style-type: none; padding: 0; }}
            li {{ padding: 15px 0; }}
            a {{ color: #00ff00; }}
        </style>
    </head>
    <body>
        <header>
            <h1>EAS Software-Defined ENDEC (v{BOT_VERSION})</h1>
        </header>
        <main>
            <section class="panel" aria-labelledby="sys-status">
                <h2 id="sys-status">System Status</h2>
                <p><strong>Connection:</strong> ONLINE</p>
                <p><strong>Latency:</strong> {round(bot.latency * 1000)}ms</p>
                <p><strong>Connected Servers:</strong> {len(servers_db)}</p>
                <p><strong>Alert Mode:</strong> Manual RP Alerts</p>
            </section>
            <section class="panel" aria-labelledby="controls">
                <h2 id="controls">Manual Trigger Controls</h2>
                <form action="/test" method="post" style="display:inline;">
                    <button type="submit" class="btn">Trigger Global Test</button>
                </form>
                <form action="/stop" method="post" style="display:inline;">
                    <button type="submit" class="btn">Stop All Audio</button>
                </form>
            </section>
            <section class="panel" aria-labelledby="archive">
                <h2 id="archive">Broadcast Archive (Recent 20)</h2>
                <ul>{archive_html}</ul>
            </section>
        </main>
    </body>
    </html>
    """
    return web.Response(text=html, content_type='text/html')

async def web_serve_archive(request):
    filename = request.match_info.get('filename')
    safe_path = os.path.join(ARCHIVE_DIR, filename)
    if os.path.exists(safe_path) and filename.endswith(".mp3"):
        return web.FileResponse(safe_path)
    return web.Response(status=404, text="File not found")

async def trigger_global_test(trigger_source="Web ENDEC UI"):
    print(f"Initiating global test via {trigger_source}...")
    pre_text = f"This is a test of the E A S discord bot, issued via the {trigger_source}."
    main_text = "This is a test of the Emergency Alert System. This is only a test. If this had been an actual emergency, you would have received official instructions. This concludes this test."
    try:
        import shutil
        timestamp = datetime.now().strftime("%Y-%m-%d_%H-%M-%S")
        base_filename = f"{ARCHIVE_DIR}/global_test_alert_{timestamp}.mp3"
        await asyncio.to_thread(generate_eas_message, main_text, base_filename, pre_text)
        for guild_id_str, config in servers_db.items():
            guild = bot.get_guild(int(guild_id_str))
            if not guild: continue
            vc, _ = await get_or_connect_voice_client(guild, config)
            if not vc or not vc.is_connected() or vc.is_playing(): continue
            guild_filename = f"{ARCHIVE_DIR}/global_test_{guild.id}_{timestamp}.mp3"
            shutil.copy(base_filename, guild_filename)
            text_id = config.get("text_channel_id")
            if text_id:
                text_channel = guild.get_channel(text_id)
                if text_channel:
                    embed = discord.Embed(title="🚨 Global Test Alert", description=f"**Location:** {config.get('uk_location', 'All Zones')}\n**Issued By:** {trigger_source}", color=discord.Color.blue())
                    embed.add_field(name="Headline", value="This is a global test of the Emergency Alert System.", inline=False)
                    embed.add_field(name="Details", value=main_text, inline=False)
                    bot.loop.create_task(text_channel.send(content="🚨 **GLOBAL TEST ALERT** 🚨", embed=embed))
            play_err = await play_audio_file(vc, guild_filename)
            if play_err:
                print(f"Global test playback skipped in guild {guild.id}: {play_err}")
    except Exception as e:
        print(f"Error during web-triggered global test: {e}")

async def web_trigger_test(request):
    bot.loop.create_task(trigger_global_test())
    return web.HTTPFound('/')

async def web_stop_audio(request):
    for vc in bot.voice_clients:
        if vc.is_playing(): vc.stop()
    return web.HTTPFound('/')

async def web_force_poll(request):
    # Auto alerts are intentionally disabled in RP mode.
    return web.HTTPFound('/')

async def start_web_server():
    app = web.Application()
    aiohttp_session.setup(app, EncryptedCookieStorage(WEB_SESSION_KEY))
    app.add_routes([
        web.get('/', web_index), web.get('/login', discord_login),
        web.get('/callback', discord_callback), web.get('/archive/{filename}', web_serve_archive),
        web.post('/test', web_trigger_test), web.post('/stop', web_stop_audio),
        web.post('/poll', web_force_poll)
    ])
    runner = web.AppRunner(app)
    await runner.setup()
    import ssl
    ssl_context = None
    if os.path.exists("cert.pem") and os.path.exists("key.pem"):
        try:
            ssl_context = ssl.create_default_context(ssl.Purpose.CLIENT_AUTH)
            ssl_context.load_cert_chain("cert.pem", "key.pem")
            print("🔒 SSL Certificates found! Web ENDEC will run in secure HTTPS mode.")
        except Exception as e:
            print(f"⚠️ Failed to load SSL certificates: {e}. Falling back to HTTP.")
    site = web.TCPSite(runner, '0.0.0.0', 2424, ssl_context=ssl_context)
    await site.start()
    protocol = "https" if ssl_context else "http"
    print(f"🌐 Web ENDEC Interface started on {protocol}://localhost:2424")

@bot.event
async def on_ready():
    print(f'Logged in as {bot.user.name} (ID: {bot.user.id})')
    if not BOT_OWNER_IDS:
        print("WARNING: BOT_OWNER_IDS is empty. Owner-only commands and web owner auth will deny everyone.")
    if not VOICE_RUNTIME_READY:
        print(f"Voice support disabled: {get_voice_dependency_message()}")
    if not os.path.exists(ARCHIVE_DIR):
        os.makedirs(ARCHIVE_DIR)
        print(f"Created archive folder at {ARCHIVE_DIR}")
    if not os.path.exists(WEATHER_SOUNDS_DIR):
        os.makedirs(WEATHER_SOUNDS_DIR)
        print(f"Created weather sounds folder at {WEATHER_SOUNDS_DIR}")
    bot.loop.create_task(start_web_server())
    for guild_id_str, config in servers_db.items():
        vc_id = config.get("voice_channel_id")
        if vc_id:
            channel = bot.get_channel(vc_id)
            if channel and isinstance(channel, discord.VoiceChannel):
                try: await channel.connect(self_deaf=True)
                except Exception as e: print(f"Failed to auto-join VC {vc_id}: {e}")

@bot.event
async def on_command_error(ctx, error):
    if isinstance(error, commands.NotOwner):
        try:
            await ctx.message.delete()
            await ctx.author.send(f"❌ {ctx.author.mention}, you do not have permission to use the `{ctx.command.name}` command. Error: Owner-only command.")
        except: pass
    elif isinstance(error, commands.MissingPermissions):
        try:
            await ctx.message.delete()
            missing = ", ".join(error.missing_permissions)
            await ctx.author.send(f"❌ {ctx.author.mention}, you do not have permission to use the `{ctx.command.name}` command. Error: Missing permissions ({missing}).")
        except: pass
    elif isinstance(error, commands.CommandNotFound):
        pass
    elif isinstance(error, RuntimeError) and "library needed in order to use voice" in str(error).lower():
        await ctx.send(get_voice_dependency_message())
    else: print(f"Command error in {ctx.command}: {error}")


@bot.event
async def on_message(message):
    if message.author.bot:
        return

    if message.guild:
        current_prefix = get_guild_prefix(message.guild.id)
        content = message.content.strip()

        # If user appears to call a known command with the wrong prefix,
        # provide a hint instead of failing silently.
        m = re.match(r"^(\S{1,6}!)([A-Za-z0-9_]+)\b", content)
        if m:
            used_prefix = m.group(1)
            command_name = m.group(2).lower()
            known_command = bot.get_command(command_name)

            if known_command and used_prefix != current_prefix:
                await message.channel.send(
                    f"Unknown prefix `{used_prefix}`. Current prefix for this server is `{current_prefix}`. Try `{current_prefix}{command_name}`."
                )

    await bot.process_commands(message)

@bot.command()
async def help(ctx):
    prefix = get_guild_prefix(ctx.guild.id) if ctx.guild else DEFAULT_PREFIX
    embed = discord.Embed(title="📡 RP EAS Bot Help & Commands", description=f"Manual RP alert bot with selectable voices and UK forecasts. **Version {BOT_VERSION}**", color=discord.Color.blue())
    embed.add_field(name="🎙️ General Commands", value=f"`{prefix}join`, `{prefix}leave`, `{prefix}active`, `{prefix}history`, `{prefix}weather [UK location] [--sounds]`, `{prefix}voices`, `{prefix}voice`, `{prefix}prefix`, `{prefix}windunit`, `{prefix}weathersounds`, `{prefix}stop`, `{prefix}status`", inline=False)
    embed.add_field(name="⚙️ Admin Commands", value=f"`{prefix}setup [default UK location]`, `{prefix}setvoice <voice name>`, `{prefix}setprefix <new prefix>`, `{prefix}setwindunit <mph|kph>`, `{prefix}setweatherintro` (attach file), `{prefix}setweatheroutro` (attach file), `{prefix}clearweathersounds`, `{prefix}customalert <event | message | area(optional) | severity(optional)>`, `{prefix}test`", inline=False)
    if is_configured_owner_id(ctx.author.id):
        embed.add_field(name="👑 Owner Commands", value=f"`{prefix}testg`, `{prefix}pipe`, `{prefix}serverslist`, `{prefix}restart`, `{prefix}shutdown`, `{prefix}getlogs`", inline=False)
    await ctx.send(embed=embed)

@bot.command()
@commands.has_permissions(administrator=True)
async def setup(ctx, *, default_uk_location: str = None):
    if not await ensure_voice_runtime(ctx):
        return

    if not ctx.author.voice:
        await ctx.send("⚠️ You must be in a voice channel to finish setup.")
        return

    vc_id, text_id = ctx.author.voice.channel.id, ctx.channel.id

    config = servers_db.get(str(ctx.guild.id), {})
    config["text_channel_id"] = text_id
    config["voice_channel_id"] = vc_id
    config["guild_name"] = ctx.guild.name
    if default_uk_location:
        config["uk_location"] = default_uk_location

    servers_db[str(ctx.guild.id)] = config
    save_db(servers_db)

    if ctx.voice_client: await ctx.voice_client.move_to(ctx.author.voice.channel)
    else: await ctx.author.voice.channel.connect(self_deaf=True)

    location_text = config.get("uk_location", "Not set")
    voice_text = config.get("voice_name", "System default")
    await ctx.send(
        f"✅ **Setup Complete!**\n"
        f"📍 **Default UK Forecast Location:** {location_text}\n"
        f"🗣️ **Voice:** {voice_text}\n"
        f"🔊 **VC:** <#{vc_id}>\n"
        f"💬 **Text:** <#{text_id}>"
    )

@bot.command()
async def join(ctx):
    if not await ensure_voice_runtime(ctx):
        return

    if ctx.author.voice:
        vc, err = await get_or_connect_voice_client(
            ctx.guild,
            servers_db.get(str(ctx.guild.id), {}),
            fallback_channel=ctx.author.voice.channel,
        )
        if not vc:
            await ctx.send(err)
            return
        await ctx.send(f"Joined {ctx.author.voice.channel.name}")
    else: await ctx.send("Join a voice channel first.")

@bot.command()
async def leave(ctx):
    if ctx.voice_client:
        await ctx.voice_client.disconnect()
        await ctx.send("Left voice channel.")
    else: await ctx.send("I'm not in a voice channel.")

@bot.command()
async def test(ctx):
    if not await ensure_voice_runtime(ctx):
        return

    guild_id_str = str(ctx.guild.id)
    config = servers_db.get(guild_id_str, {})
    vc, err = await get_or_connect_voice_client(ctx.guild, config)
    if not vc:
        await ctx.send(err or "I need to be in a voice channel first.")
        return
    if vc.is_playing():
        await ctx.send("Audio is already playing.")
        return
    await ctx.send("Generating test EAS message...")
    location = config.get("uk_location", ctx.guild.name)
    voice_name = config.get("voice_name")
    text = f"This is a test of the Emergency Alert System for {location}. This is only a test."
    try:
        text_id = config.get("text_channel_id")
        if text_id:
            text_channel = ctx.guild.get_channel(text_id)
            if text_channel:
                embed = discord.Embed(title="🚨 Required Monthly Test", description=f"**Location:** {location}\n**Issued By:** Bot Admin", color=discord.Color.blue())
                embed.add_field(name="Details", value=text, inline=False)
                await text_channel.send(content="🚨 **TEST ALERT** 🚨", embed=embed)
        timestamp = datetime.now().strftime("%Y-%m-%d_%H-%M-%S")
        filename = f"{ARCHIVE_DIR}/test_alert_{ctx.guild.id}_{timestamp}.mp3"
        intro = "This voice channel has been interrupted in order to partissipate in the emergency alert system."
        await asyncio.to_thread(generate_eas_message, text, filename, intro, voice_name)
        guild_history = alert_history.setdefault(guild_id_str, [])
        guild_history.append({"event": "Test Alert", "time": datetime.now().strftime("%I:%M %p")})
        play_err = await play_audio_file(vc, filename)
        if play_err:
            await ctx.send(f"Failed to start audio playback: {play_err}")
            return
        await ctx.send("Now playing the test message.")
    except Exception as e: await ctx.send(f"Failed to play audio: {e}")


@bot.command(aliases=['rpalert', 'alert'])
@commands.has_permissions(administrator=True)
async def customalert(ctx, *, payload: str):
    if not await ensure_voice_runtime(ctx):
        return

    """
    Usage:
    <prefix>customalert Event | Message | Area(optional) | Severity(optional)
    """
    parts = [p.strip() for p in payload.split('|') if p.strip()]
    if len(parts) < 2:
        prefix = get_guild_prefix(ctx.guild.id) if ctx.guild else DEFAULT_PREFIX
        await ctx.send(f"Usage: `{prefix}customalert Event | Message | Area(optional) | Severity(optional)`")
        return

    event = parts[0]
    message = parts[1]
    area = parts[2] if len(parts) > 2 else ctx.guild.name
    severity = parts[3] if len(parts) > 3 else "Moderate"

    guild_id_str = str(ctx.guild.id)
    config = servers_db.get(guild_id_str, {})
    voice_name = config.get("voice_name")

    fallback_channel = ctx.author.voice.channel if ctx.author.voice else None
    vc, err = await get_or_connect_voice_client(ctx.guild, config, fallback_channel=fallback_channel)

    if not vc:
        await ctx.send(err)
        return

    if vc.is_playing():
        await ctx.send("Audio is already playing.")
        return

    color = discord.Color.gold()
    if severity.lower() in ["severe", "extreme", "critical"]:
        color = discord.Color.red()

    embed = discord.Embed(title=f"🚨 {event}", description=f"**Area:** {area}\n**Severity:** {severity}\n**Issued By:** {ctx.author.display_name}", color=color)
    embed.add_field(name="Details", value=message[:1024], inline=False)
    await ctx.send(content="🚨 **CUSTOM RP ALERT** 🚨", embed=embed)

    spoken_text = f"{event}. {message}. Affected area: {area}. Severity: {severity}."
    timestamp = datetime.now().strftime("%Y-%m-%d_%H-%M-%S")
    filename = f"{ARCHIVE_DIR}/custom_alert_{ctx.guild.id}_{timestamp}.mp3"
    intro = "This voice channel has been interrupted for a roleplay emergency alert."

    try:
        await asyncio.to_thread(generate_eas_message, spoken_text, filename, intro, voice_name)
        guild_history = alert_history.setdefault(guild_id_str, [])
        guild_history.append({"event": event, "time": datetime.now().strftime("%I:%M %p")})
        play_err = await play_audio_file(vc, filename)
        if play_err:
            await ctx.send(f"Failed to start audio playback: {play_err}")
    except Exception as e:
        await ctx.send(f"Failed to generate custom alert audio: {e}")


@bot.command(name='voices')
async def voices(ctx):
    await ctx.send("Scanning installed SAPI voices...")
    voice_names = await asyncio.to_thread(list_installed_voices)
    if not voice_names:
        await ctx.send("No installed SAPI voices were detected.")
        return

    message = "**Installed voices:**\n"
    for name in voice_names:
        line = f"- {name}\n"
        if len(message) + len(line) > 1900:
            await ctx.send(message)
            message = ""
        message += line
    if message:
        await ctx.send(message)


@bot.command(name='voice')
async def voice(ctx):
    config = servers_db.get(str(ctx.guild.id), {})
    selected = config.get("voice_name", "System default")
    await ctx.send(f"Current configured voice: **{selected}**")


@bot.command(name='setvoice')
@commands.has_permissions(administrator=True)
async def setvoice(ctx, *, voice_name: str):
    voice_names = await asyncio.to_thread(list_installed_voices)
    if not voice_names:
        await ctx.send("No installed SAPI voices were detected.")
        return

    selected = next((v for v in voice_names if v.lower() == voice_name.lower()), None)
    if not selected:
        suggestions = [v for v in voice_names if voice_name.lower() in v.lower()]
        if suggestions:
            await ctx.send("Voice not found. Did you mean:\n" + "\n".join(f"- {v}" for v in suggestions[:10]))
        else:
            prefix = get_guild_prefix(ctx.guild.id) if ctx.guild else DEFAULT_PREFIX
            await ctx.send(f"Voice not found. Use `{prefix}voices` to list installed voices.")
        return

    guild_id_str = str(ctx.guild.id)
    config = servers_db.get(guild_id_str, {})
    config["voice_name"] = selected
    servers_db[guild_id_str] = config
    save_db(servers_db)
    await ctx.send(f"✅ Voice set to **{selected}**")


@bot.command(name='prefix')
async def prefix(ctx):
    if not ctx.guild:
        await ctx.send(f"Current command prefix: **{DEFAULT_PREFIX}**")
        return

    current = get_guild_prefix(ctx.guild.id)
    await ctx.send(f"Current command prefix for this server: **{current}**")


@bot.command(name='setprefix')
@commands.has_permissions(administrator=True)
async def setprefix(ctx, *, new_prefix: str):
    if not ctx.guild:
        await ctx.send("This command can only be used in a server.")
        return

    cleaned = new_prefix.strip()
    if not cleaned:
        await ctx.send("Prefix cannot be empty.")
        return
    if len(cleaned) > 5:
        await ctx.send("Prefix must be 1 to 5 characters long.")
        return
    if any(ch.isspace() for ch in cleaned):
        await ctx.send("Prefix cannot contain spaces.")
        return

    guild_id_str = str(ctx.guild.id)
    config = servers_db.get(guild_id_str, {})
    old_prefix = config.get("command_prefix", DEFAULT_PREFIX)
    config["command_prefix"] = cleaned
    servers_db[guild_id_str] = config
    save_db(servers_db)

    await ctx.send(
        f"✅ Command prefix changed from **{old_prefix}** to **{cleaned}**.\n"
        f"Try: `{cleaned}help`"
    )


@bot.command(name='windunit')
async def windunit(ctx):
    unit = get_wind_unit(ctx.guild.id) if ctx.guild else "kph"
    await ctx.send(f"Current wind speed unit: **{unit}**")


@bot.command(name='setwindunit')
@commands.has_permissions(administrator=True)
async def setwindunit(ctx, unit: str):
    if not ctx.guild:
        await ctx.send("This command can only be used in a server.")
        return

    normalized = unit.strip().lower()
    if normalized not in ["mph", "kph"]:
        prefix = get_guild_prefix(ctx.guild.id)
        await ctx.send(f"Invalid unit. Use `{prefix}setwindunit mph` or `{prefix}setwindunit kph`.")
        return

    guild_id_str = str(ctx.guild.id)
    config = servers_db.get(guild_id_str, {})
    config["wind_unit"] = normalized
    servers_db[guild_id_str] = config
    save_db(servers_db)
    await ctx.send(f"✅ Wind speed unit set to **{normalized}**")


@bot.command(name='weathersounds')
async def weathersounds(ctx):
    config = servers_db.get(str(ctx.guild.id), {}) if ctx.guild else {}
    intro_file = config.get("weather_intro_file")
    outro_file = config.get("weather_outro_file")

    intro_status = os.path.basename(intro_file) if intro_file and os.path.exists(intro_file) else "Default tone"
    outro_status = os.path.basename(outro_file) if outro_file and os.path.exists(outro_file) else "Default tone"
    await ctx.send(f"Weather sound settings:\n- Intro: **{intro_status}**\n- Outro: **{outro_status}**")


@bot.command(name='setweatherintro')
@commands.has_permissions(administrator=True)
async def setweatherintro(ctx):
    if not ctx.guild:
        await ctx.send("This command can only be used in a server.")
        return
    if not ctx.message.attachments:
        await ctx.send("Attach an audio file (.mp3, .wav, .ogg, .m4a) with this command.")
        return

    attachment = ctx.message.attachments[0]
    if not any(attachment.filename.lower().endswith(ext) for ext in [".mp3", ".wav", ".ogg", ".m4a"]):
        await ctx.send("Unsupported file format. Use .mp3, .wav, .ogg, or .m4a")
        return

    guild_id_str = str(ctx.guild.id)
    config = servers_db.get(guild_id_str, {})
    ext = os.path.splitext(attachment.filename)[1].lower()
    target_path = os.path.join(WEATHER_SOUNDS_DIR, f"{guild_id_str}_intro{ext}")

    old_intro = config.get("weather_intro_file")
    await attachment.save(target_path)
    if old_intro and old_intro != target_path and os.path.exists(old_intro):
        os.remove(old_intro)

    config["weather_intro_file"] = target_path
    servers_db[guild_id_str] = config
    save_db(servers_db)
    await ctx.send(f"✅ Custom weather intro set to **{attachment.filename}**")


@bot.command(name='setweatheroutro')
@commands.has_permissions(administrator=True)
async def setweatheroutro(ctx):
    if not ctx.guild:
        await ctx.send("This command can only be used in a server.")
        return
    if not ctx.message.attachments:
        await ctx.send("Attach an audio file (.mp3, .wav, .ogg, .m4a) with this command.")
        return

    attachment = ctx.message.attachments[0]
    if not any(attachment.filename.lower().endswith(ext) for ext in [".mp3", ".wav", ".ogg", ".m4a"]):
        await ctx.send("Unsupported file format. Use .mp3, .wav, .ogg, or .m4a")
        return

    guild_id_str = str(ctx.guild.id)
    config = servers_db.get(guild_id_str, {})
    ext = os.path.splitext(attachment.filename)[1].lower()
    target_path = os.path.join(WEATHER_SOUNDS_DIR, f"{guild_id_str}_outro{ext}")

    old_outro = config.get("weather_outro_file")
    await attachment.save(target_path)
    if old_outro and old_outro != target_path and os.path.exists(old_outro):
        os.remove(old_outro)

    config["weather_outro_file"] = target_path
    servers_db[guild_id_str] = config
    save_db(servers_db)
    await ctx.send(f"✅ Custom weather outro set to **{attachment.filename}**")


@bot.command(name='clearweathersounds')
@commands.has_permissions(administrator=True)
async def clearweathersounds(ctx):
    if not ctx.guild:
        await ctx.send("This command can only be used in a server.")
        return

    guild_id_str = str(ctx.guild.id)
    config = servers_db.get(guild_id_str, {})
    intro_file = config.pop("weather_intro_file", None)
    outro_file = config.pop("weather_outro_file", None)

    for file_path in [intro_file, outro_file]:
        if file_path and os.path.exists(file_path):
            os.remove(file_path)

    servers_db[guild_id_str] = config
    save_db(servers_db)
    await ctx.send("✅ Cleared custom weather intro/outro. Default tones will be used.")

@bot.command()
@configured_owner_only()
async def pipe(ctx):
    if not ctx.message.attachments:
        await ctx.send("Please upload an audio file (.mp3, .wav, .ogg, .m4a) with this command.")
        return
    attachment = ctx.message.attachments[0]
    if not any(attachment.filename.lower().endswith(ext) for ext in ['.mp3', '.wav', '.ogg', '.m4a']):
        await ctx.send("Unsupported file format.")
        return
    await ctx.send(f"📥 Processing global broadcast: `{attachment.filename}`...")
    try:
        temp_input = f"temp_pipe_input_{ctx.guild.id}_{datetime.now().strftime('%H%M%S')}{os.path.splitext(attachment.filename)[1]}"
        await attachment.save(temp_input)
        from pydub import AudioSegment
        user_audio = await asyncio.to_thread(AudioSegment.from_file, temp_input)
        from EASGen import EASGen
        from eas_audio import apply_radio_filter, _generate_tom
        import shutil
        intro_file = f"temp_pipe_intro_{ctx.guild.id}.wav"
        await asyncio.to_thread(_generate_tom, "This voice channel has been interrupted in order to partissipate in the emergency alert system.", intro_file)
        intro_audio = apply_radio_filter(AudioSegment.from_wav(intro_file))
        header = EASGen.genHeader("ZCZC-WXR-EAN-008043+0015-1231234-KDEN/NWS-")
        attn, eom = EASGen.genATTN(8), EASGen.genEOM()
        def compile_broadcast():
            silence = AudioSegment.silent(duration=1000)
            return intro_audio + silence + header + AudioSegment.silent(duration=500) + attn + silence + apply_radio_filter(user_audio) + silence + eom
        final_broadcast = await asyncio.to_thread(compile_broadcast)
        timestamp = datetime.now().strftime("%Y-%m-%d_%H-%M-%S")
        base_filename = f"{ARCHIVE_DIR}/pipe_broadcast_base_{timestamp}.mp3"
        await asyncio.to_thread(final_broadcast.export, base_filename, format="mp3")
        if os.path.exists(temp_input): os.remove(temp_input)
        if os.path.exists(intro_file): os.remove(intro_file)
        for guild_id_str, config in servers_db.items():
            guild = bot.get_guild(int(guild_id_str))
            if not guild: continue
            vc, _ = await get_or_connect_voice_client(guild, config)
            if not vc or not vc.is_connected() or vc.is_playing(): continue
            guild_filename = f"{ARCHIVE_DIR}/pipe_broadcast_{guild.id}_{timestamp}.mp3"
            shutil.copy(base_filename, guild_filename)
            text_id = config.get("text_channel_id")
            if text_id:
                text_channel = guild.get_channel(text_id)
                if text_channel:
                    embed = discord.Embed(title="🚨 Manual EAS Broadcast", description=f"**Location:** {config.get('uk_location', 'All Zones')}\n**Issued By:** Bot Owner", color=discord.Color.red())
                    embed.add_field(name="Details", value="An audio broadcast has been manually issued by the system administrator.", inline=False)
                    bot.loop.create_task(text_channel.send(content="🚨 **MANUAL BROADCAST** 🚨", embed=embed))
            play_err = await play_audio_file(vc, guild_filename)
            if play_err:
                print(f"Pipe playback skipped in guild {guild.id}: {play_err}")
        await ctx.send("📢 Global broadcast initiated.")
    except Exception as e: await ctx.send(f"❌ Failed: {e}")

@bot.command()
async def ping(ctx): await ctx.send(f'Pong! {round(bot.latency * 1000)}ms')

@bot.command()
async def active(ctx):
    guild_id_str = str(ctx.guild.id)
    recent = alert_history.get(guild_id_str, [])
    if not recent:
        await ctx.send("🟢 No active or recent manual RP alerts in this server.")
        return
    latest = recent[-1]
    await ctx.send(f"🔔 Most recent manual alert: **{latest['event']}** at {latest['time']}")

@bot.command(aliases=['silence'])
async def stop(ctx):
    if ctx.voice_client and ctx.voice_client.is_playing():
        ctx.voice_client.stop()
        await ctx.send("🔇 Stopped.")
    else: await ctx.send("No audio playing.")

@bot.command()
async def status(ctx):
    guild_id_str = str(ctx.guild.id)
    config = servers_db.get(guild_id_str, {})
    location = config.get("uk_location", "Not configured")
    selected_voice = config.get("voice_name", "System default")
    selected_prefix = config.get("command_prefix", DEFAULT_PREFIX)
    wind_unit = config.get("wind_unit", "kph")
    vc_status = f"Connected to '{ctx.voice_client.channel.name}'" if ctx.voice_client else "Not connected"
    await ctx.send(f"**EAS Bot Status**\n📡 **Mode:** Manual RP Alerts\n⌨️ **Command Prefix:** {selected_prefix}\n📍 **Default UK Location:** {location}\n🗣️ **Voice Name:** {selected_voice}\n💨 **Wind Speed Unit:** {wind_unit}\n🔊 **Voice Connection:** {vc_status}\n🏓 **Ping:** {round(bot.latency * 1000)}ms\n🌐 **Servers:** {len(servers_db)}")

@bot.command()
async def history(ctx):
    guild_id_str = str(ctx.guild.id)
    history_list = alert_history.get(guild_id_str, [])
    if not history_list:
        await ctx.send("No manual alert history for this server.")
        return
    message = "**Recent Manual Alert History:**\n"
    for alert in reversed(history_list[-5:]):
        message += f"- **{alert['event']}** at {alert['time']}\n"
    await ctx.send(message)


def weather_code_to_text(code):
    return UK_WEATHER_CODE_MAP.get(code, "mixed conditions")


def add_forecast_sounds(audio_file, intro_file=None, outro_file=None):
    from pydub import AudioSegment
    from pydub.generators import Sine

    base = AudioSegment.from_file(audio_file)

    if intro_file and os.path.exists(intro_file):
        pre = AudioSegment.from_file(intro_file)
    else:
        pre = Sine(1100).to_audio_segment(duration=220).apply_gain(-16)

    if outro_file and os.path.exists(outro_file):
        post = AudioSegment.from_file(outro_file)
    else:
        post = Sine(750).to_audio_segment(duration=260).apply_gain(-16)

    pre = pre + AudioSegment.silent(duration=130)
    post = AudioSegment.silent(duration=130) + post
    combined = pre + base + post
    combined.export(audio_file, format="mp3")


@bot.command()
async def weather(ctx, *, location_and_flags: str = None):
    if not await ensure_voice_runtime(ctx):
        return

    guild_id_str = str(ctx.guild.id)
    config = servers_db.get(guild_id_str, {})
    wind_unit = get_wind_unit(ctx.guild.id)
    wind_unit_label = "mph" if wind_unit == "mph" else "km/h"

    raw_query = location_and_flags or ""
    with_sounds = "--sounds" in raw_query.lower()
    location = raw_query.replace("--sounds", "").strip() if raw_query else ""
    if not location:
        location = config.get("uk_location", "")

    if not location:
        prefix = get_guild_prefix(ctx.guild.id) if ctx.guild else DEFAULT_PREFIX
        await ctx.send(f"Provide a UK location. Example: `{prefix}weather London --sounds` or run `{prefix}setup London`.")
        return

    await ctx.send(f"🔍 Fetching UK forecast for **{location}**...")

    try:
        geocode_url = "https://geocoding-api.open-meteo.com/v1/search"
        geo_res = requests.get(
            geocode_url,
            params={"name": location, "count": 5, "language": "en", "format": "json", "countryCode": "GB"},
            timeout=12,
        )
        if geo_res.status_code != 200:
            await ctx.send("❌ Failed to contact the UK geocoding service.")
            return
        geo_data = geo_res.json()
        results = geo_data.get("results", [])
        if not results:
            await ctx.send("❌ No UK location match found. Try a town/city name like `Leeds` or `Bristol`.")
            return

        best = results[0]
        lat = best.get("latitude")
        lon = best.get("longitude")
        resolved_name = ", ".join(
            p for p in [best.get("name"), best.get("admin1"), best.get("country")] if p
        )

        forecast_res = requests.get(
            "https://api.open-meteo.com/v1/forecast",
            params={
                "latitude": lat,
                "longitude": lon,
                "daily": "weather_code,temperature_2m_max,temperature_2m_min,precipitation_probability_max,wind_speed_10m_max",
                "timezone": "Europe/London",
                "forecast_days": 7,
            },
            timeout=12,
        )
        if forecast_res.status_code != 200:
            await ctx.send("❌ Failed to fetch weather forecast data.")
            return

        daily = forecast_res.json().get("daily", {})
        dates = daily.get("time", [])
        codes = daily.get("weather_code", [])
        highs = daily.get("temperature_2m_max", [])
        lows = daily.get("temperature_2m_min", [])
        rain = daily.get("precipitation_probability_max", [])
        wind = daily.get("wind_speed_10m_max", [])

        if not dates:
            await ctx.send("❌ Forecast response did not contain daily data.")
            return

        lines = []
        spoken_chunks = []
        for idx, day in enumerate(dates):
            condition = weather_code_to_text(codes[idx] if idx < len(codes) else -1)
            high = highs[idx] if idx < len(highs) else "?"
            low = lows[idx] if idx < len(lows) else "?"
            rain_chance = rain[idx] if idx < len(rain) else "?"
            max_wind_kph = wind[idx] if idx < len(wind) else "?"
            max_wind = convert_wind_speed(max_wind_kph, wind_unit)
            lines.append(
                f"**{day}** - {condition}; High {high} C, Low {low} C; Rain chance {rain_chance}%; Wind up to {max_wind} {wind_unit_label}"
            )
            spoken_chunks.append(
                f"{day}. {condition}. High {high} degrees. Low {low} degrees. Rain chance {rain_chance} percent. Wind up to {max_wind} {'miles per hour' if wind_unit == 'mph' else 'kilometers per hour'}."
            )

        embed = discord.Embed(
            title=f"🌦️ UK 7-Day Forecast: {resolved_name}",
            description="\n".join(lines)[:4000],
            color=discord.Color.blue(),
        )
        embed.set_footer(text="Source: Open-Meteo geocoding + forecast APIs")
        await ctx.send(embed=embed)

        if "uk_location" not in config:
            config["uk_location"] = resolved_name
            servers_db[guild_id_str] = config
            save_db(servers_db)

        fallback_channel = ctx.author.voice.channel if ctx.author.voice else None
        vc, err = await get_or_connect_voice_client(ctx.guild, config, fallback_channel=fallback_channel)

        if vc:
            filename = f"{ARCHIVE_DIR}/weather_{ctx.guild.id}_{datetime.now().strftime('%H%M%S')}.mp3"
            voice_name = config.get("voice_name")
            spoken_text = f"Seven day UK forecast for {resolved_name}. " + " ".join(spoken_chunks)
            await asyncio.to_thread(generate_normal_speech, spoken_text, filename, voice_name)
            if with_sounds:
                intro_file = config.get("weather_intro_file")
                outro_file = config.get("weather_outro_file")
                await asyncio.to_thread(add_forecast_sounds, filename, intro_file, outro_file)
            play_err = await play_audio_file(vc, filename)
            if play_err:
                await ctx.send(f"Forecast generated, but playback failed: {play_err}")
            else:
                await ctx.send("🔊 Reading forecast in voice channel.")
        else:
            await ctx.send(f"Forecast generated, but voice playback could not start: {err}")
    except Exception as e:
        print(f"Weather error: {e}")
        await ctx.send("Error fetching weather.")

@bot.command(aliases=['testg'])
@configured_owner_only()
async def testglobal(ctx): await trigger_global_test("Bot Owner")

@bot.command()
@configured_owner_only()
async def serverslist(ctx):
    if not servers_db: return await ctx.send("No servers.")
    msg = "**Servers:**\n"
    for gid, cfg in servers_db.items():
        msg += f"- **{cfg.get('guild_name')}**: Voice={cfg.get('voice_name', 'System default')} | Default UK Location={cfg.get('uk_location', 'Not set')}\n"
    await ctx.send(msg[:2000])

@bot.command()
@configured_owner_only()
async def freshpull(ctx):
    prefix = get_guild_prefix(ctx.guild.id) if ctx.guild else DEFAULT_PREFIX
    await ctx.send(f"Auto alert pulling is disabled in RP mode. Use `{prefix}customalert` for manual broadcasts.")

@bot.command()
@configured_owner_only()
async def shutdown(ctx): await ctx.send("🛑 Closing..."); await bot.close()

@bot.command()
@configured_owner_only()
async def restart(ctx): await ctx.send("🔄 Restarting..."); await bot.close(); os._exit(0)

@bot.command()
@configured_owner_only()
async def getlogs(ctx):
    await ctx.send("Logs:")
    files = [discord.File(f) for f in ["logs/bot.log", "logs/bot_errors.log"] if os.path.exists(f)]
    if files: await ctx.send(files=files)
    else: await ctx.send("No logs.")

if __name__ == '__main__':
    if TOKEN: bot.run(TOKEN)
    else: print("No TOKEN.")
