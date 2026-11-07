from __future__ import annotations

import asyncio

from src.database.checks import check_database_filesystem, check_startup
from src.database.connection import db


async def main() -> None:
    check_database_filesystem()
    await db.initialize()
    await check_startup(db)
    await db.close()


if __name__ == "__main__":
    asyncio.run(main())
