from motor.motor_asyncio import AsyncIOMotorClient
from config import settings
import logging

logger = logging.getLogger(__name__)

class Database:
    client: AsyncIOMotorClient = None
    db = None

db_instance = Database()

async def connect_db():
    db_instance.client = AsyncIOMotorClient(settings.MONGODB_URL)
    db_instance.db = db_instance.client[settings.MONGODB_DB_NAME]
    await db_instance.db.users.create_index("email", unique=True)
    await db_instance.db.users.create_index("google_id", unique=True)
    await db_instance.db.sessions.create_index("token", unique=True)
    await db_instance.db.sessions.create_index("expires_at", expireAfterSeconds=0)
    logger.info("MongoDB connected")

async def close_db():
    if db_instance.client:
        db_instance.client.close()

def get_db():
    return db_instance.db