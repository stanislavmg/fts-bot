import os

API_TOKEN = os.getenv("TG_BOT_TOKEN", "")

FS_CONSUMER_KEY = os.getenv("FS_CONSUMER_KEY", "")
FS_CONSUMER_SECRET = os.getenv("FS_CONSUMER_SECRET", "")

LLM_API_KEY = os.getenv("LLM_API_KEY", "")
LLM_BASE_URL = os.getenv("LLM_BASE_URL", "https://api.deepseek.com")
LLM_MODEL = os.getenv("LLM_MODEL", "deepseek-chat")

FS_REQUEST_TOKEN_URL = "https://authentication.fatsecret.com/oauth/request_token"
FS_AUTHORIZE_URL = "https://authentication.fatsecret.com/oauth/authorize"
FS_ACCESS_TOKEN_URL = "https://authentication.fatsecret.com/oauth/access_token"
FS_API_URL = "https://platform.fatsecret.com/rest/server.api"

DB_PATH = os.path.join(os.path.dirname(__file__), "bot.db")
