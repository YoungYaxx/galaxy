import discord
from discord.ext import commands
from discord import app_commands
import json
import random
import os
import sqlite3
import aiohttp
from datetime import datetime, timedelta
import pytz

# --- CARICAMENTO E CONFIGURAZIONE INIZIALE ---

# Rende i percorsi dei file relativi alla posizione dello script
script_dir = os.path.dirname(os.path.abspath(__file__))
config_path = os.path.join(os.path.dirname(script_dir), 'config.json')

# Path del database per Render (o locale se non su Render)
render_data_path = '/var/data/render/data.db'
local_data_path = os.path.join(script_dir, 'data.db')
db_path = render_data_path if os.path.exists('/var/data/render') else local_data_path


# Carica la configurazione del token
with open(config_path, 'r') as f:
    config = json.load(f)

# Carica i file di lingua
languages = {}
for filename in os.listdir(script_dir):
    if filename.endswith('.json') and (filename == 'it.json' or filename == 'en.json'):
        lang_code = filename.split('.')[0]
        filepath = os.path.join(script_dir, filename)
        with open(filepath, 'r', encoding='utf-8') as f:
            languages[lang_code] = json.load(f)

# Imposta gli intents del bot
intents = discord.Intents.default()
intents.message_content = True
intents.members = True

# Crea l'istanza del bot, disabilitando il comando help predefinito
bot = commands.Bot(command_prefix='/', intents=intents, help_command=None)

@bot.before_invoke
async def before_any_command(ctx: commands.Context):
    """Check globale eseguito prima di ogni comando."""
    # Controlla lo stato di manutenzione
    cursor.execute("SELECT maintenance_mode FROM bot_status WHERE id = 1")
    maintenance_on = cursor.fetchone()['maintenance_mode']
    
    bot_owner_id = config.get('bot_owner_id')
    
    # Se la manutenzione è attiva e l'utente non è il proprietario, blocca il comando
    if maintenance_on and str(ctx.author.id) != str(bot_owner_id):
        await ctx.send("Il bot è attualmente in manutenzione. Riprova più tardi.", ephemeral=True)
        raise commands.CommandError("Bot in maintenance mode.")


# --- DATABASE ---

# Connessione al database
conn = sqlite3.connect(db_path)
cursor = conn.cursor()

# Crea le tabelle del database se non esistono
cursor.execute('''
CREATE TABLE IF NOT EXISTS guild_settings (
    guild_id INTEGER PRIMARY KEY,
    language TEXT DEFAULT 'it',
    log_channel_id INTEGER,
    staff_role_id INTEGER,
    timezone TEXT DEFAULT 'UTC'
)
''')
cursor.execute('''
CREATE TABLE IF NOT EXISTS warnings (
    warn_id INTEGER PRIMARY KEY AUTOINCREMENT,
    guild_id INTEGER NOT NULL,
    user_id INTEGER NOT NULL,
    moderator_id INTEGER NOT NULL,
    reason TEXT,
    timestamp DATETIME DEFAULT CURRENT_TIMESTAMP
)
''')
# Tabella per lo stato globale del bot
cursor.execute('''
CREATE TABLE IF NOT EXISTS bot_status (
    id INTEGER PRIMARY KEY CHECK (id = 1),
    maintenance_mode INTEGER NOT NULL DEFAULT 0
)
''')
# Assicura che la riga esista
cursor.execute("INSERT OR IGNORE INTO bot_status (id) VALUES (1)")
conn.commit()

# --- FUNZIONI HELPER PER LA LINGUA E LOG ---

def get_guild_lang(guild_id: int) -> str:
    """Ottiene la lingua per un dato server, default 'it'."""
    cursor.execute("SELECT language FROM guild_settings WHERE guild_id = ?", (guild_id,))
    result = cursor.fetchone()
    if result:
        return result[0]
    return 'it'

def t(guild_id: int, key: str, **kwargs):
    """
    Ottiene un valore tradotto per il server dato.
    Se il valore è una stringa, la formatta con kwargs.
    Altrimenti, restituisce il valore così com'è (es. una lista).
    """
    lang = get_guild_lang(guild_id)
    value = languages.get(lang, languages.get('it', {})).get(key, key)
    
    if isinstance(value, str):
        return value.format(**kwargs)
    
    return value

# --- FUNZIONE DI CONTROLLO PERMESSI STAFF ---
async def is_staff_or_admin(interaction: discord.Interaction) -> bool:
    """Controlla se l'utente è admin o ha il ruolo staff configurato."""
    if interaction.user.guild_permissions.administrator:
        return True
    
    cursor.execute("SELECT staff_role_id FROM guild_settings WHERE guild_id = ?", (interaction.guild_id,))
    result = cursor.fetchone()
    if result and result[0]:
        staff_role = interaction.guild.get_role(result[0])
        if staff_role and staff_role in interaction.user.roles:
            return True
            
    # Se il check fallisce, invia un messaggio di errore
    guild_id = interaction.guild_id
    embed = discord.Embed(
        title=t(guild_id, 'perms_error_title'),
        description=t(guild_id, 'perms_error_desc'),
        color=discord.Color.red()
    )
    # Usa followup se la risposta è stata differita, altrimenti rispondi
    if interaction.response.is_done():
        await interaction.followup.send(embed=embed, ephemeral=True)
    else:
        await interaction.response.send_message(embed=embed, ephemeral=True)
    return False

# --- EVENTI DEL BOT ---

@bot.event
async def on_ready():
    print(f'Bot connesso come {bot.user}')
    try:
        synced = await bot.tree.sync()
        print(f"Sincronizzati {len(synced)} comandi slash.")
    except Exception as e:
        print(f"Errore durante la sincronizzazione dei comandi: {e}")

# --- COMANDO HELP INTERATTIVO ---

class HelpView(discord.ui.View):
    def __init__(self, guild_id):
        super().__init__(timeout=180) # Timeout di 3 minuti
        self.guild_id = guild_id
        self.add_item(HelpSelect(guild_id))

class HelpSelect(discord.ui.Select):
    def __init__(self, guild_id: int):
        options = [
            discord.SelectOption(label=t(guild_id, 'help_category_fun'), description=t(guild_id, 'help_category_fun_desc'), value="fun", emoji="🎉"),
            discord.SelectOption(label=t(guild_id, 'help_category_mod'), description=t(guild_id, 'help_category_mod_desc'), value="mod", emoji="🛡️"),
            discord.SelectOption(label=t(guild_id, 'help_category_config'), description=t(guild_id, 'help_category_config_desc'), value="config", emoji="⚙️"),
        ]
        super().__init__(placeholder=t(guild_id, 'help_select_placeholder'), min_values=1, max_values=1, options=options)

    async def callback(self, interaction: discord.Interaction):
        category = self.values[0]
        guild_id = interaction.guild_id
        
        embed = discord.Embed(
            title=t(guild_id, 'help_commands_title', category=category.capitalize()),
            color=interaction.client.user.color
        )
        
        # Logica per trovare i comandi di quella categoria
        if category == "fun":
            # Comandi globali che non sono in un gruppo
            cmds = [c for c in bot.tree.get_commands() if c.parent is None and c.name not in ["help", "mod", "config"]]
            for cmd in cmds:
                embed.add_field(name=f"/{cmd.name}", value=cmd.description, inline=False)
        elif category == "mod":
            # Comandi nel gruppo 'mod'
            for cmd in mod_group.commands:
                embed.add_field(name=f"/{mod_group.name} {cmd.name}", value=cmd.description, inline=False)
        elif category == "config":
            # Comandi nel gruppo 'config'
            cmds = [c for c in bot.tree.get_commands() if c.name == "config"]
            for cmd in cmds:
                 embed.add_field(name=f"/{cmd.name}", value=cmd.description, inline=False)

        await interaction.response.edit_message(embed=embed)


@bot.tree.command(name="help", description="Mostra il pannello di aiuto interattivo.")
async def help_command(interaction: discord.Interaction):
    guild_id = interaction.guild_id
    embed = discord.Embed(
        title=t(guild_id, 'help_title'),
        description=t(guild_id, 'help_description'),
        color=bot.user.color
    )
    embed.set_image(url=t(guild_id, 'help_image_url'))
    view = HelpView(guild_id)
    await interaction.response.send_message(embed=embed, view=view, ephemeral=True)


# --- PANNELLO DI CONFIGURAZIONE (FINALE E STABILE) ---

# Modal per Canale Log
class SetChannelModal(discord.ui.Modal, title="Imposta Canale Log"):
    channel_id_input = discord.ui.TextInput(label="ID del Canale", placeholder="Incolla qui l'ID del canale testuale...")

    async def on_submit(self, interaction: discord.Interaction):
        guild_id = interaction.guild_id
        try:
            channel = interaction.guild.get_channel(int(self.channel_id_input.value))
            if channel and isinstance(channel, discord.TextChannel):
                cursor.execute("UPDATE guild_settings SET log_channel_id = ? WHERE guild_id = ?", (channel.id, guild_id))
                conn.commit()
                await interaction.response.send_message(t(guild_id, 'config_log_channel_success', channel=channel.mention), ephemeral=True)
            else:
                await interaction.response.send_message(t(guild_id, 'modal_error_invalid_id'), ephemeral=True)
        except (ValueError, TypeError):
            await interaction.response.send_message(t(guild_id, 'modal_error_invalid_id'), ephemeral=True)

# Modal per Ruolo Staff
class SetRoleModal(discord.ui.Modal, title="Imposta Ruolo Staff"):
    role_id_input = discord.ui.TextInput(label="ID del Ruolo", placeholder="Incolla qui l'ID del ruolo...")

    async def on_submit(self, interaction: discord.Interaction):
        guild_id = interaction.guild_id
        try:
            role = interaction.guild.get_role(int(self.role_id_input.value))
            if role:
                cursor.execute("UPDATE guild_settings SET staff_role_id = ? WHERE guild_id = ?", (role.id, guild_id))
                conn.commit()
                await interaction.response.send_message(t(guild_id, 'config_set_staff_role_success', role=role.mention), ephemeral=True)
            else:
                await interaction.response.send_message(t(guild_id, 'modal_error_invalid_id'), ephemeral=True)
        except (ValueError, TypeError):
            await interaction.response.send_message(t(guild_id, 'modal_error_invalid_id'), ephemeral=True)

# View per la selezione (Lingua e Timezone)
class SelectView(discord.ui.View):
    def __init__(self, guild_id, select: discord.ui.Select):
        super().__init__(timeout=180)
        self.guild_id = guild_id
        self.add_item(select)

# Select specifici
class LanguageSelect(discord.ui.Select):
    def __init__(self, guild_id):
        options = [
            discord.SelectOption(label="Italiano", value="it", emoji="🇮🇹"),
            discord.SelectOption(label="English", value="en", emoji="🇬🇧")
        ]
        super().__init__(placeholder="Scegli una lingua...", options=options)
    async def callback(self, interaction: discord.Interaction):
        cursor.execute("UPDATE guild_settings SET language = ? WHERE guild_id = ?", (self.values[0], interaction.guild_id))
        conn.commit()
        await interaction.response.send_message(t(interaction.guild_id, 'config_lang_success'), ephemeral=True)

class TimezoneSelect(discord.ui.Select):
    def __init__(self, guild_id):
        options = [discord.SelectOption(label=tz) for tz in ['UTC', 'Europe/London', 'Europe/Rome', 'Europe/Paris', 'America/New_York']]
        super().__init__(placeholder="Scegli un fuso orario...", options=options)
    async def callback(self, interaction: discord.Interaction):
        cursor.execute("UPDATE guild_settings SET timezone = ? WHERE guild_id = ?", (self.values[0], interaction.guild_id))
        conn.commit()
        await interaction.response.send_message(t(interaction.guild_id, 'config_set_timezone_success', timezone=self.values[0]), ephemeral=True)

# View principale con i pulsanti
class ConfigPanelView(discord.ui.View):
    def __init__(self, guild_id: int):
        super().__init__(timeout=180)
        self.guild_id = guild_id

    @discord.ui.button(label="Lingua", style=discord.ButtonStyle.secondary, emoji="🌐")
    async def button_language(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.send_message(view=SelectView(self.guild_id, LanguageSelect(self.guild_id)), ephemeral=True)

    @discord.ui.button(label="Canale Log", style=discord.ButtonStyle.secondary, emoji="📜")
    async def button_log_channel(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.send_modal(SetChannelModal())

    @discord.ui.button(label="Ruolo Staff", style=discord.ButtonStyle.secondary, emoji="🛡️")
    async def button_staff_role(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.send_modal(SetRoleModal())

    @discord.ui.button(label="Fuso Orario", style=discord.ButtonStyle.secondary, emoji="⏰")
    async def button_timezone(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.send_message(view=SelectView(self.guild_id, TimezoneSelect(self.guild_id)), ephemeral=True)


@bot.tree.command(name="config", description="Mostra il pannello di configurazione del bot.")
@app_commands.check(is_staff_or_admin)
async def config_command(interaction: discord.Interaction):
    guild_id = interaction.guild_id
    embed = discord.Embed(
        title=t(guild_id, 'config_panel_title'),
        description=t(guild_id, 'config_panel_desc'),
        color=discord.Color.blue()
    )
    view = ConfigPanelView(guild_id)
    await interaction.response.send_message(embed=embed, view=view, ephemeral=True)

# --- COMANDI DI MODERAZIONE ---

mod_group = app_commands.Group(name="mod", description="Comandi di moderazione.")

@mod_group.command(name="clear", description="Cancella messaggi in un canale.")
@app_commands.describe(amount="Il numero di messaggi da cancellare (max 100).")
@app_commands.check(is_staff_or_admin)
async def clear(interaction: discord.Interaction, amount: app_commands.Range[int, 1, 100]):
    """Cancella un numero specificato di messaggi."""
    await interaction.response.defer(ephemeral=True)
    deleted = await interaction.channel.purge(limit=amount)
    response_text = t(interaction.guild_id, 'clear_success', amount=len(deleted))
    await interaction.followup.send(response_text)

async def log_action(interaction: discord.Interaction, action: str, user: discord.Member, moderator: discord.User, reason: str):
    """Invia un messaggio di log nel canale configurato."""
    guild_id = interaction.guild_id
    cursor.execute("SELECT log_channel_id FROM guild_settings WHERE guild_id = ?", (guild_id,))
    result = cursor.fetchone()
    if result and result[0]:
        log_channel = bot.get_channel(result[0])
        if log_channel:
            embed = discord.Embed(
                title=t(guild_id, 'log_title'),
                color=discord.Color.red()
            )
            embed.add_field(name=t(guild_id, 'log_action'), value=action, inline=False)
            embed.add_field(name=t(guild_id, 'log_user'), value=f"{user.mention} ({user.id})", inline=True)
            embed.add_field(name=t(guild_id, 'log_moderator'), value=f"{moderator.mention} ({moderator.id})", inline=True)
            embed.add_field(name=t(guild_id, 'log_reason'), value=reason, inline=False)
            embed.set_timestamp(datetime.utcnow())
            await log_channel.send(embed=embed)

@mod_group.command(name="kick", description="Espelle un utente dal server.")
@app_commands.describe(user="L'utente da espellere.", reason="Il motivo dell'espulsione.")
@app_commands.check(is_staff_or_admin)
async def kick(interaction: discord.Interaction, user: discord.Member, reason: str = None):
    guild_id = interaction.guild_id
    reason = reason or t(guild_id, 'kick_reason_default')
    
    try:
        dm_text = t(guild_id, 'kick_success_dm', guild_name=interaction.guild.name, reason=reason)
        await user.send(dm_text)
    except discord.Forbidden:
        pass # L'utente ha i DM chiusi

    await user.kick(reason=reason)
    
    response_text = t(guild_id, 'kick_success_channel', user=user.display_name)
    await interaction.response.send_message(response_text)
    await log_action(interaction, t(guild_id, 'log_action_kick'), user, interaction.user, reason)

@mod_group.command(name="ban", description="Banna un utente dal server.")
@app_commands.describe(user="L'utente da bannare.", reason="Il motivo del ban.")
@app_commands.check(is_staff_or_admin)
async def ban(interaction: discord.Interaction, user: discord.Member, reason: str = None):
    guild_id = interaction.guild_id
    reason = reason or t(guild_id, 'kick_reason_default') # Riutilizzo la stringa per motivo default

    try:
        dm_text = t(guild_id, 'ban_success_dm', guild_name=interaction.guild.name, reason=reason)
        await user.send(dm_text)
    except discord.Forbidden:
        pass

    await user.ban(reason=reason)
    
    response_text = t(guild_id, 'ban_success_channel', user=user.display_name)
    await interaction.response.send_message(response_text)
    await log_action(interaction, t(guild_id, 'log_action_ban'), user, interaction.user, reason)

@mod_group.command(name="mute", description="Silenzia un utente per un tempo determinato.")
@app_commands.describe(user="L'utente da silenziare.", duration_hours="Ore di silenzio.", reason="Il motivo del silenzio.")
@app_commands.check(is_staff_or_admin)
async def mute(interaction: discord.Interaction, user: discord.Member, duration_hours: app_commands.Range[int, 1, 24*28], reason: str = None):
    guild_id = interaction.guild_id
    reason = reason or t(guild_id, 'kick_reason_default')
    duration = timedelta(hours=duration_hours)
    
    await user.timeout(duration, reason=reason)
    
    end_time = discord.utils.utcnow() + duration
    response_text = t(guild_id, 'mute_success_channel', user=user.display_name, timestamp=f"<t:{int(end_time.timestamp())}:R>", reason=reason)
    await interaction.response.send_message(response_text)
    await log_action(interaction, t(guild_id, 'log_action_mute'), user, interaction.user, reason)

@mod_group.command(name="unmute", description="Rimuove il silenzio da un utente.")
@app_commands.describe(user="L'utente a cui rimuovere il silenzio.")
@app_commands.check(is_staff_or_admin)
async def unmute(interaction: discord.Interaction, user: discord.Member, reason: str = None):
    guild_id = interaction.guild_id
    reason = reason or t(guild_id, 'kick_reason_default')
    
    await user.timeout(None, reason=reason)
    
    response_text = t(guild_id, 'unmute_success_channel', user=user.display_name)
    await interaction.response.send_message(response_text)
    await log_action(interaction, t(guild_id, 'log_action_unmute'), user, interaction.user, reason)

@mod_group.command(name="warn", description="Avvisa un utente.")
@app_commands.describe(user="L'utente da avvisare.", reason="Il motivo dell'avvertimento.")
@app_commands.check(is_staff_or_admin)
async def warn(interaction: discord.Interaction, user: discord.Member, reason: str):
    guild_id = interaction.guild_id
    moderator_id = interaction.user.id
    
    cursor.execute("INSERT INTO warnings (guild_id, user_id, moderator_id, reason) VALUES (?, ?, ?, ?)",
                   (guild_id, user.id, moderator_id, reason))
    conn.commit()
    
    warn_count = cursor.execute("SELECT COUNT(*) FROM warnings WHERE guild_id = ? AND user_id = ?", (guild_id, user.id)).fetchone()[0]
    
    try:
        dm_text = t(guild_id, 'warn_success_dm', guild_name=interaction.guild.name, reason=reason)
        await user.send(dm_text)
    except discord.Forbidden:
        pass
        
    response_text = t(guild_id, 'warn_success_channel', user=user.display_name, count=warn_count)
    await interaction.response.send_message(response_text)
    await log_action(interaction, t(guild_id, 'log_action_warn'), user, interaction.user, reason)

@mod_group.command(name="warnings", description="Mostra gli avvertimenti di un utente.")
@app_commands.describe(user="L'utente di cui vedere gli avvertimenti.")
@app_commands.check(is_staff_or_admin)
async def warnings(interaction: discord.Interaction, user: discord.Member):
    guild_id = interaction.guild_id
    cursor.execute("SELECT warn_id, moderator_id, reason, timestamp FROM warnings WHERE guild_id = ? AND user_id = ?", (guild_id, user.id))
    user_warnings = cursor.fetchall()

    embed = discord.Embed(title=t(guild_id, 'warnings_list_title', user=user.display_name), color=discord.Color.orange())

    if not user_warnings:
        embed.description = t(guild_id, 'warnings_list_no_warnings')
    else:
        for warn in user_warnings:
            moderator = interaction.guild.get_member(warn[1]) or f"ID: {warn[1]}"
            timestamp = discord.utils.format_dt(datetime.fromisoformat(warn[3]), 'f')
            embed.add_field(
                name=f"Warn ID: {warn[0]} - {timestamp}",
                value=t(guild_id, 'warnings_list_entry', warn_id=warn[0], moderator=moderator, reason=warn[2]),
                inline=False
            )
    
    await interaction.response.send_message(embed=embed, ephemeral=True)

@mod_group.command(name="clearwarns", description="Cancella tutti gli avvertimenti di un utente.")
@app_commands.describe(user="L'utente a cui cancellare gli avvertimenti.")
@app_commands.check(is_staff_or_admin)
async def clearwarns(interaction: discord.Interaction, user: discord.Member):
    guild_id = interaction.guild_id
    cursor.execute("DELETE FROM warnings WHERE guild_id = ? AND user_id = ?", (guild_id, user.id))
    conn.commit()
    
    response_text = t(guild_id, 'clearwarns_success', user=user.display_name)
    await interaction.response.send_message(response_text)
    await log_action(interaction, t(guild_id, 'log_action_clearwarns'), user, interaction.user, "N/A")

bot.tree.add_command(mod_group)


# --- COMANDI DI DIVERTIMENTO ---

# Helper per i comandi di azione con GIF
ACTION_GIFS = {
    "hug": ["https://media.giphy.com/media/v1.Y2lkPTc5MGI3NjExbmZyZ3Nqa3lqZ3g0dDA2d2Q3Z2plY2JqNnJzZ3BvZ2d3eXNpa3JmZyZlcD12MV9pbnRlcm5hbF9naWZfYnlfaWQmY3Q9Zw/2QBfQ32P3aL4c/giphy.gif"],
    "kiss": ["https://media.giphy.com/media/v1.Y2lkPTc5MGI3NjExbDB6MWw4bXJqOHVuaTl2a2M1b2ZqZzF0dGZ2YnJ2Y3R2c2J2aW5qMyZlcD12MV9pbnRlcm5hbF9naWZfYnlfaWQmY3Q9Zw/G3va31oEEnIkM/giphy.gif"],
    "slap": ["https://media.giphy.com/media/v1.Y2lkPTc5MGI3NjExbmRzZ3BjaGNlZ3ZqZzJzY3g3dGR4NTB2Z3R0N2xwb2JtN2J6M25pYiZlcD12MV9pbnRlcm5hbF9naWZfYnlfaWQmY3Q9Zw/gSIz6gGLhA2vAZSkZT/giphy.gif"]
}

async def action_command(interaction: discord.Interaction, action_type: str, user: discord.Member):
    guild_id = interaction.guild_id
    text = t(guild_id, f'action_{action_type}', user1=interaction.user.mention, user2=user.mention)
    
    embed = discord.Embed(description=text, color=discord.Color.pink())
    
    gif_url = random.choice(ACTION_GIFS[action_type])
    embed.set_image(url=gif_url)
    
    await interaction.response.send_message(embed=embed)

@bot.tree.command(name="hug", description="Abbraccia un utente.")
async def hug(interaction: discord.Interaction, user: discord.Member):
    await action_command(interaction, "hug", user)

@bot.tree.command(name="kiss", description="Bacia un utente.")
async def kiss(interaction: discord.Interaction, user: discord.Member):
    await action_command(interaction, "kiss", user)

@bot.tree.command(name="slap", description="Schiaffeggia un utente.")
async def slap(interaction: discord.Interaction, user: discord.Member):
    await action_command(interaction, "slap", user)


class RPSView(discord.ui.View):
    def __init__(self, guild_id):
        super().__init__(timeout=60)
        self.guild_id = guild_id
        self.user_choice = None
        self.bot_choice = random.choice(["rock", "paper", "scissors"])

    @discord.ui.button(label="✊", style=discord.ButtonStyle.grey)
    async def rock(self, interaction: discord.Interaction, button: discord.ui.Button):
        self.user_choice = "rock"
        await self.resolve_game(interaction)

    @discord.ui.button(label="📄", style=discord.ButtonStyle.grey)
    async def paper(self, interaction: discord.Interaction, button: discord.ui.Button):
        self.user_choice = "paper"
        await self.resolve_game(interaction)

    @discord.ui.button(label="✌️", style=discord.ButtonStyle.grey)
    async def scissors(self, interaction: discord.Interaction, button: discord.ui.Button):
        self.user_choice = "scissors"
        await self.resolve_game(interaction)
        
    async def resolve_game(self, interaction: discord.Interaction):
        # Disabilita i pulsanti
        for item in self.children:
            item.disabled = True
        
        # Determina il vincitore
        winner = None # None for tie, True for user, False for bot
        if self.user_choice == self.bot_choice:
            winner = None
        elif (self.user_choice == "rock" and self.bot_choice == "scissors") or \
             (self.user_choice == "paper" and self.bot_choice == "rock") or \
             (self.user_choice == "scissors" and self.bot_choice == "paper"):
            winner = True
        else:
            winner = False

        # Crea l'embed del risultato
        result_text = ""
        if winner is None:
            result_text = t(self.guild_id, 'rps_tie')
        elif winner:
            result_text = t(self.guild_id, 'rps_win')
        else:
            result_text = t(self.guild_id, 'rps_lose')

        embed = discord.Embed(title=t(self.guild_id, 'rps_title'), description=result_text, color=discord.Color.blurple())
        embed.add_field(name=t(self.guild_id, 'rps_user_choice'), value=self.user_choice, inline=True)
        embed.add_field(name=t(self.guild_id, 'rps_bot_choice'), value=self.bot_choice, inline=True)
        
        await interaction.response.edit_message(embed=embed, view=self)

@bot.tree.command(name="rps", description="Gioca a Sasso, Carta, Forbici.")
async def rps(interaction: discord.Interaction):
    guild_id = interaction.guild_id
    view = RPSView(guild_id)
    await interaction.response.send_message("Scegli la tua mossa!", view=view)


@bot.tree.command(name="rate", description="Valuta qualcosa da 1 a 10.")
@app_commands.describe(thing="La cosa da valutare.")
async def rate(interaction: discord.Interaction, thing: str):
    guild_id = interaction.guild_id
    rating = random.randint(1, 10)
    embed = discord.Embed(
        title=t(guild_id, 'rate_title'),
        description=t(guild_id, 'rate_result', thing=thing, rating=rating),
        color=discord.Color.random()
    )
    await interaction.response.send_message(embed=embed)

@bot.tree.command(name="ship", description="Calcola la compatibilità amorosa.")
@app_commands.describe(user1="La prima persona.", user2="La seconda persona.")
async def ship(interaction: discord.Interaction, user1: discord.Member, user2: discord.Member):
    guild_id = interaction.guild_id
    percentage = random.randint(0, 100)
    
    if percentage > 90:
        comment = t(guild_id, 'ship_perfect')
    elif percentage > 70:
        comment = t(guild_id, 'ship_good')
    elif percentage > 40:
        comment = t(guild_id, 'ship_medium')
    else:
        comment = t(guild_id, 'ship_bad')

    description = f"{t(guild_id, 'ship_result', user1=user1.mention, user2=user2.mention, percentage=percentage)}\n\n{comment}"

    embed = discord.Embed(
        title=t(guild_id, 'ship_title'),
        description=description,
        color=discord.Color.red()
    )
    await interaction.response.send_message(embed=embed)

@bot.tree.command(name="meme", description="Mostra un meme casuale.")
async def meme(interaction: discord.Interaction):
    guild_id = interaction.guild_id
    url = "https://meme-api.com/gimme"
    
    async with aiohttp.ClientSession() as session:
        async with session.get(url) as response:
            if response.status == 200:
                data = await response.json()
                image_url = data.get('url')
                title = data.get('title')
                
                embed = discord.Embed(
                    title=title or t(guild_id, 'meme_title'),
                    color=discord.Color.random()
                )
                embed.set_image(url=image_url)
                await interaction.response.send_message(embed=embed)
            else:
                embed = discord.Embed(
                    title=t(guild_id, 'error_generic_title'),
                    description=t(guild_id, 'error_api'),
                    color=discord.Color.red()
                )
                await interaction.response.send_message(embed=embed, ephemeral=True)


@bot.tree.command(name="coinflip", description="Lancia una moneta.")
async def coinflip(interaction: discord.Interaction):
    """Lancia una moneta e mostra il risultato in un embed."""
    guild_id = interaction.guild_id
    
    heads = t(guild_id, 'heads')
    tails = t(guild_id, 'tails')
    result = random.choice([heads, tails])

    embed = discord.Embed(
        title=t(guild_id, 'coinflip_title'),
        description=t(guild_id, 'coinflip_result', result=result),
        color=discord.Color.gold()
    )
    await interaction.response.send_message(embed=embed)

@bot.tree.command(name="8ball", description="Chiedi alla Palla 8 Magica.")
@app_commands.describe(question="La tua domanda alla palla 8.")
async def eight_ball(interaction: discord.Interaction, question: str):
    """Risponde a una domanda con una frase casuale."""
    guild_id = interaction.guild_id
    answers = t(guild_id, '8ball_answers')
    answer = random.choice(answers)
    
    embed = discord.Embed(
        title=t(guild_id, '8ball_title'),
        color=discord.Color.blue()
    )
    embed.add_field(name=t(guild_id, '8ball_question', question=question), value=t(guild_id, '8ball_answer', answer=answer), inline=False)
    await interaction.response.send_message(embed=embed)

async def get_animal_image(url: str, json_key: str):
    """Funzione helper per ottenere un'immagine da un'API."""
    async with aiohttp.ClientSession() as session:
        async with session.get(url) as response:
            if response.status == 200:
                data = await response.json()
                return data.get(json_key)
            return None

@bot.tree.command(name="dog", description="Mostra una foto di un cane.")
async def dog(interaction: discord.Interaction):
    """Mostra un'immagine casuale di un cane."""
    guild_id = interaction.guild_id
    image_url = await get_animal_image('https://dog.ceo/api/breeds/image/random', 'message')
    
    if image_url:
        embed = discord.Embed(
            title=t(guild_id, 'animal_title_dog'),
            color=discord.Color.green()
        )
        embed.set_image(url=image_url)
        await interaction.response.send_message(embed=embed)
    else:
        embed = discord.Embed(
            title=t(guild_id, 'error_generic_title'),
            description=t(guild_id, 'error_api'),
            color=discord.Color.red()
        )
        await interaction.response.send_message(embed=embed, ephemeral=True)

@bot.tree.command(name="cat", description="Mostra una foto di un gatto.")
async def cat(interaction: discord.Interaction):
    """Mostra un'immagine casuale di un gatto."""
    guild_id = interaction.guild_id
    # TheCatApi restituisce una lista, quindi prendiamo il primo elemento
    async with aiohttp.ClientSession() as session:
        async with session.get('https://api.thecatapi.com/v1/images/search') as response:
            if response.status == 200:
                data = await response.json()
                image_url = data[0].get('url') if data else None
            else:
                image_url = None

    if image_url:
        embed = discord.Embed(
            title=t(guild_id, 'animal_title_cat'),
            color=discord.Color.orange()
        )
        embed.set_image(url=image_url)
        await interaction.response.send_message(embed=embed)
    else:
        embed = discord.Embed(
            title=t(guild_id, 'error_generic_title'),
            description=t(guild_id, 'error_api'),
            color=discord.Color.red()
        )
        await interaction.response.send_message(embed=embed, ephemeral=True)

@bot.tree.command(name="joke", description="Racconta una battuta.")
async def joke(interaction: discord.Interaction):
    """Racconta una battuta presa da un'API."""
    guild_id = interaction.guild_id
    url = "https://v2.jokeapi.dev/joke/Any"
    joke_text = ""
    
    async with aiohttp.ClientSession() as session:
        async with session.get(url) as response:
            if response.status == 200:
                data = await response.json()
                if data['type'] == 'single':
                    joke_text = data['joke']
                else:
                    joke_text = f"{data['setup']}\n\n||{data['delivery']}||" # Delivery in spoiler
            
    if joke_text:
        embed = discord.Embed(
            title=t(guild_id, 'joke_title'),
            description=joke_text,
            color=discord.Color.purple()
        )
        await interaction.response.send_message(embed=embed)
    else:
        embed = discord.Embed(
            title=t(guild_id, 'error_generic_title'),
            description=t(guild_id, 'joke_error'),
            color=discord.Color.red()
        )
        await interaction.response.send_message(embed=embed, ephemeral=True)


# --- AVVIO DEL BOT ---
def run_bot():
    bot.run(config['token'])
