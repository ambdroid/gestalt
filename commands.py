import json
import asyncio
from sqlite3 import IntegrityError

import aiohttp
import discord

from defs import *


def escape(text):
    return discord.utils.escape_markdown(
            discord.utils.escape_mentions(str(text)))


# [text] -> ['[',']']
def parse_tags(tags):
    split = tags.lower().split('text')
    if len(split) != 2 or not ''.join(split):
        raise RuntimeError(
                'Please provide valid tags around `text` (e.g. `[text]`).')
    return split


class CommandReader:
    BOOL_KEYWORDS = {
        'on': 1,
        'off': 0,
        'yes': 1,
        'no': 0,
        'true': 1,
        'false': 0,
        '0': 0,
        '1': 1
    }

    def __init__(self, msg, cmd):
        self.msg = msg
        self.cmd = cmd

    def is_empty(self):
        return self.cmd == ''

    def read_word(self):
        # add empty strings to pad array if string empty or no split
        split = self.cmd.split(maxsplit = 1) + ['','']
        self.cmd = split[1]
        return split[0]

    def read_quote(self):
        match = re.match('\\"[^\\"]*\\"', self.cmd)
        if match == None:
            return self.read_word()
        self.cmd = match.string[len(match[0]):].strip()
        return match[0][1:-1]

    def read_bool_int(self):
        word = self.read_word().lower()
        if word in CommandReader.BOOL_KEYWORDS:
            return CommandReader.BOOL_KEYWORDS[word]

    def read_remainder(self):
        ret = self.cmd
        if len(ret) > 1 and ret[0] == ret[-1] == '"':
            ret = ret[1:-1]
        self.cmd = ''
        return ret

    # discord.ext includes a MemberConverter
    # but that's only available whem using discord.ext Command
    def read_member(self):
        # even if the member is in a mention, consume the text of the mention
        name = self.read_quote()
        if self.msg.mentions:
            return self.msg.mentions[0]
        return self.msg.guild.get_member_named(name)

    def read_role(self):
        name = self.read_quote()
        if self.msg.role_mentions:
            return self.msg.role_mentions[0]
        guild = self.msg.guild
        if name == 'everyone':
            return guild.default_role
        return discord.utils.get(guild.roles, name = name)


class GestaltCommands:
    def get_user_proxy(self, message, name):
        if name == '':
            raise RuntimeError('Please provide a proxy name/ID.')

        # can't do 'and ? in (proxid, cmdname)'; breaks case insensitivity
        proxies = self.fetchall(
                'select * from proxies where userid = ? '
                'and (proxid = ? or cmdname = ?) '
                'and state != ?',
                (message.author.id, name, name, ProxyState.hidden))

        if not proxies:
            raise RuntimeError('You have no proxy with that name/ID.')
        if len(proxies) > 1:
            raise RuntimeError('You have multiple proxies with that name/ID.')

        return proxies[0]


    async def cmd_help(self, message, topic):
        await self.send_embed(message, HELPMSGS.get(topic, HELPMSGS['']))


    async def cmd_invite(self, message):
        if (await self.application_info()).bot_public:
            await self.send_embed(message,
                    discord.utils.oauth_url(self.user.id, permissions = PERMS))


    async def cmd_permcheck(self, message, guildid):
        guildid = message.guild.id if guildid == '' else int(guildid)
        guild = self.get_guild(guildid)
        if guild == None:
            raise RuntimeError(
                    'That guild does not exist or I am not in it.')
        if guild.get_member(message.author.id) == None:
            raise RuntimeError('You are not a member of that guild.')

        memberauth = guild.get_member(message.author.id)
        memberbot = guild.get_member(self.user.id)
        lines = ['**%s**:' % guild.name]
        for chan in guild.text_channels:
            if not memberauth.permissions_in(chan).view_channel:
                continue

            errors = []
            for p in PERMS: # p = ('name', bool)
                if p[1] and not p in list(memberbot.permissions_in(chan)):
                    errors += [p[0]]

            # lack of access implies lack of other perms, so leave them out
            if 'read_messages' in errors:
                errors = ['read_messages']
            errors = REACT_CONFIRM if errors == [] else ', '.join(errors)
            lines.append('`#%s`: %s' % (chan.name, errors))

        await self.send_embed(message, '\n'.join(lines))


    async def cmd_proxy_list(self, message):
        rows = sorted(self.fetchall(
                'select p.*, m.roleid, m.nick from ('
                    'select * from proxies where userid = ?'
                    'order by type asc'
                ') as p left join masks as m '
                'on p.maskid = m.maskid',
                (message.author.id,)),
                key = lambda row: (
                    # randomize so it's not just in order of account creation
                    1000 + abs(hash(str(row['otherid'])))
                    if row['type'] in (ProxyType.swap, ProxyType.pkswap,
                        ProxyType.pkreceipt)
                    else row['type']))

        lines = []
        omit = False
        # must be at least one: the override
        for proxy in rows:
            if proxy['state'] == ProxyState.hidden:
                continue
            # don't show non-global proxies in other servers
            if message.guild and proxy['guildid'] not in [0, message.guild.id]:
                omit = True
                continue
            line = '[`%s`] ' % proxy['proxid']
            if proxy['cmdname']:
                line += '**%s**' % escape(proxy['cmdname'])
            else:
                line += '*no name*'

            if proxy['type'] == ProxyType.override:
                line = SYMBOL_OVERRIDE + line
                parens = ''
            elif proxy['type'] == ProxyType.swap:
                line = SYMBOL_SWAP + line
                user = self.get_user(proxy['otherid'])
                if not user:
                    continue
                parens = 'with **%s**' % escape(user)
            elif proxy['type'] == ProxyType.collective:
                line = SYMBOL_COLLECTIVE + line
                guild = self.get_guild(proxy['guildid'])
                if not guild or not (role := guild.get_role(proxy['roleid'])):
                    continue
                parens = ('**%s** on **%s** in **%s**'
                        % (escape(proxy['nick']), escape(role.name),
                            escape(guild.name)))
            elif proxy['type'] == ProxyType.pkswap:
                line = SYMBOL_PKSWAP + line
                # we don't have pkhids
                # parens = 'PluralKit member **%s**' % proxy['maskid']
                parens = ''
            elif proxy['type'] == ProxyType.pkreceipt:
                line = SYMBOL_RECEIPT + line

            if proxy['prefix'] is not None:
                parens += (' `%s`'
                        % (proxy['prefix'] + 'text' + proxy['postfix'])
                        # hack because escaping ` doesn't work in code blocks
                        .replace('`', '\N{REVERSED PRIME}'))
            if proxy['state'] == ProxyState.inactive:
                parens += ' *(inactive)*'
            if proxy['flags'] & ProxyFlags.auto:
                parens += ' auto **on**'
            if proxy['become'] < 1.0:
                parens += ' *%i%%*' % int(proxy['become'] * 100)

            if parens and proxy['type'] != ProxyType.pkreceipt:
                line += ' (%s)' % parens.strip()
            lines.append(line)

        if omit:
            lines.append('Proxies in other servers have been omitted.')
        await self.send_embed(message, '\n'.join(lines))


    async def cmd_proxy_tags(self, message, proxid, tags):
        (prefix, postfix) = parse_tags(tags)

        try:
            self.execute(
                    'update proxies set prefix = ?, postfix = ? '
                    'where proxid = ?',
                    (prefix, postfix, proxid))
        except IntegrityError:
            raise RuntimeError(ERROR_TAGS)

        await self.mark_success(message, True)


    async def cmd_proxy_auto(self, message, proxy, auto):
        if auto == None:
            auto = not bool(proxy['flags'] & ProxyFlags.auto)
        self.set_proxy_auto(proxy, bool(auto))

        await self.mark_success(message, True)


    async def cmd_proxy_rename(self, message, proxid, newname):
        self.execute('update proxies set cmdname = ? where proxid = ?',
                (newname, proxid))

        await self.mark_success(message, True)


    async def cmd_proxy_keepproxy(self, message, proxy, keep):
        if keep == None:
            keep = not bool(proxy['flags'] & ProxyFlags.keepproxy)
        self.execute(
                'update proxies set flags = (flags & ~?) | ? '
                'where proxid = ?',
                (ProxyFlags.keepproxy, ProxyFlags.keepproxy * int(keep),
                proxy['proxid']))

        await self.mark_success(message, True)


    async def cmd_collective_list(self, message):
        rows = self.fetchall(
                'select * from masks where (guildid, type) = (?, ?)',
                (message.guild.id, ProxyType.collective))

        if len(rows) == 0:
            text = 'This guild does not have any collectives.'
        else:
            guild = message.guild
            text = '\n'.join(['`%s`: %s %s' %
                    (row['maskid'],
                        '**%s**' % escape(row['nick']),
                        # @everyone.mention shows up as @@everyone. weird!
                        # note that this is an embed; mentions don't work
                        ('@everyone' if row['roleid'] == guild.id
                            else guild.get_role(row['roleid']).mention))
                    for row in rows])

        await self.send_embed(message, text)


    async def cmd_collective_new(self, message, role):
        # new collective with name of role and no avatar
        collid = self.gen_id()
        # '@everyone' is awkward and more likely to cause collisions as cmdname
        name = role.guild.name if role == role.guild.default_role else role.name
        self.execute('insert or ignore into masks values'
                '(?, ?, ?, ?, NULL, ?, NULL)',
                (collid, role.guild.id, role.id, name, ProxyType.collective))
        # if there wasn't already a collective on that role
        if self.cur.rowcount == 1:
            for member in role.members:
                if not member.bot:
                    self.mkproxy(member.id, ProxyType.collective,
                            cmdname = name, guildid = role.guild.id,
                            maskid = collid)

            await self.mark_success(message, True)


    async def cmd_collective_update(self, message, collid, name, value):
        self.execute(
                'update masks set %s = ? '
                'where maskid = ?'
                % ('nick' if name == 'name' else 'avatar'),
                (value, collid))
        if self.cur.rowcount == 1:
            await self.mark_success(message, True)


    async def cmd_collective_delete(self, message, coll):
        self.execute('delete from proxies where maskid = ?', (coll['maskid'],))
        self.execute('delete from masks where maskid = ?', (coll['maskid'],))
        if self.cur.rowcount == 1:
            await self.mark_success(message, True)


    async def cmd_prefs_list(self, message, user):
        # list current prefs in 'pref: [on/off]' format
        text = '\n'.join(['%s: **%s**' %
                (pref.name, 'on' if user['prefs'] & pref else 'off')
                for pref in Prefs])
        await self.send_embed(message, text)


    async def cmd_prefs_default(self, message):
        self.execute(
                'update users set prefs = ? where userid = ?',
                (DEFAULT_PREFS, message.author.id))
        await self.mark_success(message, True)


    async def cmd_prefs_update(self, message, user, name, value):
        bit = int(Prefs[name])
        if value == None: # only 'prefs' + name given. invert the thing
            prefs = user['prefs'] ^ bit
        else:
            prefs = (user['prefs'] & ~bit) | (bit * value)
        self.execute(
                'update users set prefs = ? where userid = ?',
                (prefs, message.author.id))

        await self.mark_success(message, True)


    def make_or_activate_swap(self, auth, other, tags):
        (prefix, postfix) = parse_tags(tags) if tags else (None, None)
        # to support future features, look at proxies from target, not author
        swap = self.fetchone(
                'select state from proxies '
                'where (userid, otherid, type) = (?, ?, ?)',
                (other.id, auth.id, ProxyType.swap))
        if not swap:
            if auth.id == other.id:
                # no need to ask yourself for confirmation, just do it
                self.mkproxy(auth.id, ProxyType.swap, cmdname = auth.name,
                        prefix = prefix, postfix = postfix, otherid = auth.id)
                return True
            # create swap. author's is inactive, target's is hidden
            self.mkproxy(auth.id, ProxyType.swap, cmdname = other.name,
                    prefix = prefix, postfix = postfix, otherid = other.id,
                    state = ProxyState.inactive)
            self.mkproxy(other.id, ProxyType.swap, cmdname = auth.name,
                    otherid = auth.id, state = ProxyState.hidden)
            return True
        elif swap[0] == ProxyState.inactive:
            # target is initiator. author can activate swap
            self.execute(
                    'update proxies set prefix = ?, postfix = ?, state = ?'
                    'where (userid, otherid) = (?, ?)',
                    (prefix, postfix, ProxyState.active, auth.id, other.id))
            self.execute(
                    'update proxies set state = ? '
                    'where (userid, otherid) = (?, ?)',
                    (ProxyState.active, other.id, auth.id))
            return True

        return False


    async def cmd_swap_open(self, message, member, tags):
        if self.make_or_activate_swap(message.author, member, tags):
            await self.mark_success(message, True)


    async def cmd_swap_close(self, message, proxy):
        self.execute(
                'delete from proxies '
                'where (userid, otherid) = (?, ?)'
                'or (otherid, userid) = (?, ?)',
                (proxy['userid'], proxy['otherid'])*2)

        await self.mark_success(message, True)


    async def cmd_edit(self, message, content):
        channel = message.channel
        if message.reference:
            proxied = self.fetchone(
                    'select msgid, authid from history where msgid = ?',
                    (message.reference.message_id,))
        else:
            proxied = self.fetchone(
                    'select msgid, authid from history '
                    'where (chanid, authid) = (?, ?)'
                    'order by msgid desc limit 1',
                    (channel.id, message.author.id))
        if not proxied or proxied['authid'] != message.author.id:
            return await self.mark_success(message, False)
        try:
            proxied = await channel.fetch_message(proxied['msgid'])
        except discord.errors.NotFound:
            return await self.mark_success(message, False)

        hook = self.fetchone('select * from webhooks where chanid = ?',
                (channel.id,))
        if not hook or proxied.webhook_id != hook[1]:
            return await self.mark_success(message, False)
        hook = discord.Webhook.partial(hook[1], hook[2], adapter = self.adapter)

        try:
            await hook.edit_message(proxied.id, content = content)
        except discord.errors.NotFound:
            self.execute('delete from webhooks where chanid = ?', (channel.id,))
            return await self.mark_success(message, False)

        if self.has_perm(message, manage_messages = True):
            await message.delete()

        logchan = self.fetchone('select logchan from guilds where guildid = ?',
                (message.guild.id,))
        if logchan:
            logchan = logchan[0]
            embed = discord.Embed(description = content,
                    timestamp = discord.utils.snowflake_time(message.id))
            embed.add_field(
                    name = 'Old message',
                    value = proxied.content,
                    inline = False)
            embed.set_author(
                    name = '[Edited] #%s: %s' % (channel.name,
                        proxied.author.display_name),
                    icon_url = proxied.author.avatar_url)
            embed.set_thumbnail(url = proxied.author.avatar_url)
            embed.set_footer(text =
                    'Sender: %s (%i) | '
                    'Message ID: %i | '
                    'Original Message ID: %i'
                    % (str(message.author), message.author.id, proxied.id,
                        message.id))
            try:
                await self.get_channel(logchan).send(proxied.jump_url,
                        embed = embed)
            except:
                pass


    async def cmd_become(self, message, proxy):
        # self.execute('update proxies set become = 1.0 where userid = ?',
        #         (message.author.id,))
        self.set_proxy_auto(proxy, True)
        self.execute(
                'update proxies set become = 0.0 '
                'where (userid, proxid) = (?, ?)',
                (message.author.id, proxy['proxid']))
        await self.mark_success(message, True)


    async def cmd_log_channel(self, message, channel):
        self.execute('insert or replace into guilds values (?, ?)',
                (message.guild.id, channel.id))
        await self.mark_success(message, True)


    async def cmd_log_disable(self, message):
        self.execute('delete from guilds where guildid = ?',
                (message.guild.id,))
        await self.mark_success(message, True)


    async def pk_api_get(self, url):
        try:
            async with self.session.get(PK_ENDPOINT + url,
                    timeout = aiohttp.ClientTimeout(total = 5.0)) as r:
                if r.status != 200:
                    raise RuntimeError(ERROR_PKAPI)
                response = await r.text(encoding = 'UTF-8')
                try:
                    return json.loads(response)
                except json.decoder.JSONDecodeError:
                    raise RuntimeError(ERROR_PKAPI)
        except asyncio.TimeoutError:
            raise RuntimeError('Could not reach PluralKit API.')


    async def cmd_pk_swap(self, message, swap, pkhid):
        async with self.in_progress(message):
            system = await self.pk_api_get('/systems/' + str(swap['userid']))
            member = await self.pk_api_get('/members/' + pkhid)
        try:
            if system['id'] != member['system']:
                raise RuntimeError('That member is not in your system.')
            # in the unlikely that PK goes rogue and tries to mess with us
            if len(member['uuid']) == 5:
                raise RuntimeError(ERROR_PKAPI)
            # it would be really nice to just check the pkhid in the command
            # that way we could check if the proxy exists as the first step
            # unfortunately, pkhids are NOT guaranteed to be constant!
            # therefore, we're forced to use the pkuuid...
            # NB: a pk system may be attached to multiple accounts
            if self.fetchone(
                    'select 1 from proxies '
                    'where (userid, maskid, type, state) = (?, ?, ?, ?)',
                    (swap['otherid'], member['uuid'], ProxyType.pkswap,
                        ProxyState.active)):
                return
            if swap['cmdname']:
                receipt = '%s\'s %s' % (swap['cmdname'], member['name'])
            else:
                receipt = '%s (Receipt)' % member['name']
            proxid = self.mkproxy(swap['otherid'], ProxyType.pkswap,
                    cmdname = member['name'], otherid = swap['userid'],
                    maskid = member['uuid'])
            if swap['userid'] != swap['otherid']:
                self.mkproxy(swap['userid'], ProxyType.pkreceipt,
                        cmdname = receipt, otherid = swap['otherid'],
                        maskid = proxid, state = ProxyState.inactive)
        except KeyError:
            raise RuntimeError(ERROR_PKAPI)

        await self.mark_success(message, True)


    async def cmd_pk_close(self, message, proxy):
        if proxy['type'] == ProxyType.pkreceipt:
            self.execute('delete from proxies where proxid in (?, ?)',
                    (proxy['proxid'], proxy['maskid']))
        else: # pkswap
            self.execute(
                    'delete from proxies where (proxid = ?) '
                    'or (userid, maskid) = (?, ?)', # uses index; faster
                    (proxy['proxid'], proxy['otherid'], proxy['proxid']))

        await self.mark_success(message, True)


    async def cmd_pk_sync(self, message):
        ref = message.reference
        ref = ref.cached_message or await message.channel.fetch_message(
                ref.message_id)
        if not (ref and ref.webhook_id):
                raise RuntimeError('Please reply to a proxied message.')

        async with self.in_progress(message):
            proxied = await self.pk_api_get('/messages/' + str(ref.id))
        try:
            pkuuid = proxied['member']['uuid']
        except KeyError:
            raise RuntimeError(ERROR_PKAPI)

        exists = self.fetchone(
                'select 1 from proxies where (type, maskid, state) = (?, ?, ?)',
                (ProxyType.pkswap, pkuuid, ProxyState.active))
        if not exists:
            raise RuntimeError('That member has no Gestalt proxies.')

        mask = self.fetchone(
                'select updated from masks '
                'where (maskid, guildid) = (?, ?)',
                ('pk-' + pkuuid, message.guild.id))
        if mask and mask['updated'] > ref.id:
            raise RuntimeError('Please use a more recent proxied message.')

        self.execute(
                'insert or replace into masks values (?, ?, NULL, ?, ?, ?, ?)',
                ('pk-' + pkuuid, message.guild.id, ref.author.display_name,
                    str(ref.author.avatar_url), ProxyType.pkswap, ref.id))

        await self.mark_success(message, True)


    # parse, convert, and validate arguments, then call the relevant function
    async def do_command(self, message, cmd):
        reader = CommandReader(message, cmd)
        arg = reader.read_word().lower()
        authid = message.author.id

        if arg == 'help':
            topic = reader.read_word()
            return await self.cmd_help(message, topic)

        elif arg == 'invite':
            return await self.cmd_invite(message)

        elif arg == 'permcheck':
            guildid = reader.read_word()
            if re.search('[^0-9]', guildid) or not (guildid or message.guild):
                raise RuntimeError('Please provide a valid guild ID.')
            return await self.cmd_permcheck(message, guildid)

        elif arg in ['proxy', 'p']:
            name = reader.read_quote()

            if name == '':
                return await self.cmd_proxy_list(message)

            arg = reader.read_word().lower()
            proxy = self.get_user_proxy(message, name)

            if arg == 'tags':
                arg = reader.read_remainder()
                return await self.cmd_proxy_tags(message, proxy['proxid'], arg)

            elif arg == 'auto':
                if reader.is_empty():
                    val = None
                else:
                    val = reader.read_bool_int()
                    if val == None:
                        raise RuntimeError('Please specify "on" or "off".')
                return await self.cmd_proxy_auto(message, proxy, val)

            elif arg == 'rename':
                newname = reader.read_remainder()
                if not newname:
                    raise RuntimeError('Please provide a new name.')
                return await self.cmd_proxy_rename(message, proxy['proxid'],
                        newname)

            elif arg == 'keepproxy':
                keep = reader.read_bool_int()
                return await self.cmd_proxy_keepproxy(message, proxy, keep)

        elif arg in ['collective', 'c']:
            if not message.guild:
                raise RuntimeError(ERROR_DM)
            guild = message.guild
            arg = reader.read_quote()

            if arg == '':
                return await self.cmd_collective_list(message)

            elif arg.lower() in ['new', 'create']:
                if not message.author.guild_permissions.manage_roles:
                    raise RuntimeError(ERROR_MANAGE_ROLES)

                role = reader.read_role()
                if role == None:
                    raise RuntimeError('Please provide a role.')

                if role.managed:
                    # bots, server booster, integrated subscription services
                    # requiring users to pay to participate is antithetical
                    # to community-oriented identity play
                    raise RuntimeError(ERROR_CURSED)

                return await self.cmd_collective_new(message, role)

            else: # arg is collective ID/proxy name
                collid = arg
                action = reader.read_word().lower()

                try:
                    # if get_user_proxy succeeds, ['maskid'] must exist
                    collid = self.get_user_proxy(message, collid)['maskid']
                except RuntimeError:
                    pass # could save error, but would be confusing
                row = self.fetchone('select * from masks where maskid = ?',
                        (collid,))
                # non-collective masks shouldn't have visible ids
                # but check just to be safe
                if row == None or row['type'] != ProxyType.collective:
                    raise RuntimeError('Collective not found.')
                if row['guildid'] != guild.id:
                    raise RuntimeError(
                            'That collective belongs to another guild.')

                if action in ['name', 'avatar']:
                    arg = reader.read_remainder()

                    role = guild.get_role(row['roleid'])
                    if role == None:
                        raise RuntimeError('That role no longer exists?')

                    member = message.author # Member because this isn't a DM
                    if not (role in member.roles
                            or member.guild_permissions.manage_roles):
                        raise RuntimeError(
                                'You don\'t have access to that collective!')

                    # allow empty avatar URL but not name
                    if action == 'name' and not arg:
                        raise RuntimeError('Please provide a new name.')
                    if action == 'avatar':
                        if message.attachments and not arg:
                            arg = message.attachments[0].url
                        elif arg and not re.match('http(s?)://.*', arg):
                            raise RuntimeError('Invalid avatar URL!')

                    return await self.cmd_collective_update(message, collid,
                            action, arg)

                elif action == 'delete':
                    if not message.author.guild_permissions.manage_roles:
                        raise RuntimeError(ERROR_MANAGE_ROLES)
                    # all the more reason to delete it then, right?
                    # if guild.get_role(row[1]) == None:

                    return await self.cmd_collective_delete(message, row)

        elif arg == 'prefs':
            # user must exist due to on_message
            user = self.fetchone(
                    'select * from users where userid = ?',
                    (authid,))
            arg = reader.read_word()
            if len(arg) == 0:
                return await self.cmd_prefs_list(message, user)

            if arg in ['default', 'defaults']:
                return await self.cmd_prefs_default(message)

            if not arg in Prefs.__members__.keys():
                raise RuntimeError('That preference does not exist.')

            if reader.is_empty():
                value = None
            else:
                value = reader.read_bool_int()
                if value == None:
                    raise RuntimeError('Please specify "on" or "off".')

            return await self.cmd_prefs_update(message, user, arg, value)

        elif arg in ['swap', 's']:
            arg = reader.read_word().lower()
            if arg == 'open':
                if not message.guild:
                    raise RuntimeError(ERROR_DM)

                member = reader.read_member()
                if member == None:
                    raise RuntimeError('User not found.')
                tags = reader.read_remainder() or None

                if member.id == self.user.id:
                    raise RuntimeError(ERROR_BLURSED)
                if member.bot:
                    raise RuntimeError(ERROR_CURSED)

                try:
                    return await self.cmd_swap_open(message, member, tags)
                except IntegrityError:
                    raise RuntimeError(ERROR_TAGS)

            elif arg in ['close', 'off']:
                name = reader.read_quote()
                proxy = self.get_user_proxy(message, name)
                if proxy['type'] != ProxyType.swap:
                    raise RuntimeError(
                            'You do not have a swap with that ID.')

                return await self.cmd_swap_close(message, proxy)

        elif arg in ['edit', 'e']:
            content = reader.read_remainder()
            return await self.cmd_edit(message, content)

        elif arg in ['become', 'bc']:
            proxy = self.get_user_proxy(message, reader.read_quote())
            if (proxy['type'] == ProxyType.override):
                raise RuntimeError('You are already yourself!')
            if proxy['state'] != ProxyState.active:
                raise RuntimeError('That proxy is not active.')

            return await self.cmd_become(message, proxy)

        elif arg in ['pluralkit', 'pk']:

            arg = reader.read_word()
            if arg == 'swap':
                swap = self.get_user_proxy(message, reader.read_quote())
                if (swap['type'] != ProxyType.swap
                        or swap['state'] != ProxyState.active):
                    raise RuntimeError('Please provide an active swap.')
                pkid = reader.read_word()

                return await self.cmd_pk_swap(message, swap, pkid)

            elif arg == 'close':
                swap = self.get_user_proxy(message, reader.read_quote())
                if swap['type'] not in (ProxyType.pkreceipt, ProxyType.pkswap):
                    raise RuntimeError('Please provide a swap receipt.')

                return await self.cmd_pk_close(message, swap)

            elif arg == 'sync':
                if not message.guild:
                    raise RuntimeError(ERROR_DM)
                if not message.reference:
                    raise RuntimeError('Please reply to a proxied message.')

                return await self.cmd_pk_sync(message)

        elif arg == 'log':
            if not message.guild:
                raise RuntimeError(ERROR_DM)
            if not message.author.guild_permissions.administrator:
                raise RuntimeError(
                        'You need `Manage Server` permissions to do that.')

            arg = reader.read_word().lower()
            if arg == 'channel':
                arg = reader.read_remainder()
                if message.channel_mentions:
                    channel = message.channel_mentions[0]
                else:
                    channel = discord.utils.get(
                            self.get_guild(message.guild.id).channels,
                            name = arg)
                    if not channel:
                        raise RuntimeError('Please provide a channel.')

                return await self.cmd_log_channel(message, channel)

            if arg == 'disable':
                return await self.cmd_log_disable(message)

        elif arg == 'explain':
            if self.has_perm(message, send_messages = True):
                reply = await message.channel.send(EXPLAIN)
                self.execute('insert into history values (?, 0, ?, NULL, NULL)',
                        (reply.id, authid))
                return

