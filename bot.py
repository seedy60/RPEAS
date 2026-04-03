import discord
from discord.ext import commands, tasks
import os
import requests
import asyncio
import re
import json
import pytz
import aiohttp
from aiohttp import web
import aiohttp_session
from aiohttp_session.cookie_storage import EncryptedCookieStorage
import base64
from cryptography import fernet
from urllib.parse import urlencode
from dotenv import load_dotenv
from eas_audio import generate_eas_message, generate_normal_speech
from datetime import datetime
import sys

# Load configuration from .env
load_dotenv()
TOKEN = os.getenv('DISCORD_TOKEN')
BOT_VERSION = "1.0"

# JSON Database Setup
DB_FILE = "servers.json"

# Define the archive directory outside of the bot folder
ARCHIVE_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "alerts_archive"))

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

# Configure bot
intents = discord.Intents.default()
intents.message_content = True
intents.voice_states = True

bot = commands.Bot(command_prefix='fco!', intents=intents, help_command=None)

# --- State Variables ---
seen_alerts = set()
active_alerts_cache = {} # Dict of zone -> list of alerts
alert_history = {}       # Dict of zone -> list of alerts

# Life-Threatening Alerts that will trigger a @everyone ping
URGENT_EVENTS = [
    "Tornado Warning",
    "Flash Flood Warning",
    "Severe Thunderstorm Warning",
    "Tsunami Warning",
    "Civil Emergency Message",
    "Evacuation Immediate",
    "Shelter in Place Warning",
    "AMBER Alert",
    "Nuclear Power Plant Warning",
    "Hazardous Materials Warning",
    "Fire Warning"
]

# --- Web Server (ENDEC Dashboard) ---

# Replace with your actual Discord User ID
BOT_OWNER_ID = "1365401272798281850"

# aiohttp_session EncryptedCookieStorage requires exactly 32 raw bytes.
WEB_SESSION_KEY = os.urandom(32)

async def discord_login(request):
    """Redirects the user to Discord's OAuth2 login page."""
    client_id = os.getenv("DISCORD_CLIENT_ID")
    redirect_uri = os.getenv("REDIRECT_URI")
    if not client_id or not redirect_uri:
        return web.Response(status=500, text="OAuth2 not configured in .env file.")
        
    oauth_url = f"https://discord.com/api/oauth2/authorize?client_id={client_id}&redirect_uri={redirect_uri}&response_type=code&scope=identify"
    raise web.HTTPFound(oauth_url)

async def discord_callback(request):
    """Handles the callback from Discord, gets the user ID, and sets the session."""
    code = request.query.get("code")
    if not code:
        return web.Response(status=400, text="Missing authorization code.")
        
    client_id = os.getenv("DISCORD_CLIENT_ID")
    client_secret = os.getenv("DISCORD_CLIENT_SECRET")
    redirect_uri = os.getenv("REDIRECT_URI")
    
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
            
    if user_id != BOT_OWNER_ID:
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
                <p><strong>API Polling:</strong> {'Active' if check_nws_alerts.is_running() else 'Inactive'}</p>
            </section>
            <section class="panel" aria-labelledby="controls">
                <h2 id="controls">Manual Trigger Controls</h2>
                <form action="/test" method="post" style="display:inline;">
                    <button type="submit" class="btn">Trigger Global Test</button>
                </form>
                <form action="/stop" method="post" style="display:inline;">
                    <button type="submit" class="btn">Stop All Audio</button>
                </form>
                <form action="/poll" method="post" style="display:inline;">
                    <button type="submit" class="btn">Force API Poll</button>
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
            vc = guild.voice_client
            if not vc or not vc.is_connected():
                vc_id = config.get("voice_channel_id")
                if vc_id:
                    channel = guild.get_channel(vc_id)
                    if channel:
                        try: vc = await channel.connect(self_deaf=True)
                        except: continue
            if not vc or not vc.is_connected() or vc.is_playing(): continue
            guild_filename = f"{ARCHIVE_DIR}/global_test_{guild.id}_{timestamp}.mp3"
            shutil.copy(base_filename, guild_filename)
            text_id = config.get("text_channel_id")
            if text_id:
                text_channel = guild.get_channel(text_id)
                if text_channel:
                    embed = discord.Embed(title="🚨 Global Test Alert", description=f"**Location:** {config.get('place_name', 'All Zones')}\n**Issued By:** {trigger_source}", color=discord.Color.blue())
                    embed.add_field(name="Headline", value="This is a global test of the Emergency Alert System.", inline=False)
                    embed.add_field(name="Details", value=main_text, inline=False)
                    bot.loop.create_task(text_channel.send(content="🚨 **GLOBAL TEST ALERT** 🚨", embed=embed))
            vc.play(discord.FFmpegPCMAudio(source=guild_filename))
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
    bot.loop.create_task(check_nws_alerts())
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
    if not os.path.exists(ARCHIVE_DIR):
        os.makedirs(ARCHIVE_DIR)
        print(f"Created archive folder at {ARCHIVE_DIR}")
    bot.loop.create_task(start_web_server())
    for guild_id_str, config in servers_db.items():
        vc_id = config.get("voice_channel_id")
        if vc_id:
            channel = bot.get_channel(vc_id)
            if channel and isinstance(channel, discord.VoiceChannel):
                try: await channel.connect(self_deaf=True)
                except Exception as e: print(f"Failed to auto-join VC {vc_id}: {e}")
    if not check_nws_alerts.is_running():
        check_nws_alerts.start()

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
    elif isinstance(error, commands.CommandNotFound): pass
    else: print(f"Command error in {ctx.command}: {error}")

@bot.command()
async def help(ctx):
    embed = discord.Embed(title="📡 EAS Bot Help & Commands", description=f"A professional Emergency Alert System (EAS) relay bot. **Version {BOT_VERSION}**", color=discord.Color.blue())
    embed.add_field(name="🎙️ General Commands", value="`fco!join`, `fco!leave`, `fco!active`, `fco!history`, `fco!weather [ZIP]`, `fco!stop`, `fco!status`", inline=False)
    embed.add_field(name="⚙️ Admin Commands", value="`fco!setup <ZIP>`, `fco!test`", inline=False)
    if await bot.is_owner(ctx.author):
        embed.add_field(name="👑 Owner Commands", value="`fco!testg`, `fco!pipe`, `fco!serverslist`, `fco!freshpull`, `fco!restart`, `fco!shutdown`, `fco!getlogs`", inline=False)
    await ctx.send(embed=embed)

@bot.command()
@commands.has_permissions(administrator=True)
async def setup(ctx, zip_code: str = None):
    if not zip_code or not zip_code.isdigit() or len(zip_code) != 5:
        await ctx.send("Please provide a valid 5-digit US ZIP Code. Example: `fco!setup 81240`")
        return
    await ctx.send(f"🔍 Looking up zone information for ZIP Code {zip_code}...")
    headers = {"User-Agent": "EASDiscordBot/1.0"}
    try:
        zip_res = requests.get(f"http://api.zippopotam.us/us/{zip_code}", timeout=10)
        if zip_res.status_code != 200:
            await ctx.send("❌ Could not find that ZIP Code.")
            return
        zip_data = zip_res.json()
        lat, lon = zip_data['places'][0]['latitude'], zip_data['places'][0]['longitude']
        place_name = f"{zip_data['places'][0]['place name']}, {zip_data['places'][0]['state abbreviation']}"
        points_res = requests.get(f"https://api.weather.gov/points/{lat},{lon}", headers=headers, timeout=10)
        if points_res.status_code != 200:
            await ctx.send("❌ Failed to contact NWS API.")
            return
        points_data = points_res.json()
        county_url = points_data.get('properties', {}).get('county')
        if not county_url:
            await ctx.send("❌ Could not determine NWS Zone.")
            return
        zone_id = county_url.split('/')[-1]
    except Exception as e:
        await ctx.send(f"❌ Error during lookup: {e}")
        return
    if not ctx.author.voice:
        await ctx.send(f"⚠️ I found the zone for {place_name} (`{zone_id}`), but you must be in a voice channel to finish setup!")
        return
    vc_id, text_id = ctx.author.voice.channel.id, ctx.channel.id
    servers_db[str(ctx.guild.id)] = {"zone": zone_id, "place_name": place_name, "text_channel_id": text_id, "voice_channel_id": vc_id, "guild_name": ctx.guild.name, "zip_code": zip_code}
    save_db(servers_db)
    if ctx.voice_client: await ctx.voice_client.move_to(ctx.author.voice.channel)
    else: await ctx.author.voice.channel.connect(self_deaf=True)
    await ctx.send(f"✅ **Setup Complete!**\n📍 **Location:** {place_name}\n📡 **Monitoring Zone:** `{zone_id}`\n🔊 **VC:** <#{vc_id}>\n💬 **Text:** <#{text_id}>")

@bot.command()
async def join(ctx):
    if ctx.author.voice:
        if ctx.voice_client: await ctx.voice_client.move_to(ctx.author.voice.channel)
        else: await ctx.author.voice.channel.connect(self_deaf=True)
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
    guild_id_str = str(ctx.guild.id)
    config = servers_db.get(guild_id_str, {})
    zone = config.get("zone", "COC043")
    vc = ctx.voice_client
    if not vc:
        vc_id = config.get("voice_channel_id")
        if vc_id:
            channel = ctx.guild.get_channel(vc_id)
            if channel:
                try: vc = await channel.connect(self_deaf=True)
                except Exception as e: await ctx.send(f"Failed to auto-join VC: {e}")
    if not vc:
        await ctx.send("I need to be in a voice channel first.")
        return
    if vc.is_playing():
        await ctx.send("Audio is already playing.")
        return
    await ctx.send("Generating test EAS message...")
    text = f"This is a test of the Emergency Alert System for zone {zone}. This is only a test."
    try:
        text_id = config.get("text_channel_id")
        if text_id:
            text_channel = ctx.guild.get_channel(text_id)
            if text_channel:
                embed = discord.Embed(title="🚨 Required Monthly Test", description=f"**Location:** {config.get('place_name', zone)}\n**Issued By:** Bot Admin", color=discord.Color.blue())
                embed.add_field(name="Details", value=text, inline=False)
                await text_channel.send(content="🚨 **TEST ALERT** 🚨", embed=embed)
        timestamp = datetime.now().strftime("%Y-%m-%d_%H-%M-%S")
        filename = f"{ARCHIVE_DIR}/test_alert_{ctx.guild.id}_{timestamp}.mp3"
        intro = "This voice channel has been interrupted in order to partissipate in the emergency alert system."
        await asyncio.to_thread(generate_eas_message, text, filename, intro)
        vc.play(discord.FFmpegPCMAudio(source=filename))
        await ctx.send("Now playing the test message.")
    except Exception as e: await ctx.send(f"Failed to play audio: {e}")

@bot.command()
@commands.is_owner()
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
            vc = guild.voice_client
            if not vc or not vc.is_connected():
                vc_id = config.get("voice_channel_id")
                if vc_id:
                    channel = guild.get_channel(vc_id)
                    if channel:
                        try: vc = await channel.connect(self_deaf=True)
                        except: continue
            if not vc or not vc.is_connected() or vc.is_playing(): continue
            guild_filename = f"{ARCHIVE_DIR}/pipe_broadcast_{guild.id}_{timestamp}.mp3"
            shutil.copy(base_filename, guild_filename)
            text_id = config.get("text_channel_id")
            if text_id:
                text_channel = guild.get_channel(text_id)
                if text_channel:
                    embed = discord.Embed(title="🚨 Manual EAS Broadcast", description=f"**Location:** {config.get('place_name', 'All Zones')}\n**Issued By:** Bot Owner", color=discord.Color.red())
                    embed.add_field(name="Details", value="An audio broadcast has been manually issued by the system administrator.", inline=False)
                    bot.loop.create_task(text_channel.send(content="🚨 **MANUAL BROADCAST** 🚨", embed=embed))
            vc.play(discord.FFmpegPCMAudio(source=guild_filename))
        await ctx.send("📢 Global broadcast initiated.")
    except Exception as e: await ctx.send(f"❌ Failed: {e}")

@bot.command()
async def ping(ctx): await ctx.send(f'Pong! {round(bot.latency * 1000)}ms')

@bot.command()
async def active(ctx):
    guild_id_str = str(ctx.guild.id)
    if guild_id_str not in servers_db:
        await ctx.send("Run `fco!setup <ZIP>`.")
        return
    zone = servers_db[guild_id_str]["zone"]
    zone_alerts = active_alerts_cache.get(zone, [])
    if not zone_alerts:
        await ctx.send(f"🟢 No active alerts for {zone}.")
        return
    message = f"**Active Alerts for {zone}:**\n"
    for alert in zone_alerts:
        message += f"⚠️ **{alert['event']}** ({alert['sender']})\n> {alert['headline']}\n\n"
    await ctx.send(message[:2000])

@bot.command(aliases=['silence'])
async def stop(ctx):
    if ctx.voice_client and ctx.voice_client.is_playing():
        ctx.voice_client.stop()
        await ctx.send("🔇 Stopped.")
    else: await ctx.send("No audio playing.")

@bot.command()
async def status(ctx):
    guild_id_str = str(ctx.guild.id)
    zone = servers_db.get(guild_id_str, {}).get("zone", "Not configured")
    vc_status = f"Connected to '{ctx.voice_client.channel.name}'" if ctx.voice_client else "Not connected"
    await ctx.send(f"**EAS Bot Status**\n📡 **Zone:** `{zone}`\n🔊 **Voice:** {vc_status}\n⏱️ **API:** Active\n🏓 **Ping:** {round(bot.latency * 1000)}ms\n🌐 **Servers:** {len(servers_db)}")

@bot.command()
async def history(ctx):
    guild_id_str = str(ctx.guild.id)
    if guild_id_str not in servers_db: return
    zone = servers_db[guild_id_str]["zone"]
    history_list = alert_history.get(zone, [])
    if not history_list:
        await ctx.send(f"No history for {zone}.")
        return
    message = f"**Recent Alert History for {zone}:**\n"
    for alert in reversed(history_list[-5:]):
        message += f"- **{alert['event']}** at {alert['time']}\n"
    await ctx.send(message)

@bot.command()
async def weather(ctx, target_zip: str = None):
    guild_id_str = str(ctx.guild.id)
    config = servers_db.get(guild_id_str, {})
    zip_to_use = target_zip or config.get("zip_code")
    if not zip_to_use:
        await ctx.send("Provide a ZIP or run `fco!setup`.")
        return
    await ctx.send(f"🔍 Fetching detailed 7-day forecast for {zip_to_use}... 📡")
    headers = {"User-Agent": "EASDiscordBot/1.0"}
    try:
        zip_res = requests.get(f"http://api.zippopotam.us/us/{zip_to_use}", timeout=10)
        if zip_res.status_code != 200:
            await ctx.send("❌ ZIP not found.")
            return
        zip_data = zip_res.json()
        lat, lon = zip_data['places'][0]['latitude'], zip_data['places'][0]['longitude']
        place_name = f"{zip_data['places'][0]['place name']}, {zip_data['places'][0]['state abbreviation']}"
        points_res = requests.get(f"https://api.weather.gov/points/{lat},{lon}", headers=headers, timeout=10)
        points_data = points_res.json()
        forecast_url = points_data.get('properties', {}).get('forecast')
        cwa = points_data.get('properties', {}).get('cwa')
        hwo_url = f"https://api.weather.gov/products/types/HWO/locations/{cwa}"
        forecast_text_blocks = []
        spoken_text = f"Detailed forecast for {place_name}. "
        res_forecast = requests.get(forecast_url, headers=headers, timeout=10)
        if res_forecast.status_code == 200:
            periods = res_forecast.json().get('properties', {}).get('periods', [])
            current_block = ""
            for period in periods:
                line = f"**{period.get('name')}**: {period.get('detailedForecast')}\n"
                if len(current_block) + len(line) > 1800:
                    forecast_text_blocks.append(current_block); current_block = line
                else: current_block += line
                spoken_text += f"{period.get('name')}. {period.get('detailedForecast')} "
            if current_block: forecast_text_blocks.append(current_block)
        res_hwo_list = requests.get(hwo_url, headers=headers, timeout=10)
        def parse_hwo_text(t):
            m = re.search(r'(This hazardous weather outlook|\.DAY ONE.*?|DISCUSSION\.\.\.)', t, re.I)
            if m:
                p = re.split(r'\.SPOTTER|\$\$|\&\&', t[m.start():], flags=re.I)[0].strip()
                return re.sub(r'\s+', ' ', p.replace('\n', ' ').replace('*', ''))
            return None
        hwo_found, hwo_summary = False, ""
        if res_hwo_list.status_code == 200 and res_hwo_list.json().get('@graph'):
            url = res_hwo_list.json()['@graph'][0]['@id']
            res_hwo = requests.get(url, headers=headers, timeout=10)
            parsed = parse_hwo_text(res_hwo.json().get('productText', ''))
            if parsed: hwo_summary = parsed; spoken_text += " Hazardous Weather Outlook. " + parsed; hwo_found = True
        if not hwo_found and cwa:
            try:
                from bs4 import BeautifulSoup
                scrape_res = requests.get(f"https://www.weather.gov/{cwa.lower()}/ghwo", headers=headers, timeout=10)
                if scrape_res.status_code == 200:
                    soup = BeautifulSoup(scrape_res.content, 'html.parser')
                    for table in soup.find_all('table'):
                        parsed = parse_hwo_text(table.get_text())
                        if parsed: hwo_summary = parsed; spoken_text += " Hazardous Weather Outlook. " + parsed; hwo_found = True; break
            except: pass
        if not hwo_found:
            afd_res = requests.get(f"https://api.weather.gov/products/types/AFD/locations/{cwa}", headers=headers, timeout=10)
            if afd_res.status_code == 200 and afd_res.json().get('@graph'):
                url = afd_res.json()['@graph'][0]['@id']
                res_afd = requests.get(url, headers=headers, timeout=10)
                m = re.search(r'\.(?:KEY MESSAGES|SYNOPSIS)\.\.\.(.*?)\&\&', res_afd.json().get('productText', ''), re.S | re.I)
                if m:
                    hwo_summary = re.sub(r'\s+', ' ', m.group(1).replace('\n', ' ').replace('*', '').replace('-', ''))
                    hwo_summary = re.sub(r'(?:Updated|Revised) at.*?\d{4}', '', hwo_summary, flags=re.I).strip()
                    spoken_text += " Regional Weather Summary. " + hwo_summary; hwo_found = True
        if not hwo_found: hwo_summary = "No outlook active."
        spoken_text += " For the latest information, go to weather.gov."
        for i, block in enumerate(forecast_text_blocks):
            embed = discord.Embed(title=f"🌤️ 7-Day Forecast: {place_name} ({i+1}/{len(forecast_text_blocks)})", description=block, color=discord.Color.blue())
            if i == len(forecast_text_blocks) - 1: embed.add_field(name="⚠️ Outlook", value=hwo_summary[:1024], inline=False)
            await ctx.send(embed=embed)
            if i < len(forecast_text_blocks) - 1: await asyncio.sleep(10)
        if ctx.voice_client and not ctx.voice_client.is_playing():
            filename = f"{ARCHIVE_DIR}/weather_{ctx.guild.id}_{datetime.now().strftime('%H%M%S')}.mp3"
            await asyncio.to_thread(generate_normal_speech, spoken_text, filename)
            ctx.voice_client.play(discord.FFmpegPCMAudio(source=filename))
    except Exception as e: print(f"Weather error: {e}"); await ctx.send("Error fetching weather.")

@bot.command(aliases=['testg'])
@commands.is_owner()
async def testglobal(ctx): await trigger_global_test("Bot Owner")

@bot.command()
@commands.is_owner()
async def serverslist(ctx):
    if not servers_db: return await ctx.send("No servers.")
    msg = "**Servers:**\n"
    for gid, cfg in servers_db.items(): msg += f"- **{cfg.get('guild_name')}**: {cfg.get('zone')}\n"
    await ctx.send(msg[:2000])

@bot.command()
@commands.is_owner()
async def freshpull(ctx):
    await ctx.send("🔄 Pulling...")
    await check_nws_alerts()
    await ctx.send("✅ Done.")

@bot.command()
@commands.is_owner()
async def shutdown(ctx): await ctx.send("🛑 Closing..."); await bot.close()

@bot.command()
@commands.is_owner()
async def restart(ctx): await ctx.send("🔄 Restarting..."); await bot.close(); os._exit(0)

@bot.command()
@commands.is_owner()
async def getlogs(ctx):
    await ctx.send("Logs:")
    files = [discord.File(f) for f in ["logs/bot.log", "logs/bot_errors.log"] if os.path.exists(f)]
    if files: await ctx.send(files=files)
    else: await ctx.send("No logs.")

last_weekly_test_date = None

@tasks.loop(minutes=2.0)
async def check_nws_alerts():
    global active_alerts_cache, alert_history, last_weekly_test_date
    try:
        now_mdt = datetime.now(pytz.timezone("US/Mountain"))
        if now_mdt.weekday() == 2 and now_mdt.hour == 9 and now_mdt.minute >= 30:
            if last_weekly_test_date != now_mdt.strftime("%Y-%m-%d"):
                last_weekly_test_date = now_mdt.strftime("%Y-%m-%d")
                await trigger_global_test("Automated System")
    except Exception as e: print(f"Weekly test error: {e}")
    unique_zones = set(cfg["zone"] for cfg in servers_db.values())
    if not unique_zones: return
    headers = {"User-Agent": "EASDiscordBot/1.0", "Accept": "application/geo+json"}
    async with aiohttp.ClientSession() as session:
        for zone in unique_zones:
            try:
                async with session.get(f"https://api.weather.gov/alerts/active?zone={zone}", headers=headers, timeout=15) as resp:
                    if resp.status == 200:
                        data = await resp.json()
                        new_cache, new_alerts = [], []
                        for f in data.get('features', []):
                            p = f.get('properties', {})
                            aid, ev, sname, hline, desc, inst, sev, adesc = p.get('id'), p.get('event', 'Alert'), p.get('senderName', 'NWS'), p.get('headline', ''), p.get('description', ''), p.get('instruction', ''), p.get('severity', 'Unknown'), p.get('areaDesc', '')
                            new_cache.append({"event": ev, "sender": sname, "headline": hline})
                            if aid and aid not in seen_alerts:
                                seen_alerts.add(aid)
                                if zone not in alert_history: alert_history[zone] = []
                                alert_history[zone].append({"event": ev, "time": datetime.now().strftime("%I:%M %p")})
                                new_alerts.append((aid, sname, hline, desc, inst, sev, adesc, ev))
                        active_alerts_cache[zone] = new_cache
                        for gid, cfg in servers_db.items():
                            if cfg.get("zone") == zone:
                                guild = bot.get_guild(int(gid))
                                if not guild: continue
                                tid = cfg.get("text_channel_id")
                                if tid:
                                    tchan = guild.get_channel(tid)
                                    if tchan:
                                        for aid, sname, hline, desc, inst, sev, adesc, ev in new_alerts:
                                            color = discord.Color.red() if sev in ["Extreme", "Severe"] else discord.Color.gold()
                                            embed = discord.Embed(title=f"🚨 {ev}", description=f"**Location:** {cfg.get('place_name', zone)}\n**Affected:** {adesc.replace(';', ',')}", color=color)
                                            embed.add_field(name="Headline", value=hline, inline=False)
                                            if desc: embed.add_field(name="Details", value=desc[:1020], inline=False)
                                            ping = "@everyone " if ev in URGENT_EVENTS else ""
                                            bot.loop.create_task(tchan.send(content=f"{ping}🚨 **NEW ALERT** 🚨", embed=embed))
                                vc = guild.voice_client
                                if vc and vc.is_connected() and not vc.is_playing() and new_alerts:
                                    aid, sname, hline, desc, inst, sev, adesc, ev = new_alerts[0]
                                    speech = f"Transmitted at request of {sname}. {hline}. {desc.replace('\n', ' ')} Please note: {inst.replace('\n', ' ')}"
                                    fname = f"{ARCHIVE_DIR}/alert_{aid.split('.')[-1]}_{gid}_{datetime.now().strftime('%H%M%S')}.mp3"
                                    await asyncio.to_thread(generate_eas_message, speech, fname, "This voice channel has been interrupted for the Emergency Alert System.")
                                    vc.play(discord.FFmpegPCMAudio(source=fname))
            except Exception as e: print(f"API Error {zone}: {e}")
    for gid, cfg in servers_db.items():
        guild = bot.get_guild(int(gid))
        if guild:
            vc, vcid = guild.voice_client, cfg.get("voice_channel_id")
            if vcid:
                chan = guild.get_channel(vcid)
                if chan and (not vc or not vc.is_connected()):
                    try:
                        if vc: await vc.disconnect(force=True)
                        await chan.connect(self_deaf=True, reconnect=True, timeout=15.0)
                    except: pass

@check_nws_alerts.before_loop
async def before_check_nws_alerts(): await bot.wait_until_ready()

if __name__ == '__main__':
    if TOKEN: bot.run(TOKEN)
    else: print("No TOKEN.")
