import os
import sys

def main():
    env_vars = {
        "DISCORD_TOKEN": os.getenv("DISCORD_TOKEN", ""),
        "MONGO_URL": os.getenv("MONGO_URL", ""),
        "MONGODB_URI": os.getenv("MONGODB_URI", "") or os.getenv("MONGO_URL", ""),
        "WEBHOOK_URL": os.getenv("WEBHOOK_URL", ""),
        "OWNER_IDS": os.getenv("OWNER_IDS", ""),
        "AUTO_SYNC_COMMANDS": os.getenv("AUTO_SYNC_COMMANDS", "true"),
        "PORT": os.getenv("PORT", "7860")
    }

    env_path = ".env"
    with open(env_path, "w", encoding="utf-8") as f:
        for k, v in env_vars.items():
            if v:
                f.write(f"{k}={v}\n")
    print(f"Generated {env_path} successfully.")

if __name__ == "__main__":
    main()
