import json
import time
import asyncio
import datetime
import os

import aiohttp
import discord

from defs import *
import gesp


def escape(text):
    return discord.utils.escape_markdown(
            discord.utils.escape_mentions(str(text)))


# [text] -> ['[',']']
def parse_tags(tags):
    split = tags.lower().split('text')
    if len(split) != 2 or not ''.join(split):
        raise UserError(
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

    def read_clear(self):
        if self.cmd.startswith('-clear'):
            self.cmd = self.cmd.removeprefix('-clear').strip()
            return CLEAR

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

    def read_channel(self):
        _ = self.read_word() # discard
        if self.msg.channel_mentions:
            chan = self.msg.channel_mentions[0]
            if chan.guild == self.msg.guild:
                return chan

    def read_link(self):
        if m := LINK_REGEX.match(self.cmd):
            self.cmd = self.cmd.removeprefix(m[0]).strip()
            return m[1] # excluding <...> if present

    def read_color(self):
        name = self.read_word()
        try:
            return str(discord.Color.from_str(
                NAMED_COLORS.get(name.lower(), name)))
        except (ValueError, IndexError):
            pass


class GestaltCommands:
    def get_user_proxy(self, message, name):
        if name == '':
            raise UserError('Please provide a proxy name/ID.')

        # can't do 'and ? in (proxid, cmdname)'; breaks case insensitivity
        proxies = self.fetchall(
                'select * from proxies where userid = ? '
                'and (proxid = ? or cmdname = ?) '
                'and state != ?',
                (message.author.id, name, name, ProxyState.hidden))

        if not proxies:
            raise UserError('You have no proxy with that name/ID.')
        if len(proxies) > 1:
            raise UserError('You have multiple proxies with that name/ID.')

        return proxies[0]


    async def cmd_help(self, message, topic):
        await self.reply(message, HELPMSGS.get(topic, HELPMSGS['']))


    async def cmd_invite(self, message):
        if (await self.application_info()).bot_public:
            await self.reply(message,
                    discord.utils.oauth_url(self.user.id, permissions = PERMS))


    async def cmd_permcheck(self, message, guildid):
        guildid = message.guild.id if guildid == '' else int(guildid)
        guild = self.get_guild(guildid)
        if guild == None:
            raise UserError('That guild does not exist or I am not in it.')
        if guild.get_member(message.author.id) == None:
            raise UserError('You are not a member of that guild.')

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

        await self.reply(message, '\n'.join(lines))


    def proxy_string(self, proxy):
        line = '%s[`%s`] ' % (ProxySymbol[proxy['type']], proxy['proxid'])
        if proxy['cmdname']:
            line += '**%s**' % escape(proxy['cmdname'])
        else:
            line += '*no name*'

        parens = ''
        if proxy['type'] == ProxyType.swap:
            user = self.get_user(proxy['otherid'])
            if not user:
                return
            parens = 'with **%s**' % escape(user)
        elif proxy['type'] == ProxyType.pkswap:
            # we don't have pkhids
            # parens = 'PluralKit member **%s**' % proxy['maskid']
            parens = ''
        elif proxy['type'] == ProxyType.mask:
            parens = '**%s** on [`%s`]' % (escape(proxy['nick']),
                    proxy['maskid'].upper())

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
        rows = sorted(self.fetchall(
                'select proxies.*, masks.nick from proxies '
                'left join masks using (maskid) '
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
        await self.reply(message, '\n'.join(lines))


    async def cmd_proxy_tags(self, message, proxy, tags):
        (prefix, postfix) = parse_tags(tags)
        if prefix is not None and self.get_tags_conflict(message.author.id,
            (prefix, postfix)) not in ([proxy['proxid']], []):
            raise UserError(ERROR_TAGS)

        self.execute(
                'update proxies set prefix = ?, postfix = ? where proxid = ?',
                (prefix, postfix, proxy['proxid']))

        await self.mark_success(message, True)


    async def cmd_proxy_rename(self, message, proxid, newname):
        self.execute('update proxies set cmdname = ? where proxid = ?',
                (newname, proxid))

        await self.mark_success(message, True)


    async def cmd_proxy_flag(self, message, proxy, name, value):
        bit = int(ProxyFlags[name])
        self.execute(
                'update proxies set flags = ? where proxid = ?',
                ((proxy['flags'] & ~bit) | (bit * value), proxy['proxid']))

        await self.mark_success(message, True)


    async def cmd_autoproxy_view(self, message):
        ap = self.fetchone(
                'select members.latch, members.become, proxies.*, masks.nick '
                'from members '
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

        await self.reply(message, '\n'.join(lines))


    async def cmd_autoproxy_set(self, message, arg, and_proxy = False):
        member = message.author
        if arg in ['off', 'latch', 'l']:
            self.set_autoproxy(member, None, latch = -1 * int(arg != 'off'))
        else:
            proxy = self.get_user_proxy(message, arg)
            if proxy['type'] == ProxyType.override:
                raise UserError('You can\'t autoproxy your override.')
            if not self.proxy_usable_in(proxy, message.guild):
                raise UserError('You can\'t use that proxy in this guild.')
            self.set_autoproxy(member, proxy['proxid'], latch = 0)
            if and_proxy:
                return await self.do_proxy(message,
                        '\\> [__Be %s.__](%s)' % (arg,
                            message.channel.jump_url),
                        dict(proxy) | {'become': 1.0},
                        self.fetchone(
                            'select prefs from users where userid = ?',
                            (member.id,))[0])

        await self.mark_success(message, True)


    async def cmd_config_list(self, message, user):
        # list current settings in 'setting: [on/off]' format
        text = '\n'.join(['%s: **%s**' %
                (pref.name, 'on' if user['prefs'] & pref else 'off')
                for pref in Prefs])
        await self.reply(message, text)


    async def cmd_config_default(self, message):
        self.execute(
                'update users set prefs = ? where userid = ?',
                (DEFAULT_PREFS, message.author.id))
        await self.mark_success(message, True)


    async def cmd_config_update(self, message, user, name, value):
        bit = int(Prefs[name])
        self.execute(
                'update users set prefs = ? where userid = ?',
                ((user['prefs'] & ~bit) | (bit * value), message.author.id))

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
                'where ('
                    '(userid, otherid) = (?, ?) or (otherid, userid) = (?, ?)'
                ') and type = ?',
                (proxy['userid'], proxy['otherid'])*2 + (ProxyType.swap,))

        await self.mark_success(message, True)


    async def cmd_mask_new(self, message, name):
        await self.initiate_vote(gesp.VoteCreate(
            user = message.author.id,
            name = name,
            context = gesp.ProgramContext.from_message(message)))


    async def cmd_mask_view(self, message, mask):
        embed = discord.Embed()
        avatar = self.hosted_avatar_fix(mask['avatar'])
        embed.set_author(name = mask['nick'], icon_url = avatar)
        embed.set_thumbnail(url = avatar)
        embed.add_field(name = 'Members', value = mask['members'])
        # assume that masks too old for a creation date have incomplete count
        embed.add_field(name = 'Message Count', value = '%i%s' %
                (mask['msgcount'], '' if mask['created'] else '+'))
        embed.add_field(name = 'Rules', value =
                discord.utils.get(RuleType, value =
                    json.loads(mask['rules'])['type']).name)
        if mask['color']:
            embed.color = discord.Color.from_str(mask['color'])
            embed.add_field(name = 'Color', value = mask['color'])
        embed.set_footer(text = 'Mask ID: %s%s' %
                (mask['maskid'].upper(), (' | Created on %s UTC' %
                    datetime.datetime.utcfromtimestamp(int(mask['created']))
                    ) if mask['created'] else ''))
        await self.reply(message, embeds = [embed])


    async def cmd_mask_join(self, message, maskid):
        if await self.initiate_action(
                gesp.ProgramContext.from_message(message),
                gesp.ActionJoin(maskid, message.author.id)):
            await self.mark_success(message, True)


    async def cmd_mask_invite(self, message, maskid, member):
        await self.initiate_vote(gesp.VotePreinvite(
            mask = maskid, user = member.id,
            context = gesp.ProgramContext.from_message(message)))


    async def cmd_mask_remove(self, message, maskid, member):
        # TODO: require replacement member when candidate is named
        # it's irrelevant right now bc only dictator and handsoff name someone
        # but they can't actually be removed according to the rules
        if await self.initiate_action(
                gesp.ProgramContext.from_message(message),
                gesp.ActionRemove(maskid, member.id)):
            await self.mark_success(message, True)


    async def cmd_mask_add(self, message, maskid, invite):
        authid = message.author.id
        guild = (invite and invite.guild) or message.guild
        if not guild.get_member(authid):
            raise UserError('You are not a member of that server.')
        if guild.id in self.mask_presence[maskid]:
            raise UserError('That mask is already in that guild.')
        if await self.initiate_action(
                gesp.ProgramContext.from_message(message),
                gesp.ActionServer(maskid, guild.id)):
            await self.mark_success(message, True)


    async def cmd_mask_autoadd(self, message, proxy, value):
        author = message.author
        if value:
            for guild in author.mutual_guilds:
                await self.try_auto_add(author.id, guild.id, proxy['maskid'])


    async def cmd_mask_avatar_attachment(self, message, maskid, attach):
        self.log('%i: %i changing %s avatar to %s (%ib, %ix%i)',
                message.id, message.author.id, maskid, attach.url,
                attach.size, attach.width, attach.height)
        if await self.initiate_action(
                gesp.ProgramContext.from_message(message),
                gesp.ActionChange(maskid, 'avatar', attach.url, message.id,
                    attach.content_type.removeprefix('image/'))):
            await self.mark_success(message, True)


    async def cmd_mask_update(self, message, maskid, name, value):
        if await self.initiate_action(
                gesp.ProgramContext.from_message(message),
                gesp.ActionChange(maskid, name,
                    None if value == CLEAR else value)):
            await self.mark_success(message, True)


    async def cmd_mask_rules(self, message, maskid, rules):
        if await self.initiate_action(
                gesp.ProgramContext.from_message(message),
                gesp.ActionRules(maskid,
                    gesp.Rules.table[RuleType[rules]].from_message(message))):
            await self.mark_success(message, True)


    async def cmd_mask_nominate(self, message, mask, member):
        authid = message.author.id
        if authid not in gesp.Rules.from_json(mask['rules']).named:
            raise UserError('You are not named in the rules.')
        self.nominate(mask['maskid'], authid, member.id)
        await self.mark_success(message, True)


    async def cmd_mask_leave(self, message, mask, member):
        authid = message.author.id
        maskid = mask['maskid']
        if mask['members'] == 1:
            # i doubt this can go wrong with await and stuff but
            # make sure this operation is atomic, just in case
            self.execute('delete from masks where (maskid, members) = (?, 1)',
                    (maskid,))
            if self.cur.rowcount == 0:
                self.log('Bad delete for mask %s', maskid)
                raise UserError(
                        '...Sorry, I lost a race condition. Don\'t panic, '
                        'I\'m looking into it. Try again?')
            self.execute('delete from guildmasks where maskid = ?', (maskid,))
            if maskid in self.mask_presence:
                del self.mask_presence[maskid]
            if self.is_hosted_avatar(mask['avatar']):
                os.remove(self.hosted_avatar_local_path(mask['avatar']))
        elif authid in gesp.Rules.from_json(mask['rules']).named:
            if not member:
                raise UserError(
                        'You are named in the rules of this mask and must '
                        'nominate someone to take your place. Please try '
                        'again with `{p}mask (id/name) leave @member`.'.format(
                            p = COMMAND_PREFIX))
            self.nominate(maskid, authid, member.id)
        gesp.ActionRemove(maskid, authid).execute(self)
        await self.mark_success(message, True)


    async def cmd_edit(self, message, content):
        if not content:
            raise UserError('We need a message here!')
        channel = message.channel
        (thread, channel) = ((channel, channel.parent)
                if type(channel) == discord.Thread
                else (discord.utils.MISSING, channel))
        if message.reference:
            proxied = self.fetchone(
                    'select msgid, authid from history where msgid = ?',
                    (message.reference.message_id,))
            if not proxied or proxied['authid'] != message.author.id:
                return await self.mark_success(message, False)
        else:
            proxied = self.fetchone(
                    'select max(msgid) as msgid, authid from history '
                    'where (chanid, threadid, authid) = (?, ?, ?)',
                    (channel.id, thread.id if thread else 0,
                        message.author.id))
            if not proxied['msgid']:
                return await self.mark_success(message, False)
            then = discord.utils.snowflake_time(proxied['msgid'])
            now = discord.utils.utcnow()
            if then <= now and (now - then).seconds > TIMEOUT_EDIT:
                raise UserError('Could not find a recent message to edit.')
        try:
            proxied = await (thread or channel).fetch_message(proxied['msgid'])
        except discord.errors.NotFound:
            return await self.mark_success(message, False)

        hook = await self.get_webhook(channel)
        if not hook or proxied.webhook_id != hook.id:
            return await self.mark_success(message, False)

        try:
            edited = await hook.edit_message(proxied.id,
                    content = self.fix_content(message, content),
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
                    raise UserError(ERROR_PKAPI)
                response = await r.text(encoding = 'UTF-8')
                try:
                    return json.loads(response)
                except json.decoder.JSONDecodeError:
                    raise UserError(ERROR_PKAPI)
        except asyncio.TimeoutError:
            raise UserError('Could not reach PluralKit API.')


    async def cmd_pk_swap(self, message, user, pkhid):
        authid = message.author.id
        async with self.in_progress(message):
            system = await self.pk_api_get('/systems/' + str(authid))
            member = await self.pk_api_get('/members/' + pkhid)
        try:
            if system['id'] != member['system']:
                raise UserError('That member is not in your system.')
            # in the unlikely that PK goes rogue and tries to mess with us
            if len(member['uuid']) == 5:
                raise UserError(ERROR_PKAPI)
            vote = gesp.VotePkswap(user = user.id, name = member['name'],
                    uuid = member['uuid'],
                    receipt = '%s\'s %s' % (user.name, member['name']),
                    context = gesp.ProgramContext.from_message(message))
            if vote.is_redundant(self):
                return
            if user.id == authid:
                vote.execute(self)
                await self.mark_success(message, True)
            else:
                await self.initiate_vote(vote)
        except KeyError:
            raise UserError(ERROR_PKAPI)


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
                raise UserError('Please reply to a proxied message.')

        async with self.in_progress(message):
            proxied = await self.pk_api_get('/messages/' + str(ref.id))
        try:
            pkuuid = proxied['member']['uuid']
        except KeyError:
            raise UserError(ERROR_PKAPI)

        mask = self.fetchone(
                'select * from proxies where (type, maskid, state) = (?, ?, ?)',
                (ProxyType.pkswap, pkuuid, ProxyState.active))
        if not mask:
            raise UserError('That member has no Gestalt proxies.')

        mask = self.fetchone(
                'select color, updated from guildmasks '
                'where (maskid, guildid) = (?, ?)',
                ('pk-' + pkuuid, message.guild.id))
        if mask and mask['updated'] > ref.id:
            raise UserError('Please use a more recent proxied message.')

        self.execute(
                'insert or replace into guildmasks values '
                '(?, ?, ?, ?, ?, ?, ?, ?)',
                ('pk-' + pkuuid, message.guild.id,
                    ref.author.display_name.removesuffix(MERGE_PADDING),
                    str(ref.author.display_avatar),
                    mask['color'] if mask else None,
                    ProxyType.pkswap, int(time.time()), ref.id))
        try:
            # if pk color is null, keep it None
            if (color := proxied['member']['color']) is not None:
                # color is hex string without '#'
                color = str(discord.Color.from_str('#' + color))
            if not mask or mask['color'] != color:
                # colors aren't set per-server, so set it everywhere
                # (even if the message is older, pk returns the current color)
                self.execute('update guildmasks set color = ? where maskid = ?',
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
            return await self.cmd_help(message, reader.read_word().lower())

        elif arg == 'invite':
            return await self.cmd_invite(message)

        elif arg == 'permcheck':
            guildid = reader.read_word()
            if re.search('[^0-9]', guildid) or not (guildid or message.guild):
                raise UserError('Please provide a valid guild ID.')
            return await self.cmd_permcheck(message, guildid)

        elif arg in ['proxy', 'p']:
            name = reader.read_quote()

            if name in ['', 'list']:
                return await self.cmd_proxy_list(message,
                        reader.read_remainder() == '-all')

            arg = reader.read_word().lower()
            proxy = self.get_user_proxy(message, name)

            if arg == 'tags':
                if proxy['type'] == ProxyType.pkreceipt:
                    raise UserError('You cannot assign tags to that proxy.')
                arg = reader.read_remainder()
                return await self.cmd_proxy_tags(message, proxy, arg)

            elif arg == 'auto':
                # removed command
                return await self.cmd_help(message, 'autoproxy')

            elif arg == 'rename':
                newname = reader.read_remainder()
                if not newname:
                    raise UserError('Please provide a new name.')
                return await self.cmd_proxy_rename(message, proxy['proxid'],
                        newname)

            elif arg in ProxyFlags.__members__.keys():
                if (value := reader.read_bool_int()) is None:
                    raise UserError('Please specify "on" or "off".')
                if arg == 'autoadd':
                    if proxy['type'] != ProxyType.mask:
                        raise UserError('That only applies to Masks.')
                    await self.cmd_mask_autoadd(message, proxy, value)
                    # continue to normal command to actually change the flag
                return await self.cmd_proxy_flag(message, proxy, arg, value)

        elif arg in ['autoproxy', 'ap']:
            if not message.guild:
                raise UserError(ERROR_DM)

            if arg := reader.read_remainder():
                return await self.cmd_autoproxy_set(message, arg)
            return await self.cmd_autoproxy_view(message)

        elif arg in ['account', 'a']:
            arg = reader.read_word().lower()
            if arg == 'config':
                # user must exist due to on_message
                user = self.fetchone(
                        'select * from users where userid = ?',
                        (authid,))
                arg = reader.read_word().lower()
                if len(arg) == 0:
                    return await self.cmd_config_list(message, user)

                if arg in ['default', 'defaults']:
                    return await self.cmd_config_default(message)

                if not arg in Prefs.__members__.keys():
                    raise UserError('That setting does not exist.')

                if (value := reader.read_bool_int()) is None:
                    raise UserError('Please specify "on" or "off".')

                return await self.cmd_config_update(message, user, arg, value)

            elif arg in ['color', 'colour']:
                if not (arg := reader.read_clear() or reader.read_color()):
                    raise UserError('Please enter a color (e.g. `#012345`)')

                return await self.cmd_account_update(message, arg)


        elif arg in ['swap', 's']:
            arg = reader.read_word().lower()
            if arg == 'open':
                if not message.guild:
                    raise UserError(ERROR_DM)

                if (member := reader.read_member()) is None:
                    raise UserError('User not found.')
                tags = reader.read_remainder() or None

                if member.id == self.user.id:
                    raise UserError(ERROR_BLURSED)
                if member.bot:
                    raise UserError(ERROR_CURSED)

                return await self.cmd_swap_open(message, member, tags)

            elif arg in ['close', 'off']:
                name = reader.read_quote()
                proxy = self.get_user_proxy(message, name)
                if proxy['type'] != ProxyType.swap:
                    raise UserError('You do not have a swap with that ID.')

                return await self.cmd_swap_close(message, proxy)

        elif arg in ['mask', 'm']:
            arg = reader.read_quote()

            if arg.lower() == 'new':
                if not (name := reader.read_remainder()):
                    raise UserError('Please provide a name.')

                return await self.cmd_mask_new(message, name)

            else: # arg is mask ID/name
                maskid = arg
                action = reader.read_word().lower()

                # TODO clean this up
                try:
                    # if get_user_proxy succeeds, ['maskid'] must exist
                    maskid = self.get_user_proxy(message, maskid)['maskid']
                except UserError:
                    pass # could save error, but would be confusing
                row = self.fetchone('select * from masks where maskid = ?',
                        (maskid,))
                if not row:
                    raise UserError('Mask not found.')
                maskid = maskid.lower() # TODO rules cache requires this...

                if action == '':
                    return await self.cmd_mask_view(message, row)

                if action == 'join':
                    if self.is_member_of(maskid, authid):
                        raise UserError('You are already a member.')
                    return await self.cmd_mask_join(message, maskid)

                if action == 'invite':
                    if not message.guild:
                        raise UserError(ERROR_DM)
                    if not self.is_member_of(maskid, authid):
                        raise UserError('Only members of the mask can do that.')
                    if not (member := reader.read_member()):
                        raise UserError('Please @mention someone.')
                    if self.is_member_of(maskid, member.id):
                        raise UserError('That user is already a member.')
                    # this bit again
                    if member.id == self.user.id:
                        raise UserError(ERROR_BLURSED)
                    if member.bot:
                        raise UserError(ERROR_CURSED)
                    return await self.cmd_mask_invite(message, maskid, member)

                if action == 'remove':
                    if not message.guild:
                        raise UserError(ERROR_DM)
                    if not self.is_member_of(maskid, authid):
                        raise UserError('Only members of the mask can do that.')
                    if not (member := reader.read_member()):
                        raise UserError('Please @mention someone.')
                    if not self.is_member_of(maskid, member.id):
                        raise UserError('That user is not a member.')
                    return await self.cmd_mask_remove(message, maskid, member)

                if action == 'add':
                    if not self.is_member_of(maskid, authid):
                        raise UserError('Only members of the mask can do that.')
                    invite = None
                    if code := reader.read_word():
                        # TODO better invite in harness for better tests
                        try:
                            invite = await self.fetch_invite(code,
                                    with_counts = False,
                                    with_expiration = False)
                        except:
                            raise UserError('That invite is invalid.')
                        if isinstance(invite.guild, discord.PartialInviteGuild):
                            raise UserError('I am not a member of that server.')
                    elif not message.guild:
                        raise UserError('Please provide an invite.')
                    return await self.cmd_mask_add(message, maskid, invite)

                if newaction := gesp.ActionChange.valid(action):
                    if not self.is_member_of(maskid, authid):
                        raise UserError('Only members of the mask can do that.')

                    if newaction == 'nick':
                        if not (arg := reader.read_remainder()):
                            raise UserError('Please provide a new name.')
                    if newaction == 'avatar':
                        arg = reader.read_clear() or reader.read_link()
                        if AVATAR_URL_BASE and str(arg).startswith(
                                AVATAR_URL_BASE):
                            raise UserError(ERROR_CURSED)
                        if CDN_REGEX.fullmatch(str(arg)):
                            raise UserError('Please reupload the attachment.')
                        if AVATAR_URL_BASE and not arg and message.attachments:
                            arg = message.attachments[0]
                            if arg.content_type not in VALID_MIME_TYPES:
                                raise UserError(
                                        'That attachment is not a valid image.')
                            if arg.size > AVATAR_MAX_SIZE_MB * 1024 * 1024:
                                raise UserError(
                                        'That attachment is too large '
                                        '(max %iMB)' % AVATAR_MAX_SIZE_MB)
                            return await self.cmd_mask_avatar_attachment(
                                    message, maskid, arg)
                        if not arg:
                            raise UserError(
                                    'Please provide a valid URL or attachment.')
                    if newaction == 'color':
                        if not (arg := reader.read_clear() or
                                reader.read_color()):
                            raise UserError(
                                    'Please enter a color (e.g. `#012345`)')

                    return await self.cmd_mask_update(message, maskid,
                            newaction, arg)

                if action == 'rules':
                    if not self.is_member_of(maskid, authid):
                        raise UserError('Only members of the mask can do that.')
                    rules = reader.read_remainder()
                    if rules not in RuleType.__members__.keys():
                        raise UserError('Unknown rule type.')
                    return await self.cmd_mask_rules(message, maskid, rules)

                if action == 'nominate':
                    if not message.guild:
                        raise UserError(ERROR_DM)
                    if not self.is_member_of(maskid, authid):
                        raise UserError('Only members of the mask can do that.')
                    if not (member := reader.read_member()):
                        raise UserError('You need to nominate someone!')
                    if not self.is_member_of(maskid, member.id):
                        raise UserError('That user is not a member.')
                    if member.id == authid:
                        raise UserError(ERROR_CURSED)
                    return await self.cmd_mask_nominate(message, row, member)

                if action == 'leave':
                    if not self.is_member_of(maskid, authid):
                        raise UserError('Only members of the mask can do that?')
                    if member := reader.read_member():
                        if not self.is_member_of(maskid, member.id):
                            raise UserError('That user is not a member.')
                        if member.id == authid:
                            raise UserError(ERROR_CURSED)
                    return await self.cmd_mask_leave(message, row, member)

        elif arg in ['edit', 'e']:
            return await self.cmd_edit(message, reader.cmd) # no parsing

        elif arg in ['become', 'bc']:
            proxy = self.get_user_proxy(message, reader.read_quote())
            if (proxy['type'] == ProxyType.override):
                raise UserError('You are already yourself!')
            if proxy['state'] != ProxyState.active:
                raise UserError('That proxy is not active.')

            return await self.cmd_become(message, proxy)

        elif arg in ['pluralkit', 'pk']:

            arg = reader.read_word()
            if arg == 'swap':
                if (member := reader.read_member()) is None:
                    raise UserError('User not found.')
                pkid = reader.read_word()

                return await self.cmd_pk_swap(message, member, pkid)

            elif arg == 'close':
                swap = self.get_user_proxy(message, reader.read_quote())
                if swap['type'] not in (ProxyType.pkreceipt, ProxyType.pkswap):
                    raise UserError('Please provide a swap receipt.')

                return await self.cmd_pk_close(message, swap)

            elif arg == 'sync':
                if not message.guild:
                    raise UserError(ERROR_DM)
                if not message.reference:
                    raise UserError('Please reply to a proxied message.')

                return await self.cmd_pk_sync(message)

        elif arg == 'log':
            if not message.guild:
                raise UserError(ERROR_DM)
            if not message.author.guild_permissions.administrator:
                raise UserError(
                        'You need `Manage Server` permissions to do that.')

            arg = reader.read_word().lower()
            if arg == 'channel':
                channel = reader.read_channel()
                if not channel:
                    raise UserError('Please mention a channel.')

                return await self.cmd_log_channel(message, channel)

            if arg == 'disable':
                return await self.cmd_log_disable(message)

        elif arg == 'channel':
            if not message.guild:
                raise UserError(ERROR_DM)
            if not message.author.guild_permissions.manage_channels:
                raise UserError(
                        'You need `Manage Channels` permissions to do that.')

            channel = reader.read_channel()
            if not channel:
                raise UserError('Please mention a channel.')

            arg = reader.read_word()
            if arg == 'mode':
                mode = reader.read_word()
                if mode not in ChannelMode.__members__.keys():
                    raise UserError('Invalid channel mode.')

                return await self.cmd_channel_mode(message, channel, mode)

        elif arg == 'explain':
            return await self.reply(message, plain = EXPLAIN)

        elif arg == 'motd':
            if message.author.id == self.owner:
                self.execute('update meta set motd = ?',
                        (reader.read_remainder(),))
                await self.update_status()
                await self.mark_success(message, True)

        elif arg == 'eval':
            program = reader.read_remainder()
            try:
                result = gesp.eval(program)
            except Exception as e:
                raise UserError(e.args[0])
            await self.program_finished(message.channel, result)

