import sys
import os
import json

# Questo script è ora un semplice dispatcher basato su argomenti
# per essere compatibile con la startCommand di Render.

def main():
    if len(sys.argv) < 2:
        print("Usage: python run.py [web|bot]")
        sys.exit(1)

    # Carica la configurazione per ottenere le chiavi necessarie
    # Il percorso è relativo a dove viene eseguito lo script (la root del progetto)
    with open('discord_bot/config.json', 'r') as f:
        config = json.load(f)

    # Imposta le variabili d'ambiente
    os.environ['FLASK_SECRET_KEY'] = 'supersecretkey_changelater' # Render può sovrascriverlo
    os.environ['DISCORD_CLIENT_ID'] = config.get('DISCORD_CLIENT_ID', 'YOUR_DISCORD_CLIENT_ID')
    os.environ['DISCORD_CLIENT_SECRET'] = config.get('DISCORD_CLIENT_SECRET', 'YOUR_DISCORD_CLIENT_SECRET')
    os.environ['DISCORD_BOT_TOKEN'] = config.get('token', 'YOUR_BOT_TOKEN')
    os.environ['BOT_OWNER_ID'] = config.get('bot_owner_id', 'YOUR_USER_ID')
    # L'URI di redirect deve essere impostato nell'ambiente di Render
    os.environ.setdefault('DISCORD_REDIRECT_URI', 'http://localhost:5000/callback')

    service_type = sys.argv[1]

    if service_type == "web":
        print("Starting web dashboard...")
        from dashboard.main import run_dashboard
        run_dashboard()
    elif service_type == "bot":
        print("Starting Discord bot...")
        from bot.main import run_bot
        run_bot()
    else:
        print(f"Unknown service type: {service_type}")
        sys.exit(1)

if __name__ == "__main__":
    main()
