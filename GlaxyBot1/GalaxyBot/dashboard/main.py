from flask import Flask, redirect, url_for, request, session, render_template, g, jsonify
from waitress import serve
import requests
import os
import sqlite3

# --- App Setup ---
app = Flask(__name__)
app.secret_key = os.getenv('FLASK_SECRET_KEY')

# Path del database per Render (o locale se non su Render)
render_data_path = '/var/data/render/data.db'
local_data_path = os.path.join(os.path.dirname(os.path.dirname(__file__)), 'bot', 'data.db')
DB_PATH = render_data_path if os.path.exists('/var/data/render') else local_data_path


# --- Database Connection Handling ---
def get_db():
    db = getattr(g, '_database', None)
    if db is None:
        db = g._database = sqlite3.connect(DB_PATH)
        db.row_factory = sqlite3.Row # Allows accessing columns by name
    return db

@app.teardown_appcontext
def close_connection(exception):
    db = getattr(g, '_database', None)
    if db is not None:
        db.close()


# --- Auth & Permission Helpers ---
def get_user_admin_guilds():
    """Fetches guilds from Discord API where the user is an admin."""
    if 'access_token' not in session:
        return []
    headers = {'Authorization': f'Bearer {session["access_token"]}'}
    guilds_r = requests.get(f'{API_BASE_URL}/users/@me/guilds', headers=headers)
    if guilds_r.status_code != 200:
        return [] # Token might be expired
    user_guilds = guilds_r.json()
    return [g for g in user_guilds if (int(g['permissions']) & 0x8) == 0x8]

def is_admin_of_guild(guild_id: int) -> bool:
    """Checks if the logged-in user is an admin of the specified guild."""
    admin_guilds = get_user_admin_guilds()
    return any(int(g['id']) == guild_id for g in admin_guilds)


# --- OAuth2 Configuration ---
CLIENT_ID = os.getenv('DISCORD_CLIENT_ID')
CLIENT_SECRET = os.getenv('DISCORD_CLIENT_SECRET')
REDIRECT_URI = os.getenv('DISCORD_REDIRECT_URI')
API_BASE_URL = 'https://discord.com/api/v10'

# --- Routes ---

@app.route('/')
def index():
    if 'user_id' in session:
        return redirect(url_for('select_server'))
    return render_template('login.html')

@app.route('/login')
def login():
    return redirect(
        f'{API_BASE_URL}/oauth2/authorize?client_id={CLIENT_ID}&redirect_uri={REDIRECT_URI}&response_type=code&scope=identify%20guilds'
    )

@app.route('/logout')
def logout():
    session.clear()
    return redirect(url_for('index'))

@app.route('/callback')
def callback():
    code = request.args.get('code')
    if not code:
        return "Error: No code provided.", 400

    token_data = {
        'client_id': CLIENT_ID,
        'client_secret': CLIENT_SECRET,
        'grant_type': 'authorization_code',
        'code': code,
        'redirect_uri': REDIRECT_URI,
        'scope': 'identify guilds'
    }
    headers = {'Content-Type': 'application/x-www-form-urlencoded'}
    r = requests.post(f'{API_BASE_URL}/oauth2/token', data=token_data, headers=headers)
    r.raise_for_status()
    token_info = r.json()

    session['access_token'] = token_info['access_token']
    
    headers = {'Authorization': f'Bearer {session["access_token"]}'}
    user_r = requests.get(f'{API_BASE_URL}/users/@me', headers=headers)
    user_r.raise_for_status()
    user_info = user_r.json()
    
    session['user_id'] = user_info['id']
    session['username'] = user_info['username']

    return redirect(url_for('select_server'))

@app.route('/servers')
def select_server():
    if 'user_id' not in session:
        return redirect(url_for('login'))

    headers = {'Authorization': f'Bearer {session["access_token"]}'}
    guilds_r = requests.get(f'{API_BASE_URL}/users/@me/guilds', headers=headers)
    guilds_r.raise_for_status()
    user_guilds = guilds_r.json()

    # Filtra per i server dove l'utente è amministratore
    admin_guilds = [g for g in user_guilds if (int(g['permissions']) & 0x8) == 0x8]
    
    return render_template('select_server.html', guilds=admin_guilds, username=session.get('username'))

@app.route('/dashboard/<int:guild_id>')
def dashboard(guild_id):
    if 'user_id' not in session:
        return redirect(url_for('login'))
    
    if not is_admin_of_guild(guild_id):
        return "You do not have permission to access this dashboard.", 403

    admin_guilds = get_user_admin_guilds()
    guild_data = next((g for g in admin_guilds if int(g['id']) == guild_id), None)
    
    if not guild_data:
         return "Guild not found or permission error.", 404

    # Costruisci l'URL dell'icona
    if guild_data['icon']:
        guild_data['icon_url'] = f"https://cdn.discordapp.com/icons/{guild_id}/{guild_data['icon']}.png"
    else:
        guild_data['icon_url'] = "https://cdn.discordapp.com/embed/avatars/0.png" # Default icon

    return render_template('dashboard.html', guild=guild_data)


# --- API Endpoints ---

@app.route('/api/guild/<int:guild_id>/<resource>')
def get_guild_resource(guild_id, resource):
    """Fetches channels or roles for a guild using the bot's token."""
    if 'user_id' not in session or not is_admin_of_guild(guild_id):
        return jsonify({"error": "Unauthorized"}), 403

    if resource not in ['channels', 'roles']:
        return jsonify({"error": "Invalid resource"}), 400

    bot_token = os.getenv('DISCORD_BOT_TOKEN')
    headers = {'Authorization': f'Bot {bot_token}'}
    
    r = requests.get(f'{API_BASE_URL}/guilds/{guild_id}/{resource}', headers=headers)
    r.raise_for_status()
    
    # Semplifica i dati per il frontend
    if resource == 'channels':
        # Filtra solo i canali di testo
        items = [{'id': item['id'], 'name': item['name']} for item in r.json() if item['type'] == 0]
    else: # roles
        items = [{'id': item['id'], 'name': item['name']} for item in r.json()]
        
    return jsonify(items)


@app.route('/api/settings/<int:guild_id>', methods=['GET'])
def get_settings(guild_id):
    if 'user_id' not in session or not is_admin_of_guild(guild_id):
        return jsonify({"error": "Unauthorized"}), 403
    
    db = get_db()
    cur = db.execute("SELECT * FROM guild_settings WHERE guild_id = ?", (guild_id,))
    settings = cur.fetchone()
    
    if settings:
        return jsonify(dict(settings))
    else:
        # Return default settings if none are in the DB
        return jsonify({
            "guild_id": guild_id,
            "language": "it",
            "log_channel_id": None,
            "staff_role_id": None,
            "timezone": "UTC"
        })

@app.route('/api/settings/<int:guild_id>', methods=['POST'])
def update_settings(guild_id):
    if 'user_id' not in session or not is_admin_of_guild(guild_id):
        return jsonify({"error": "Unauthorized"}), 403
    
    data = request.json
    db = get_db()
    
    # Esegue l'update per ogni chiave inviata
    for key, value in data.items():
        # Semplice validazione per sicurezza
        if key in ['language', 'log_channel_id', 'staff_role_id', 'timezone']:
            # Assicura che la riga esista
            db.execute("INSERT OR IGNORE INTO guild_settings (guild_id) VALUES (?)", (guild_id,))
            db.execute(f"UPDATE guild_settings SET {key} = ? WHERE guild_id = ?", (value, guild_id))
    
    db.commit()
    return jsonify({"success": True, "message": "Settings updated."})


# --- Admin Routes ---

@app.route('/admin')
def admin_panel():
    if 'user_id' not in session:
        return redirect(url_for('login'))
    
    # Proteggi questa rotta solo per il proprietario del bot
    bot_owner_id = os.getenv('BOT_OWNER_ID')
    if session['user_id'] != bot_owner_id:
        return "Access Forbidden.", 403
        
    return render_template('admin.html')

@app.route('/api/admin/status', methods=['GET'])
def get_admin_status():
    bot_owner_id = os.getenv('BOT_OWNER_ID')
    if 'user_id' not in session or session['user_id'] != bot_owner_id:
        return jsonify({"error": "Unauthorized"}), 403
        
    db = get_db()
    cur = db.execute("SELECT maintenance_mode FROM bot_status WHERE id = 1")
    status = cur.fetchone()
    return jsonify({"maintenance_mode": bool(status['maintenance_mode'])})

@app.route('/api/admin/toggle', methods=['POST'])
def toggle_maintenance():
    bot_owner_id = os.getenv('BOT_OWNER_ID')
    if 'user_id' not in session or session['user_id'] != bot_owner_id:
        return jsonify({"error": "Unauthorized"}), 403
        
    db = get_db()
    # Inverte il valore booleano (0 o 1)
    db.execute("UPDATE bot_status SET maintenance_mode = 1 - maintenance_mode WHERE id = 1")
    db.commit()
    return jsonify({"success": True})


def run_dashboard():
    serve(app, host='0.0.0.0', port=5000)
