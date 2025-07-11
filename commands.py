from functools import cache
import json
import time
import asyncio
from datetime import datetime
import os
import re

import aiohttp
import discord

from defs import *
import gesp


def escape(text):
    return discord.utils.escape_markdown(discord.utils.escape_mentions(str(text)))


# [text] -> ['[',']']
def parse_tags(tags):
    split = tags.lower().split("text")
    if len(split) != 2 or not "".join(split):
        raise UserError("Please provide valid tags around `text` (e.g. `[text]`).")
    return split


def unparse_tags(prefix, postfix):
    return "`%s`" % (
        f"`{prefix}text{postfix}`".replace("``", "`\N{ZWNBSP}`").replace(
            "``", "`\N{ZWNBSP}`"
        )
    )


class CommandReader:
    BOOL_KEYWORDS = {
        "on": 1,
        "off": 0,
        "yes": 1,
        "no": 0,
        "true": 1,
        "false": 0,
        "0": 0,
        "1": 1,
    }

    def from_message(message):
        reader = CommandReader(message)
        if reader.read_token(COMMAND_PREFIX) or not message.guild:
            return reader

    def __init__(self, message, command=None):
        self.msg = message
        self.cmd = command or message.content

    def is_empty(self):
        return self.cmd == ""

    @staticmethod
    @cache
    def get_token_regex(token):
        return re.compile(re.escape(token) + r"\s*(.*)", re.DOTALL | re.IGNORECASE)

    def read_token(self, token):
        if match := self.get_token_regex(token).fullmatch(self.cmd):
            self.cmd = match[1]
        return bool(match)

    def read_clear(self):
        if self.read_token("-clear"):
            return CLEAR

    def read_word(self):
        # add empty strings to pad array if string empty or no split
        split = self.cmd.split(maxsplit=1) + ["", ""]
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
        (ret, self.cmd) = (self.cmd, "")
        return ret

    # discord.ext includes a MemberConverter
    # but that's only available whem using discord.ext Command
    def read_member(self):
        if self.msg.mentions:
            # consume the text of the mention
            _ = self.read_word()
            return self.msg.mentions[0]

    def read_channel(self):
        _ = self.read_word()  # discard
        if self.msg.channel_mentions:
            chan = self.msg.channel_mentions[0]
            if chan.guild == self.msg.guild:
                return chan

    def read_link(self):
        if m := LINK_REGEX.match(self.cmd):
            self.cmd = self.cmd.removeprefix(m[0]).strip()
            return m[1]  # excluding <...> if present

    def read_message(self, bot):
        if ref := self.msg.reference:
            if ref.cached_message:
                return ref.cached_message
            msgid, chanid = ref.message_id, ref.channel_id
            cmd = self.cmd
        elif m := MESSAGE_LINK_REGEX.match(self.cmd):
            msgid, chanid = int(m[2]), int(m[1])
            cmd = self.cmd.removeprefix(m[0]).strip()
        else:
            return None
        if chan := bot.get_channel(chanid):
            self.cmd = cmd
            return discord.PartialMessage(channel=chan, id=msgid)
        return None

    def read_color(self):
        name = self.read_word()
        try:
            return str(discord.Color.from_str(NAMED_COLORS.get(name.lower(), name)))
        except ValueError:
            pass


class GestaltCommands:
    def get_user_proxy(self, message, name):
        if name == "":
            raise UserError("Please provide a proxy name/ID.")

        # can't do 'and ? in (proxid, cmdname)'; breaks case insensitivity
        proxies = self.fetchall(
            "select proxies.*, guildmasks.guildid from proxies "
            "left join guildmasks on ("
            "(guildmasks.guildid, guildmasks.maskid) = (?, proxies.maskid)"
            ") where userid = ? and (proxid = ? or cmdname = ?) and state != ?",
            (
                message.guild.id if message.guild else 0,
                message.author.id,
                name,
                name,
                ProxyState.hidden,
            ),
        )

        if not proxies:
            raise UserError("You have no proxy with that name/ID.")
        if len(proxies) > 1:
            raise UserError("You have multiple proxies with that name/ID.")

        return proxies[0]

    async def cmd_help(self, message, topic):
        await self.reply(message, HELPMSGS.get(topic, HELPMSGS[""]))

    async def cmd_invite(self, message):
        if (await self.application_info()).bot_public:
            await self.reply(
                message, discord.utils.oauth_url(self.user.id, permissions=PERMS)
            )

    async def cmd_permcheck(self, message, guildid):
        guildid = message.guild.id if guildid == "" else int(guildid)
        guild = self.get_guild(guildid)
        if guild == None or guild.get_member(message.author.id) == None:
            raise UserError("That guild is unknown or you are not a member.")

        memberauth = guild.get_member(message.author.id)
        lines = []
        for chan in guild.channels:
            if chan.type not in ALLOWED_CHANNELS:
                continue
            if not chan.permissions_for(memberauth).view_channel:
                continue

            errors = []
            for p in PERMS:  # p = ('name', bool)
                if p[1] and not p in list(chan.permissions_for(guild.me)):
                    errors += [p[0]]

            # lack of access implies lack of other perms, so leave them out
            if "read_messages" in errors:
                errors = ["read_messages"]
            errors = REACT_CONFIRM if errors == [] else ", ".join(errors)
            lines.append(f"{chan.mention}: {errors}")

        await self.reply_lines(
            message,
            discord.Embed(title=f"Permission check for {escape(guild.name)}"),
            lines,
        )

    def proxy_string(self, proxy):
        line = "%s[`%s`] " % (ProxySymbol[proxy["type"]], proxy["proxid"])
        if proxy["cmdname"]:
            line += "**%s**" % escape(proxy["cmdname"])
        else:
            line += "*no name*"

        parens = ""
        if proxy["type"] == ProxyType.swap:
            user = self.get_user(proxy["otherid"])
            if not user:
                return
            parens = "with **%s**" % escape(user)
        elif proxy["type"] == ProxyType.pkswap:
            # we don't have pkhids
            # parens = 'PluralKit member **%s**' % proxy['maskid']
            parens = ""
        elif proxy["type"] == ProxyType.mask:
            parens = "**%s** on [`%s`]" % (
                escape(proxy["nick"]),
                proxy["maskid"].upper(),
            )

        if proxy["prefix"] is not None:
            parens += " " + unparse_tags(proxy["prefix"], proxy["postfix"])
        if proxy["state"] == ProxyState.inactive:
            parens += " *(inactive)*"

        if parens and proxy["type"] != ProxyType.pkreceipt:
            line += " (%s)" % parens.strip()
        return line

    async def cmd_proxy_list(self, message, all_):
        guild = message.guild
        all_ |= bool(not guild)
        rows = sorted(
            self.fetchall(
                "select proxies.*, guildmasks.guildid, masks.nick from proxies "
                "left join guildmasks on ("
                "(guildmasks.guildid, guildmasks.maskid) = (?, proxies.maskid)"
                ") left join masks using (maskid) "
                "where userid = ?",
                (guild.id if guild else 0, message.author.id),
            ),
            key=lambda row: (
                # randomize so it's not just in order of account creation
                1000 + abs(hash(str(row["otherid"])))
                if row["type"]
                in (ProxyType.swap, ProxyType.pkswap, ProxyType.pkreceipt)
                else row["type"]
            ),
        )

        lines = []
        # must be at least one: the override
        for proxy in rows:
            if (all_ or self.proxy_visible_in(proxy, guild)) and (
                line := self.proxy_string(proxy)
            ):
                lines.append(line)

        await self.reply_lines(
            message,
            discord.Embed(
                title=f"Proxies of {escape(message.author.display_name)}:"
            ).set_footer(
                text=None if all_ else "Proxies in other servers may have been omitted."
            ),
            lines,
        )

    def get_cards_proxy(self, proxy, recurse=True):
        embed = discord.Embed()
        yield embed

        embed.set_author(
            name=ProxySymbol[proxy["type"]] + (proxy["cmdname"] or "(no name)")
        )
        # embed.add_field(name = 'Owner', value = '<@%i>' % proxy['userid'])

        if proxy["type"] == ProxyType.override:
            friendly = "Override"

        if proxy["type"] == ProxyType.swap:
            friendly = "Swap"
            embed.add_field(name="Partner", value="<@%i>" % proxy["otherid"])
            if proxy["state"] == ProxyState.inactive:
                embed.description = "*(this swap is inactive)*"
            elif recurse and proxy["userid"] != proxy["otherid"]:
                swap = self.fetchone(
                    "select * from proxies "
                    "where (userid, otherid, type) = (?, ?, ?)",
                    (proxy["otherid"], proxy["userid"], ProxyType.swap),
                )
                yield from self.get_cards_proxy(swap, False)

        if proxy["type"] == ProxyType.pkswap:
            friendly = "PluralKit Swap"
            yield self.get_card_pk(proxy["maskid"])

        if proxy["type"] == ProxyType.pkreceipt:
            friendly = "PluralKit Receipt"
            swap = self.fetchone(
                "select * from proxies where proxid = ?", (proxy["maskid"],)
            )
            yield from self.get_cards_proxy(swap)

        if proxy["type"] == ProxyType.mask:
            friendly = "Mask"
            mask = self.fetchone(
                "select * from masks where maskid = ?", (proxy["maskid"],)
            )
            yield self.get_card_mask(mask)

        embed.insert_field_at(0, name="Type", value=friendly)

        # assume that proxies too old for a creation date have incomplete count
        if proxy["msgcount"]:
            embed.add_field(
                name="Message Count",
                value="%i%s"
                % (proxy["msgcount"] or 0, "" if proxy["created"] else "+"),
            )

        if proxy["prefix"] is not None:
            embed.add_field(
                name="Tags", value=unparse_tags(proxy["prefix"], proxy["postfix"])
            )

        embed.set_footer(
            text="Proxy ID: %s%s"
            % (
                proxy["proxid"],
                (
                    (
                        " | Created on %s UTC"
                        % datetime.utcfromtimestamp(int(proxy["created"]))
                    )
                    if proxy["created"]
                    else ""
                ),
            )
        )

    async def get_card_pk(self, pkid):
        member = await self.pk_api_get("/members/" + pkid)
        embed = discord.Embed()

        avatar = member["avatar_url"]
        embed.set_author(
            name=member["name"],
            icon_url=member["webhook_avatar_url"] or avatar,
            url=PK_DASH % member["id"],
        )
        embed.set_thumbnail(url=avatar)
        embed.set_image(url=member["banner"])

        if display := member["display_name"]:
            embed.add_field(name="Display Name", value=display)

        if birthday := member["birthday"]:
            birthday = datetime.strptime(birthday, "%Y-%m-%d")
            embed.add_field(
                name="Birthdate",
                value=birthday.strftime(
                    # pk represents a null year as 0004 for some reason
                    "%b %d"
                    if birthday.year == 4
                    else "%b %d, %Y"
                ),
            )

        if pronouns := member["pronouns"]:
            embed.add_field(name="Pronouns", value=pronouns)

        if count := member["message_count"]:
            embed.add_field(name="Message Count", value=count)

        if tags := member["proxy_tags"]:
            embed.add_field(
                name="Proxy Tags",
                value="\n".join(
                    unparse_tags(tag["prefix"] or "", tag["suffix"] or "")
                    for tag in tags
                ),
            )

        if color := member["color"]:
            embed.add_field(name="Color", value="#" + color)
            # according to pk source, color might sometimes be invalid
            try:
                embed.color = discord.Color.from_str("#" + color)
            except ValueError:
                pass

        if description := member["description"]:
            embed.add_field(name="Description", value=description, inline=False)

        embed.set_footer(
            text="System ID: %s | Member ID: %s%s"
            % (
                member["system"],
                member["id"],
                (
                    (
                        " | Created on %s UTC"
                        %
                        # TODO Falsehoods Programmers Believe About Time
                        member["created"].split(".")[0].replace("T", " ")
                    )
                    if member["created"]
                    else ""
                ),
            )
        )

        return embed

    async def cmd_proxy_view(self, message, proxy):
        embeds = list(self.get_cards_proxy(proxy))
        (now, later) = (
            (embeds[:-1], embeds[-1])
            if asyncio.iscoroutine(embeds[-1])
            else (embeds, [])
        )
        if (reply := await self.reply(message, embeds=now)) and later:
            async with self.in_progress(reply):
                now.append(await later)
                await reply.edit(embeds=now)

    async def cmd_proxy_tags(self, message, proxy, tags):
        (prefix, postfix) = (None, None) if tags is CLEAR else parse_tags(tags)
        if prefix is not None and self.get_tags_conflict(
            message.author.id, (prefix, postfix)
        ) not in ([proxy["proxid"]], []):
            raise UserError(ERROR_TAGS)

        self.execute(
            "update proxies set prefix = ?, postfix = ? where proxid = ?",
            (prefix, postfix, proxy["proxid"]),
        )

        await self.mark_success(message, True)

    async def cmd_proxy_rename(self, message, proxid, newname):
        self.execute(
            "update proxies set cmdname = ? where proxid = ?", (newname, proxid)
        )

        await self.mark_success(message, True)

    async def cmd_proxy_flag(self, message, proxy, name, value):
        bit = int(ProxyFlags[name])
        self.execute(
            "update proxies set flags = ? where proxid = ?",
            ((proxy["flags"] & ~bit) | (bit * value), proxy["proxid"]),
        )

        await self.mark_success(message, True)

    async def cmd_autoproxy_view(self, message):
        ap = self.fetchone(
            "select latch, become, proxies.*, guildmasks.guildid, masks.nick "
            "from members "
            "left join proxies using (proxid) "
            "left join guildmasks using (guildid, maskid) "
            "left join masks using (maskid) "
            "where (members.userid, members.guildid) = (?, ?)",
            (message.author.id, message.guild.id),
        )
        # NOTE: valid == False if proxy has been deleted
        # the lack of joined row from proxies sets fetched proxid to NULL
        if valid := ap and ap["proxid"]:
            if not (valid := self.proxy_usable_in(ap, message.guild)):
                self.set_autoproxy(message.author, None)
        proxy_string = valid and self.proxy_string(ap)

        lines = []
        if ap:
            if ap["latch"]:
                lines.append("Your autoproxy is set to latch in this server.")
                if proxy_string:
                    lines.append("Your current latched proxy is:")
                else:
                    lines.append("However, no proxy is latched.")
            if proxy_string:
                if not ap["latch"]:
                    lines.append("Your autoproxy in this server is set to:")
                lines.append(proxy_string)
                if ap["become"] < 1.0:
                    lines.append(
                        "This proxy is in Become mode (%i%%)." % int(ap["become"] * 100)
                    )
        if not lines:
            lines.append("You have no autoproxy enabled in this server.")
        lines.append(
            "For more information, please see `%shelp autoproxy`." % COMMAND_PREFIX
        )

        await self.reply(message, "\n".join(lines))

    async def cmd_autoproxy_set(self, message, arg, and_proxy=False):
        member = message.author
        if arg in ["off", "latch", "l"]:
            self.set_autoproxy(member, None, latch=-1 * int(arg != "off"))
        else:
            proxy = self.get_user_proxy(message, arg)
            if proxy["type"] == ProxyType.override:
                raise UserError("You can't autoproxy your override.")
            if not self.proxy_usable_in(proxy, message.guild):
                raise UserError("You can't use that proxy in this guild.")
            self.set_autoproxy(member, proxy["proxid"], latch=0)
            if and_proxy:
                return await self.do_proxy(
                    message,
                    "\\> [__Be %s.__](%s)" % (arg, message.channel.jump_url),
                    dict(proxy) | {"become": 1.0},
                    self.fetchone(
                        "select prefs from users where userid = ?", (member.id,)
                    )[0],
                )

        await self.mark_success(message, True)

    async def cmd_config_list(self, message, user):
        # list current settings in 'setting: [on/off]' format
        text = "\n".join(
            [
                "%s: **%s**" % (pref.name, "on" if user["prefs"] & pref else "off")
                for pref in Prefs
            ]
        )
        await self.reply(message, text)

    async def cmd_config_default(self, message):
        self.execute(
            "update users set prefs = ? where userid = ?",
            (DEFAULT_PREFS, message.author.id),
        )
        await self.mark_success(message, True)

    async def cmd_config_update(self, message, user, name, value):
        bit = int(Prefs[name])
        self.execute(
            "update users set prefs = ? where userid = ?",
            ((user["prefs"] & ~bit) | (bit * value), message.author.id),
        )

        await self.mark_success(message, True)

    async def cmd_account_update(self, message, value):
        self.execute(
            "update users set color = ? where userid = ?", (value, message.author.id)
        )
        await self.mark_success(message, True)

    def make_or_activate_swap(self, auth, other, tags):
        (prefix, postfix) = parse_tags(tags) if tags else (None, None)
        if self.fetchone(
            "select state from proxies " "where (userid, otherid, type) = (?, ?, ?)",
            (auth.id, other.id, ProxyType.swap),
        ):
            return False
        # look at proxies from target, not author
        swap = self.fetchone(
            "select proxid, state from proxies "
            "where (userid, otherid, type) = (?, ?, ?)",
            (other.id, auth.id, ProxyType.swap),
        )
        self.mkproxy(
            auth.id,
            ProxyType.swap,
            cmdname=other.name,
            prefix=prefix,
            postfix=postfix,
            otherid=other.id,
            state=(
                ProxyState.active
                if auth.id == other.id or swap
                else ProxyState.inactive
            ),
        )
        if swap:
            # target is initiator. author can activate swap
            self.execute(
                "update proxies set state = ? where proxid = ?",
                (ProxyState.active, swap["proxid"]),
            )

        return True

    async def cmd_swap_open(self, message, member, tags):
        if self.make_or_activate_swap(message.author, member, tags):
            await self.mark_success(message, True)

    async def cmd_swap_close(self, message, proxy):
        self.execute(
            "delete from proxies "
            "where ("
            "(userid, otherid) = (?, ?) or (otherid, userid) = (?, ?)"
            ") and type = ?",
            (proxy["userid"], proxy["otherid"]) * 2 + (ProxyType.swap,),
        )

        await self.mark_success(message, True)

    async def cmd_mask_new(self, message, name):
        await self.initiate_vote(
            gesp.VoteCreate(
                user=message.author.id,
                name=name,
                context=gesp.ProgramContext.from_message(message),
            )
        )

    def get_card_mask(self, mask):
        embed = discord.Embed()
        avatar = self.hosted_avatar_fix(mask["avatar"])
        embed.set_author(name=mask["nick"], icon_url=avatar)
        embed.set_thumbnail(url=avatar)
        embed.add_field(name="Members", value=mask["members"])

        # assume that masks too old for a creation date have incomplete count
        if mask["msgcount"]:
            embed.add_field(
                name="Message Count",
                value="%i%s" % (mask["msgcount"], "" if mask["created"] else "+"),
            )

        embed.add_field(
            name="Rules",
            value=discord.utils.get(
                RuleType, value=json.loads(mask["rules"])["type"]
            ).name,
        )

        if mask["color"]:
            embed.color = discord.Color.from_str(mask["color"])
            embed.add_field(name="Color", value=mask["color"])

        embed.set_footer(
            text="Mask ID: %s%s"
            % (
                mask["maskid"].upper(),
                (
                    (
                        " | Created on %s UTC"
                        % datetime.utcfromtimestamp(int(mask["created"]))
                    )
                    if mask["created"]
                    else ""
                ),
            )
        )

        return embed

    async def cmd_mask_view(self, message, mask):
        await self.reply(message, embeds=[self.get_card_mask(mask)])

    async def cmd_mask_join(self, message, maskid):
        if await self.initiate_action(
            gesp.ProgramContext.from_message(message),
            gesp.ActionJoin(maskid, message.author.id),
        ):
            await self.mark_success(message, True)

    async def cmd_mask_invite(self, message, maskid, member):
        await self.initiate_vote(
            gesp.VotePreinvite(
                mask=maskid,
                user=member.id,
                context=gesp.ProgramContext.from_message(message),
            )
        )

    async def cmd_mask_remove(self, message, maskid, member):
        # TODO: require replacement member when candidate is named
        # it's irrelevant right now bc only dictator and handsoff name someone
        # but they can't actually be removed according to the rules
        if await self.initiate_action(
            gesp.ProgramContext.from_message(message),
            gesp.ActionRemove(maskid, member.id),
        ):
            await self.mark_success(message, True)

    async def cmd_mask_add(self, message, maskid, invite):
        authid = message.author.id
        guild = (invite and invite.guild) or message.guild
        if not guild.get_member(authid):
            raise UserError("You are not a member of that server.")
        if self.is_mask_in(maskid, guild.id):
            raise UserError("That mask is already in that guild.")
        if await self.initiate_action(
            gesp.ProgramContext.from_message(message),
            gesp.ActionServer(maskid, guild.id),
        ):
            await self.mark_success(message, True)

    async def cmd_mask_autoadd(self, message, proxy, value):
        author = message.author
        if value:
            for guild in author.mutual_guilds:
                await self.try_auto_add(author.id, guild.id, proxy["maskid"])

    async def cmd_mask_avatar_attachment(self, message, maskid, attach):
        self.log(
            "%i: %i changing %s avatar to %s (%ib, %ix%i)",
            message.id,
            message.author.id,
            maskid,
            attach.url,
            attach.size,
            attach.width,
            attach.height,
        )
        if await self.initiate_action(
            gesp.ProgramContext.from_message(message),
            gesp.ActionChange(
                maskid,
                "avatar",
                attach.url,
                message.id,
                attach.content_type.removeprefix("image/"),
            ),
        ):
            await self.mark_success(message, True)

    async def cmd_mask_update(self, message, maskid, name, value):
        if await self.initiate_action(
            gesp.ProgramContext.from_message(message),
            gesp.ActionChange(maskid, name, None if value == CLEAR else value),
        ):
            await self.mark_success(message, True)

    async def cmd_mask_rules(self, message, maskid, rules):
        if await self.initiate_action(
            gesp.ProgramContext.from_message(message),
            gesp.ActionRules(
                maskid, gesp.Rules.table[RuleType[rules]].from_message(message)
            ),
        ):
            await self.mark_success(message, True)

    async def cmd_mask_nominate(self, message, mask, member):
        authid = message.author.id
        if authid not in gesp.Rules.from_json(mask["rules"]).named:
            raise UserError("You are not named in the rules.")
        self.nominate(mask["maskid"], authid, member.id)
        await self.mark_success(message, True)

    async def cmd_mask_leave(self, message, maskid, member):
        authid = message.author.id
        # avoid potential race conditions if a user joins at the same time
        mask = self.fetchone("select * from masks where maskid = ?", (maskid,))
        if mask["members"] == 1:
            # triggers will delete mask and guildmasks
            if self.is_hosted_avatar(mask["avatar"]):
                os.remove(self.hosted_avatar_local_path(mask["avatar"]))
        elif authid in gesp.Rules.from_json(mask["rules"]).named:
            if not member:
                raise UserError(
                    "You are named in the rules of this mask and must "
                    "nominate someone to take your place. Please try "
                    "again with `{p}mask (id/name) leave @member`.".format(
                        p=COMMAND_PREFIX
                    )
                )
            self.nominate(maskid, authid, member.id)
        gesp.ActionRemove(maskid, authid).execute(self)
        await self.mark_success(message, True)

    async def cmd_edit(self, message, target, content):
        if not content:
            raise UserError("We need a message here!")
        channel = message.channel
        if target:
            try:
                proxied = self.fetchone(
                    "select authid from history where msgid = ?", (target.id,)
                )
            except OverflowError:  # malformed message link
                raise UserError("That message link is invalid.")
            if not proxied or proxied["authid"] != message.author.id:
                return UserError("You did not proxy that message.")
        else:
            proxied = self.fetchone(
                # redundant chanid != 0 to enable use of index
                "select max(msgid) as msgid, authid from history "
                "where (chanid, authid) = (?, ?) and chanid != 0",
                (channel.id, message.author.id),
            )
            if not proxied["msgid"]:
                raise UserError("Could not find a recent message to edit.")
            then = discord.utils.snowflake_time(proxied["msgid"])
            now = discord.utils.utcnow()
            if then <= now and (now - then).total_seconds() > TIMEOUT_EDIT:
                raise UserError("Could not find a recent message to edit.")
            target = channel.get_partial_message(proxied["msgid"])

        if isinstance(target, discord.PartialMessage):
            try:
                target = await target.fetch()
            except discord.errors.NotFound:
                raise UserError("That message has been deleted.")
        channel = target.channel

        hook = await self.get_webhook(channel)
        if not hook or target.webhook_id != hook.id:
            raise UserError(
                "That message could not be edited because it was proxied with a different webhook."
            )

        try:
            edited = await hook.edit_message(
                target.id,
                # possible channel mismatch
                content=self.fix_content(message.author, channel, content),
                thread=(
                    channel
                    if type(channel) == discord.Thread
                    else discord.utils.MISSING
                ),
                allowed_mentions=discord.AllowedMentions(
                    everyone=channel.permissions_for(message.author).mention_everyone
                ),
            )
        except discord.errors.NotFound:
            await self.confirm_webhook_deletion(hook)
            raise UserError("That message could not be edited.")

        if message.guild:
            await self.try_delete(message)
        else:
            await self.mark_success(message, True)

        await self.make_log_message(edited, message, old=target)

    async def cmd_become(self, message, proxy):
        self.set_autoproxy(message.author, proxy["proxid"], become=0.0)
        await self.mark_success(message, True)

    async def cmd_log_channel(self, message, channel):
        self.execute(
            "insert or replace into guilds values (?, ?)",
            (message.guild.id, channel.id),
        )
        await self.mark_success(message, True)

    async def cmd_log_disable(self, message):
        self.execute("delete from guilds where guildid = ?", (message.guild.id,))
        await self.mark_success(message, True)

    async def cmd_channel_mode(self, message, channel, mode):
        # blacklist = 0, log = 1
        self.execute(
            "insert or ignore into channels values (?, ?, 0, 1, ?)",
            (channel.id, channel.guild.id, ChannelMode.default),
        )
        self.execute(
            "update channels set mode = ? where chanid = ?",
            (ChannelMode[mode], channel.id),
        )
        await self.mark_success(message, True)

    async def pk_api_get(self, url):
        await self.pk_ratelimit.block()
        try:
            async with self.session.get(
                PK_ENDPOINT + url,
                timeout=aiohttp.ClientTimeout(total=5.0),
                headers={"User-Agent": PK_USER_AGENT},
            ) as r:
                if r.status != 200:
                    raise UserError(ERROR_PKAPI)
                response = await r.text(encoding="UTF-8")
                try:
                    return json.loads(response)
                except json.decoder.JSONDecodeError:
                    raise UserError(ERROR_PKAPI)
        except asyncio.TimeoutError:
            raise UserError("Could not reach PluralKit API.")

    async def cmd_pk_swap(self, message, user, pkhid):
        authid = message.author.id
        async with self.in_progress(message):
            system = await self.pk_api_get("/systems/" + str(authid))
            member = await self.pk_api_get("/members/" + pkhid)
        try:
            if system["id"] != member["system"]:
                raise UserError("That member is not in your system.")
            # in the unlikely that PK goes rogue and tries to mess with us
            if len(member["uuid"]) == 5:
                raise UserError(ERROR_PKAPI)
            vote = gesp.VotePkswap(
                user=user.id,
                name=member["name"],
                uuid=member["uuid"],
                receipt="%s's %s" % (user.name, member["name"]),
                context=gesp.ProgramContext.from_message(message),
            )
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
        if proxy["type"] == ProxyType.pkreceipt:
            self.execute(
                "delete from proxies where proxid in (?, ?)",
                (proxy["proxid"], proxy["maskid"]),
            )
        else:  # pkswap
            self.execute(
                "delete from proxies where (proxid = ?) "
                "or (userid, maskid) = (?, ?)",  # uses index; faster
                (proxy["proxid"], proxy["otherid"], proxy["proxid"]),
            )

        await self.mark_success(message, True)

    async def cmd_pk_sync(self, message):
        ref = message.reference
        ref = ref.cached_message or await message.channel.fetch_message(ref.message_id)
        if not (ref and ref.webhook_id):
            raise UserError("Please reply to a proxied message.")

        async with self.in_progress(message):
            proxied = await self.pk_api_get("/messages/" + str(ref.id))
        try:
            pkuuid = proxied["member"]["uuid"]
        except KeyError:
            raise UserError(ERROR_PKAPI)

        mask = self.fetchone(
            "select * from proxies where (type, maskid, state) = (?, ?, ?)",
            (ProxyType.pkswap, pkuuid, ProxyState.active),
        )
        if not mask:
            raise UserError("That member has no Gestalt proxies.")

        mask = self.fetchone(
            "select color, updated from guildmasks " "where (maskid, guildid) = (?, ?)",
            (pkuuid, message.guild.id),
        )
        if mask and mask["updated"] > ref.id:
            raise UserError("Please use a more recent proxied message.")

        self.execute(
            "insert or replace into guildmasks values " "(?, ?, ?, ?, ?, ?, ?, ?)",
            (
                pkuuid,
                message.guild.id,
                ref.author.display_name.removesuffix(MERGE_PADDING),
                str(ref.author.display_avatar),
                mask["color"] if mask else None,
                ProxyType.pkswap,
                int(time.time()),
                ref.id,
            ),
        )
        try:
            # if pk color is null, keep it None
            if (color := proxied["member"]["color"]) is not None:
                # color is hex string without '#'
                color = str(discord.Color.from_str("#" + color))
            if not mask or mask["color"] != color:
                # colors aren't set per-server, so set it everywhere
                # (even if the message is older, pk returns the current color)
                self.execute(
                    "update guildmasks set color = ? where maskid = ?", (color, pkuuid)
                )
        except (KeyError, ValueError, TypeError):
            pass

        await self.mark_success(message, True)

    async def do_pk_edit(self, reader):
        if not (target := reader.read_message(self)):
            return
        message = reader.msg
        chanid = message.channel.id
        self.expected_pk_errors[chanid] = None

        try:
            await self.cmd_edit(message, target, reader.cmd)
        except UserError:
            self.expected_pk_errors.pop(chanid, None)
        else:
            await asyncio.sleep(0.5)  # pk can be slow to respond
            if error := self.expected_pk_errors.pop(chanid, None):
                if error.id > message.id:  # in case something went wrong
                    await self.try_delete(error)

    # parse, convert, and validate arguments, then call the relevant function
    async def do_command(self, reader, user):
        message = reader.msg
        arg = reader.read_word().lower()
        authid = message.author.id

        # info and server management commands are always available
        if arg == "help":
            return await self.cmd_help(message, reader.read_word().lower())

        elif arg == "explain":
            return await self.reply(message, plain=EXPLAIN)

        elif arg == "invite":
            return await self.cmd_invite(message)

        elif arg == "permcheck":
            guildid = reader.read_word()
            if re.search("[^0-9]", guildid) or not (guildid or message.guild):
                raise UserError("Please provide a valid guild ID.")
            return await self.cmd_permcheck(message, guildid)

        elif arg == "log":
            if not message.guild:
                raise UserError(ERROR_DM)
            if not message.author.guild_permissions.administrator:
                raise UserError("You need `Manage Server` permissions to do that.")

            arg = reader.read_word().lower()
            if arg == "channel":
                channel = reader.read_channel()
                if not channel:
                    raise UserError("Please mention a channel.")

                return await self.cmd_log_channel(message, channel)

            if arg == "disable":
                return await self.cmd_log_disable(message)

            return

        elif arg == "channel":
            if not message.guild:
                raise UserError(ERROR_DM)
            if not message.author.guild_permissions.manage_channels:
                raise UserError("You need `Manage Channels` permissions to do that.")

            channel = reader.read_channel()
            if not channel:
                raise UserError("Please mention a channel.")

            arg = reader.read_word()
            if arg == "mode":
                mode = reader.read_word()
                if mode not in ChannelMode.__members__.keys():
                    raise UserError("Invalid channel mode.")

                return await self.cmd_channel_mode(message, channel, mode)

            return

        # ... these are not
        if not user:
            if arg == "consent":
                return await self.initiate_vote(
                    gesp.VoteNewUser(
                        user=authid, context=gesp.ProgramContext.from_message(message)
                    )
                )
            raise UserError("Please use `gs;consent` to begin.")

        elif arg == "consent":
            return await self.reply(message, WARNING)

        elif arg in ["proxy", "p"]:
            name = reader.read_quote()

            if name in ["", "list"]:
                return await self.cmd_proxy_list(message, reader.read_token("-all"))

            arg = reader.read_word().lower()
            proxy = self.get_user_proxy(message, name)

            if arg == "":
                return await self.cmd_proxy_view(message, proxy)

            if arg == "tags":
                if proxy["type"] == ProxyType.pkreceipt:
                    raise UserError("You cannot assign tags to that proxy.")
                arg = reader.read_clear() or reader.read_remainder()
                return await self.cmd_proxy_tags(message, proxy, arg)

            elif arg == "auto":
                # removed command
                return await self.cmd_help(message, "autoproxy")

            elif arg == "rename":
                newname = reader.read_remainder()
                if not newname:
                    raise UserError("Please provide a new name.")
                return await self.cmd_proxy_rename(message, proxy["proxid"], newname)

            elif arg in ProxyFlags.__members__.keys():
                if (value := reader.read_bool_int()) is None:
                    raise UserError('Please specify "on" or "off".')
                if arg == "autoadd":
                    if proxy["type"] != ProxyType.mask:
                        raise UserError("That only applies to Masks.")
                    await self.cmd_mask_autoadd(message, proxy, value)
                    # continue to normal command to actually change the flag
                return await self.cmd_proxy_flag(message, proxy, arg, value)

        elif arg in ["autoproxy", "ap"]:
            if not message.guild:
                raise UserError(ERROR_DM)

            if arg := reader.read_remainder():
                return await self.cmd_autoproxy_set(message, arg)
            return await self.cmd_autoproxy_view(message)

        elif arg in ["account", "a"]:
            arg = reader.read_word().lower()
            if arg == "config":
                arg = reader.read_word().lower()
                if len(arg) == 0:
                    return await self.cmd_config_list(message, user)

                if arg in ["default", "defaults"]:
                    return await self.cmd_config_default(message)

                if not arg in Prefs.__members__.keys():
                    raise UserError("That setting does not exist.")

                if (value := reader.read_bool_int()) is None:
                    raise UserError('Please specify "on" or "off".')

                return await self.cmd_config_update(message, user, arg, value)

            elif arg in ["color", "colour"]:
                if not (arg := reader.read_clear() or reader.read_color()):
                    raise UserError("Please enter a color (e.g. `#012345`)")

                return await self.cmd_account_update(message, arg)

        elif arg in ["swap", "s"]:
            arg = reader.read_word().lower()
            if arg == "open":
                if not message.guild:
                    raise UserError(ERROR_DM)

                if (member := reader.read_member()) is None:
                    raise UserError("User not found.")
                tags = reader.read_remainder() or None

                if member.id == self.user.id:
                    raise UserError(ERROR_BLURSED)
                if not self.can_use_gestalt(member):
                    raise UserError(ERROR_CURSED)

                return await self.cmd_swap_open(message, member, tags)

            elif arg in ["close", "off"]:
                name = reader.read_quote()
                proxy = self.get_user_proxy(message, name)
                if proxy["type"] != ProxyType.swap:
                    raise UserError("You do not have a swap with that ID.")

                return await self.cmd_swap_close(message, proxy)

        elif arg in ["mask", "m"]:
            if reader.read_token("new"):
                if not (name := reader.read_remainder()):
                    raise UserError("Please provide a name.")
                if len(name) > MAX_WEBHOOK_NAME_LENGTH:
                    raise UserError(
                        f"That name is too long ({len(name)}>{MAX_WEBHOOK_NAME_LENGTH})."
                    )

                return await self.cmd_mask_new(message, name)

            else:
                maskid = reader.read_quote()
                action = reader.read_word().lower()

                # TODO clean this up
                try:
                    # if get_user_proxy succeeds, ['maskid'] must exist
                    maskid = self.get_user_proxy(message, maskid)["maskid"]
                except UserError:
                    pass  # could save error, but would be confusing
                row = self.fetchone("select * from masks where maskid = ?", (maskid,))
                if not row:
                    raise UserError("Mask not found.")
                maskid = maskid.lower()  # TODO rules cache requires this...

                if action == "":
                    return await self.cmd_mask_view(message, row)

                if action == "join":
                    if self.is_member_of(maskid, authid):
                        raise UserError("You are already a member.")
                    return await self.cmd_mask_join(message, maskid)

                if action == "invite":
                    if not message.guild:
                        raise UserError(ERROR_DM)
                    if not self.is_member_of(maskid, authid):
                        raise UserError("Only members of the mask can do that.")
                    if not (member := reader.read_member()):
                        raise UserError("Please @mention someone.")
                    if self.is_member_of(maskid, member.id):
                        raise UserError("That user is already a member.")
                    # this bit again
                    if member.id == self.user.id:
                        raise UserError(ERROR_BLURSED)
                    if not self.can_use_gestalt(member):
                        raise UserError(ERROR_CURSED)
                    return await self.cmd_mask_invite(message, maskid, member)

                if action == "remove":
                    if not message.guild:
                        raise UserError(ERROR_DM)
                    if not self.is_member_of(maskid, authid):
                        raise UserError("Only members of the mask can do that.")
                    if not (member := reader.read_member()):
                        raise UserError("Please @mention someone.")
                    if not self.is_member_of(maskid, member.id):
                        raise UserError("That user is not a member.")
                    return await self.cmd_mask_remove(message, maskid, member)

                if action == "add":
                    if not self.is_member_of(maskid, authid):
                        raise UserError("Only members of the mask can do that.")
                    invite = None
                    if code := reader.read_word():
                        # TODO better invite in harness for better tests
                        try:
                            invite = await self.fetch_invite(
                                code, with_counts=False, with_expiration=False
                            )
                        except:
                            raise UserError("That invite is invalid.")
                        if isinstance(invite.guild, discord.PartialInviteGuild):
                            raise UserError("I am not a member of that server.")
                    elif not message.guild:
                        raise UserError("Please provide an invite.")
                    return await self.cmd_mask_add(message, maskid, invite)

                if newaction := gesp.ActionChange.valid(action):
                    if not self.is_member_of(maskid, authid):
                        raise UserError("Only members of the mask can do that.")

                    if newaction == "nick":
                        if not (arg := reader.read_remainder()):
                            raise UserError("Please provide a new name.")
                        if len(arg) > MAX_WEBHOOK_NAME_LENGTH:
                            raise UserError(
                                f"That name is too long ({len(arg)}>{MAX_WEBHOOK_NAME_LENGTH})."
                            )
                    if newaction == "avatar":
                        arg = reader.read_clear() or reader.read_link()
                        if AVATAR_URL_BASE and str(arg).startswith(AVATAR_URL_BASE):
                            raise UserError(ERROR_CURSED)
                        if CDN_REGEX.fullmatch(str(arg)):
                            raise UserError("Please reupload the attachment.")
                        if AVATAR_URL_BASE and not arg and message.attachments:
                            arg = message.attachments[0]
                            if arg.content_type not in VALID_MIME_TYPES:
                                raise UserError("That attachment is not a valid image.")
                            if arg.size > AVATAR_MAX_SIZE_MB * 1024 * 1024:
                                raise UserError(
                                    "That attachment is too large "
                                    "(max %iMB)" % AVATAR_MAX_SIZE_MB
                                )
                            return await self.cmd_mask_avatar_attachment(
                                message, maskid, arg
                            )
                        if not arg:
                            raise UserError("Please provide a valid URL or attachment.")
                    if newaction == "color":
                        if not (arg := reader.read_clear() or reader.read_color()):
                            raise UserError("Please enter a color (e.g. `#012345`)")

                    return await self.cmd_mask_update(message, maskid, newaction, arg)

                if action == "rules":
                    if not self.is_member_of(maskid, authid):
                        raise UserError("Only members of the mask can do that.")
                    rules = reader.read_remainder()
                    if rules not in RuleType.__members__.keys():
                        raise UserError("Unknown rule type.")
                    return await self.cmd_mask_rules(message, maskid, rules)

                if action == "nominate":
                    if not message.guild:
                        raise UserError(ERROR_DM)
                    if not self.is_member_of(maskid, authid):
                        raise UserError("Only members of the mask can do that.")
                    if not (member := reader.read_member()):
                        raise UserError("You need to nominate someone!")
                    if not self.is_member_of(maskid, member.id):
                        raise UserError("That user is not a member.")
                    if member.id == authid:
                        raise UserError(ERROR_CURSED)
                    return await self.cmd_mask_nominate(message, row, member)

                if action == "leave":
                    if not self.is_member_of(maskid, authid):
                        raise UserError("Only members of the mask can do that?")
                    if member := reader.read_member():
                        if not self.is_member_of(maskid, member.id):
                            raise UserError("That user is not a member.")
                        if member.id == authid:
                            raise UserError(ERROR_CURSED)
                    return await self.cmd_mask_leave(message, maskid, member)

        elif arg in ["edit", "e"]:
            return await self.cmd_edit(message, reader.read_message(self), reader.cmd)

        elif arg in ["become", "bc"]:
            proxy = self.get_user_proxy(message, reader.read_quote())
            if proxy["type"] == ProxyType.override:
                raise UserError("You are already yourself!")
            if proxy["state"] != ProxyState.active:
                raise UserError("That proxy is not active.")

            return await self.cmd_become(message, proxy)

        elif arg in ["pluralkit", "pk"]:

            arg = reader.read_word()
            if arg == "swap":
                if (member := reader.read_member()) is None:
                    raise UserError("User not found.")
                pkid = reader.read_word()

                return await self.cmd_pk_swap(message, member, pkid)

            elif arg == "close":
                swap = self.get_user_proxy(message, reader.read_quote())
                if swap["type"] not in (ProxyType.pkreceipt, ProxyType.pkswap):
                    raise UserError("Please provide a swap receipt.")

                return await self.cmd_pk_close(message, swap)

            elif arg == "sync":
                if not message.guild:
                    raise UserError(ERROR_DM)
                if not message.reference:
                    raise UserError("Please reply to a proxied message.")

                return await self.cmd_pk_sync(message)

        elif arg == "motd":
            if message.author.id in self.admins:
                self.execute("update meta set motd = ?", (reader.read_remainder(),))
                await self.update_status()
                await self.mark_success(message, True)
