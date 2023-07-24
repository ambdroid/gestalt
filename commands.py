import json
import asyncio

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

    def try_read_quote(self):
        if not self.is_empty() and (regex := QUOTE_REGEXES.get(self.cmd[0])):
            if match := regex.fullmatch(self.cmd):
                self.cmd = match.group(2).strip()
                return match.group(1)

    def read_quote(self):
        return self.try_read_quote() or self.read_word()

    def read_bool_int(self):
        return self.BOOL_KEYWORDS.get(self.read_word().lower())

    def read_remainder(self):
        if quote := self.try_read_quote():
            return quote
        (ret, self.cmd) = (self.cmd, '')
        return ret

    # discord.ext includes a MemberConverter
    # but that's only available whem using discord.ext Command
    def read_member(self):
        if self.msg.mentions:
            # consume the text of the mention
            _ = self.read_word()
            return self.msg.mentions[0]

    def read_role(self):
        name = self.read_quote()
        if self.msg.role_mentions:
            return self.msg.role_mentions[0]
        guild = self.msg.guild
        if name == 'everyone':
            return guild.default_role
        return discord.utils.get(guild.roles, name = name)

    def read_channel(self):
        _ = self.read_word() # discard
        if self.msg.channel_mentions:
            chan = self.msg.channel_mentions[0]
            if chan.guild == self.msg.guild:
                return chan


class GestaltCommands:
    def get_user_proxy(self, message, name):
        if name == '':
            raise RuntimeError('Please provide a proxy name/ID.')

        # can't do 'and ? in (proxid, cmdname)'; breaks case insensitivity
        proxies = self.fetch_valid_proxies(
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
        for chan in guild.channels:
            if chan.type not in ALLOWED_CHANNELS:
                continue
            if not chan.permissions_for(memberauth).view_channel:
                continue

            errors = []
            for p in PERMS: # p = ('name', bool)
                if p[1] and not p in list(chan.permissions_for(memberbot)):
                    errors += [p[0]]

            # lack of access implies lack of other perms, so leave them out
            if 'read_messages' in errors:
                errors = ['read_messages']
            errors = REACT_CONFIRM if errors == [] else ', '.join(errors)
            lines.append('%s: %s' % (chan.mention, errors))

        await self.send_embed(message, '\n'.join(lines))


    def proxy_string(self, proxy):
        line = '[`%s`] ' % proxy['proxid']
        if proxy['cmdname']:
            line += '**%s**' % escape(proxy['cmdname'])
        else:
            line += '*no name*'

        parens = ''
        if proxy['type'] == ProxyType.override:
            line = SYMBOL_OVERRIDE + line
        elif proxy['type'] == ProxyType.swap:
            line = SYMBOL_SWAP + line
            user = self.get_user(proxy['otherid'])
            if not user:
                return
            parens = 'with **%s**' % escape(user)
        elif proxy['type'] == ProxyType.collective:
            line = SYMBOL_COLLECTIVE + line
            guild = self.get_guild(proxy['guildid'])
            if not guild or not (role := guild.get_role(proxy['roleid'])):
                return
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
            parens += ' `%s`' % (
                    ('`%stext%s`' % (proxy['prefix'], proxy['postfix']))
                    .replace('``', '`\N{ZWNBSP}`')
                    .replace('``', '`\N{ZWNBSP}`'))
        if proxy['state'] == ProxyState.inactive:
            parens += ' *(inactive)*'

        if parens and proxy['type'] != ProxyType.pkreceipt:
            line += ' (%s)' % parens.strip()
        return line


    async def cmd_proxy_list(self, message, all_):
        rows = sorted(self.fetch_valid_proxies(
                'select proxies.*, masks.roleid, masks.nick from '
                    'proxies left join masks using (maskid) '
                'where proxies.userid = ?',
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
            if message.guild and not (all_
                    or self.proxy_visible_in(proxy, message.guild)):
                omit = True
            elif line := self.proxy_string(proxy):
                lines.append(line)

        if omit:
            lines.append('Proxies in other servers have been omitted.')
            lines.append('To view all proxies, use `proxy list -all`.')
        await self.send_embed(message, '\n'.join(lines))


    async def cmd_proxy_tags(self, message, proxy, tags):
        (prefix, postfix) = parse_tags(tags)
        if prefix is not None and self.get_tags_conflict(message.author.id,
            proxy['guildid'], (prefix, postfix)) not in ([proxy['proxid']], []):
            raise RuntimeError(ERROR_TAGS)

        self.execute(
                'update proxies set prefix = ?, postfix = ? where proxid = ?',
                (prefix, postfix, proxy['proxid']))

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


    async def cmd_autoproxy_view(self, message):
        ap = self.fetchone(
                'select members.latch, members.become, proxies.*, '
                'masks.roleid, masks.nick from '
                    'members '
                    'left join proxies using (proxid) '
                    'left join masks using (maskid) '
                'where (members.userid, members.guildid) = (?, ?)',
                (message.author.id, message.guild.id))
        # NOTE: valid == False if proxy has been deleted
        # the lack of joined row from proxies sets fetched proxid to NULL
        if (valid := ap and ap['proxid']):
            if not (valid := self.proxy_usable_in(ap, message.guild)):
                self.set_autoproxy(message.author, None)
        proxy_string = valid and self.proxy_string(ap)

        lines = []
        if ap:
            if ap['latch']:
                lines.append('Your autoproxy is set to latch in this server.')
                if proxy_string:
                    lines.append('Your current latched proxy is:')
                else:
                    lines.append('However, no proxy is latched.')
            if proxy_string:
                if not ap['latch']:
                    lines.append('Your autoproxy in this server is set to:')
                lines.append(proxy_string)
                if ap['become'] < 1.0:
                    lines.append('This proxy is in Become mode (%i%%).'
                            % int(ap['become'] * 100))
        if not lines:
            lines.append('You have no autoproxy enabled in this server.')
        lines.append('For more information, please see `%shelp proxy`.'
                % COMMAND_PREFIX)

        await self.send_embed(message, '\n'.join(lines))


    async def cmd_autoproxy_set(self, message, arg):
        member = message.author
        if arg in ['off', 'latch', 'l']:
            self.set_autoproxy(member, None, latch = -1 * int(arg != 'off'))
        else:
            proxy = self.get_user_proxy(message, arg)
            if proxy['type'] == ProxyType.override:
                raise RuntimeError('You can\'t autoproxy your override.')
            if not self.proxy_usable_in(proxy, message.guild):
                raise RuntimeError('You can\'t use that proxy in this guild.')
            self.set_autoproxy(member, proxy['proxid'], latch = 0)

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
                    (row['maskid'].upper(),
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
                '(?, ?, ?, ?, NULL, NULL, ?, NULL)',
                (collid, role.guild.id, role.id, name, ProxyType.collective))
        # if there wasn't already a collective on that role
        if self.cur.rowcount == 1:
            for member in role.members:
                if not member.bot:
                    self.mkproxy(member.id, ProxyType.collective,
                            cmdname = name, guildid = role.guild.id,
                            otherid = role.id, maskid = collid)

            await self.mark_success(message, True)


    async def cmd_collective_update(self, message, collid, name, value):
        if name not in ['nick', 'name', 'avatar', 'color', 'colour']:
            return
        self.execute(
                'update masks set %s = ? '
                'where maskid = ?'
                % {'name': 'nick', 'colour': 'color'}.get(name, name),
                (value, collid))
        if self.cur.rowcount == 1:
            await self.mark_success(message, True)


    async def cmd_collective_delete(self, message, coll):
        self.execute('delete from proxies where maskid = ?', (coll['maskid'],))
        self.execute('delete from masks where maskid = ?', (coll['maskid'],))
        if self.cur.rowcount == 1:
            await self.mark_success(message, True)


    async def cmd_config_list(self, message, user):
        # list current settings in 'setting: [on/off]' format
        text = '\n'.join(['%s: **%s**' %
                (pref.name, 'on' if user['prefs'] & pref else 'off')
                for pref in Prefs])
        await self.send_embed(message, text)


    async def cmd_config_default(self, message):
        self.execute(
                'update users set prefs = ? where userid = ?',
                (DEFAULT_PREFS, message.author.id))
        await self.mark_success(message, True)


    async def cmd_config_update(self, message, user, name, value):
        bit = int(Prefs[name])
        if value == None: # only 'config' + name given. invert the thing
            prefs = user['prefs'] ^ bit
        else:
            prefs = (user['prefs'] & ~bit) | (bit * value)
        self.execute(
                'update users set prefs = ? where userid = ?',
                (prefs, message.author.id))

        await self.mark_success(message, True)


    async def cmd_account_update(self, message, value):
        self.execute('update users set color = ? where userid = ?',
                (value, message.author.id))
        await self.mark_success(message, True)


    def make_or_activate_swap(self, auth, other, tags):
        (prefix, postfix) = parse_tags(tags) if tags else (None, None)
        if self.fetchone(
                'select state from proxies '
                'where (userid, otherid, type) = (?, ?, ?)',
                (auth.id, other.id, ProxyType.swap)):
            return False
        # look at proxies from target, not author
        swap = self.fetchone(
                'select proxid, state from proxies '
                'where (userid, otherid, type) = (?, ?, ?)',
                (other.id, auth.id, ProxyType.swap))
        self.mkproxy(auth.id, ProxyType.swap, cmdname = other.name,
                prefix = prefix, postfix = postfix, otherid = other.id,
                state = (ProxyState.active if auth.id == other.id or swap
                    else ProxyState.inactive))
        if swap:
            # target is initiator. author can activate swap
            self.execute('update proxies set state = ? where proxid = ?',
                    (ProxyState.active, swap['proxid']))

        return True


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
        if not content:
            raise RuntimeError('We need a message here!')
        channel = message.channel
        (thread, channel) = ((channel, channel.parent)
                if type(channel) == discord.Thread
                else (discord.utils.MISSING, channel))
        if message.reference:
            proxied = self.fetchone(
                    'select msgid, authid from history where msgid = ?',
                    (message.reference.message_id,))
        else:
            proxied = self.fetchone(
                    'select msgid, authid from history '
                    'where (chanid, threadid, authid) = (?, ?, ?)'
                    'order by msgid desc limit 1',
                    (channel.id, thread.id if thread else 0,
                        message.author.id))
        if not proxied or proxied['authid'] != message.author.id:
            return await self.mark_success(message, False)
        try:
            proxied = await (thread or channel).fetch_message(proxied['msgid'])
        except discord.errors.NotFound:
            return await self.mark_success(message, False)

        hook = await self.get_webhook(channel)
        if not hook or proxied.webhook_id != hook.id:
            return await self.mark_success(message, False)

        try:
            edited = await hook.edit_message(proxied.id,
                    content = self.maybe_remove_embeds(message, content),
                    thread = thread,
                    allowed_mentions = discord.AllowedMentions(
                        everyone = channel.permissions_for(
                            message.author).mention_everyone))
        except discord.errors.NotFound:
            await self.confirm_webhook_deletion(hook)
            return await self.mark_success(message, False)

        await self.try_delete(message)

        await self.make_log_message(edited, message, old = proxied)


    async def cmd_become(self, message, proxy):
        self.set_autoproxy(message.author, proxy['proxid'], become = 0.0)
        await self.mark_success(message, True)


    async def cmd_log_channel(self, message, channel):
        self.execute('insert or replace into guilds values (?, ?)',
                (message.guild.id, channel.id))
        await self.mark_success(message, True)


    async def cmd_log_disable(self, message):
        self.execute('delete from guilds where guildid = ?',
                (message.guild.id,))
        await self.mark_success(message, True)


    async def cmd_channel_mode(self, message, channel, mode):
        # blacklist = 0, log = 1
        self.execute('insert or ignore into channels values (?, ?, 0, 1, ?)',
                (channel.id, channel.guild.id, ChannelMode.default))
        self.execute('update channels set mode = ? where chanid = ?',
                (ChannelMode[mode], channel.id))
        await self.mark_success(message, True)


    async def pk_api_get(self, url):
        await self.pk_ratelimit.block()
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

        mask = self.fetchone(
                'select * from proxies where (type, maskid, state) = (?, ?, ?)',
                (ProxyType.pkswap, pkuuid, ProxyState.active))
        if not mask:
            raise RuntimeError('That member has no Gestalt proxies.')

        mask = self.fetchone(
                'select color, updated from masks '
                'where (maskid, guildid) = (?, ?)',
                ('pk-' + pkuuid, message.guild.id))
        if mask and mask['updated'] > ref.id:
            raise RuntimeError('Please use a more recent proxied message.')

        self.execute(
                'insert or replace into masks values '
                '(?, ?, NULL, ?, ?, ?, ?, ?)',
                ('pk-' + pkuuid, message.guild.id, ref.author.display_name,
                    str(ref.author.display_avatar),
                    mask['color'] if mask else None,
                    ProxyType.pkswap, ref.id))
        try:
            # if pk color is null, keep it None
            if (color := proxied['member']['color']) is not None:
                # color is hex string without '#'
                color = str(discord.Color.from_str('#' + color))
            if not mask or mask['color'] != color:
                # colors aren't set per-server, so set it everywhere
                # (even if the message is older, pk returns the current color)
                self.execute('update masks set color = ? where maskid = ?',
                        (color, 'pk-' + pkuuid))
        except (KeyError, ValueError, TypeError):
            pass

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

            if name in ['', 'list']:
                return await self.cmd_proxy_list(message,
                        reader.read_remainder() == '-all')

            arg = reader.read_word().lower()
            proxy = self.get_user_proxy(message, name)

            if arg == 'tags':
                arg = reader.read_remainder()
                return await self.cmd_proxy_tags(message, proxy, arg)

            elif arg == 'auto':
                # removed command
                return await self.cmd_help(message, 'autoproxy')

            elif arg == 'rename':
                newname = reader.read_remainder()
                if not newname:
                    raise RuntimeError('Please provide a new name.')
                return await self.cmd_proxy_rename(message, proxy['proxid'],
                        newname)

            elif arg == 'keepproxy':
                keep = reader.read_bool_int()
                return await self.cmd_proxy_keepproxy(message, proxy, keep)

        elif arg in ['autoproxy', 'ap']:
            if not message.guild:
                raise RuntimeError(ERROR_DM)

            if arg := reader.read_remainder():
                return await self.cmd_autoproxy_set(message, arg)
            return await self.cmd_autoproxy_view(message)

        elif arg in ['collective', 'c']:
            if not message.guild:
                raise RuntimeError(ERROR_DM)
            guild = message.guild
            arg = reader.read_quote()

            if arg in ['', 'list']:
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

                if action in ['nick', 'name', 'avatar', 'color', 'colour']:
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
                    if action in ['name', 'nick'] and not arg:
                        raise RuntimeError('Please provide a new name.')
                    if action == 'avatar':
                        if message.attachments and not arg:
                            arg = message.attachments[0].url
                        elif arg and not LINK_REGEX.fullmatch(arg):
                            raise RuntimeError('Invalid avatar URL!')
                    if action in ['color', 'colour']:
                        if arg == '-clear':
                            arg = None
                        else:
                            arg = NAMED_COLORS.get(arg.lower(), arg)
                            try:
                                arg = str(discord.Color.from_str(arg))
                            except ValueError:
                                raise RuntimeError(
                                        'Please enter a color (e.g. `#012345`)')

                    return await self.cmd_collective_update(message, collid,
                            action, arg)

                elif action == 'delete':
                    if not message.author.guild_permissions.manage_roles:
                        raise RuntimeError(ERROR_MANAGE_ROLES)
                    # all the more reason to delete it then, right?
                    # if guild.get_role(row[1]) == None:

                    return await self.cmd_collective_delete(message, row)

        elif arg in ['account', 'a']:
            arg = reader.read_word().lower()
            if arg == 'config':
                # user must exist due to on_message
                user = self.fetchone(
                        'select * from users where userid = ?',
                        (authid,))
                arg = reader.read_word()
                if len(arg) == 0:
                    return await self.cmd_config_list(message, user)

                if arg in ['default', 'defaults']:
                    return await self.cmd_config_default(message)

                if not arg in Prefs.__members__.keys():
                    raise RuntimeError('That setting does not exist.')

                if reader.is_empty():
                    value = None
                else:
                    value = reader.read_bool_int()
                    if value == None:
                        raise RuntimeError('Please specify "on" or "off".')

                return await self.cmd_config_update(message, user, arg, value)

            elif arg in ['color', 'colour']:
                arg = reader.read_word()
                if arg == '-clear':
                    arg = None
                else:
                    arg = NAMED_COLORS.get(arg.lower(), arg)
                    try:
                        arg = str(discord.Color.from_str(arg))
                    except ValueError:
                        raise RuntimeError(
                                'Please enter a color (e.g. `#012345`)')

                return await self.cmd_account_update(message, arg)


        elif arg in ['swap', 's']:
            arg = reader.read_word().lower()
            if arg == 'open':
                if not message.guild:
                    raise RuntimeError(ERROR_DM)

                if (member := reader.read_member()) is None:
                    raise RuntimeError('User not found.')
                tags = reader.read_remainder() or None

                if member.id == self.user.id:
                    raise RuntimeError(ERROR_BLURSED)
                if member.bot:
                    raise RuntimeError(ERROR_CURSED)

                return await self.cmd_swap_open(message, member, tags)

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
                channel = reader.read_channel()
                if not channel:
                    raise RuntimeError('Please mention a channel.')

                return await self.cmd_log_channel(message, channel)

            if arg == 'disable':
                return await self.cmd_log_disable(message)

        elif arg == 'channel':
            if not message.guild:
                raise RuntimeError(ERROR_DM)
            if not message.author.guild_permissions.manage_channels:
                raise RuntimeError(
                        'You need `Manage Channels` permissions to do that.')

            channel = reader.read_channel()
            if not channel:
                raise RuntimeError('Please mention a channel.')

            arg = reader.read_word()
            if arg == 'mode':
                mode = reader.read_word()
                if mode not in ChannelMode.__members__.keys():
                    raise RuntimeError('Invalid channel mode.')

                return await self.cmd_channel_mode(message, channel, mode)

        elif arg == 'explain':
            if self.has_perm(message, send_messages = True):
                reply = await message.channel.send(EXPLAIN)
                self.mkhistory(reply, message.author)
                return

