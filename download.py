#!/usr/bin/python3
from urllib.parse import urlparse
import asyncio
import sys

from aiohttp import ClientSession

import gestalt


instance = gestalt.Gestalt(dbfile = sys.argv[1] if len(sys.argv) > 1
        else gestalt.DEFAULT_DB)

async def download(mask, ext):
    instance.loop = asyncio.get_running_loop()
    instance.session = ClientSession()
    await gestalt.gesp.ActionChange(mask['maskid'], 'avatar', mask['avatar'],
        ext = ext).execute(instance)
    await instance.session.close()

for mask in instance.fetchall('select * from masks'):
    if mask['avatar']:
        try:
            parse = urlparse(mask['avatar'])
            if 'discord' not in parse.hostname:
                continue
        except:
            pass
        else:
            asyncio.run(download(mask, parse.path.split('.')[-1]))

