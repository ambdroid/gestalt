from collections import ChainMap, namedtuple, defaultdict
from functools import reduce, partial
from datetime import timedelta
import dataclasses as dc
import hashlib
import json
import math
import time
import os
import re

import discord

from defs import *

ParseState = namedtuple('ParseState', ['pairs', 'stack', 'pos'])
def get_pairs(code):
    if (count := code.count('(')) != code.count(')'):
        raise ValueError('Non-matching parens')
    try:
        pairs = dict(reduce(
                lambda cur, nxt : (
                    ParseState(ChainMap({cur.stack[0]:cur.pos}, cur.pairs),
                        cur.stack[1], cur.pos + 1)
                    if nxt == ')'
                    else (
                        ParseState(cur.pairs, (cur.pos, cur.stack), cur.pos + 1)
                        if nxt == '('
                        else ParseState(cur.pairs, cur.stack, cur.pos + 1)
                        )
                    ),
                code,
                ParseState({}, (), 0)).pairs)
    except IndexError:
        pairs = {}
    if len(pairs) != count:
        raise ValueError('Non-matching parens')
    return pairs

def parse_args(args, pairs, offset):
    if not args:
        return ()
    if args[0] == '(':
        end = pairs[offset] + 1 - offset
        parsed = parse_paren(args[:end], pairs, offset)
    elif m := re.match('"([^"]+)"', args):
        (parsed, end) = (m[1], len(m[0]))
    elif m := re.match('[0-9]+', args):
        (parsed, end) = (int(m[0]), len(m[0]))
    elif m := re.match('true|false', args):
        (parsed, end) = (m[0] == 'true', len(m[0]))
    else:
        raise ValueError('Syntax error')
    stripped = args[end:].strip()
    return (parsed,) + parse_args(stripped, pairs,
            offset + len(args) - len(stripped))

Exp = namedtuple('Exp', ['op', 'args'])

def parse_paren(paren, pairs, offset):
    if m := re.fullmatch('\(([a-z-]+)(\s*)(.*)\)', paren):
        start = 1 + len(m[1]) + len(m[2])
        return Exp(m[1], parse_args(m[3], pairs, offset + start))

def parse_full(expr):
    return parse_args(expr, get_pairs(expr), 0)

user = object()
typecheck = lambda ret, *defs : lambda args : defs == args and ret
types = {
    'and': typecheck(bool, bool, bool),
    'or': typecheck(bool, bool, bool),
    'not': typecheck(bool, bool),
    'add': typecheck(int, int, int),
    'sub': typecheck(int, int, int),
    'mul': typecheck(int, int, int),
    'div': typecheck(int, int, int),
    'floor': typecheck(int, int), # obviously not strictly true but good enough
    'eq': lambda args : len(args) == 2 and args[0] == args[1] and bool,
    'neq': lambda args : len(args) == 2 and args[0] == args[1] and bool,
    'lt': typecheck(bool, int, int),
    'gt': typecheck(bool, int, int),
    'lte': typecheck(bool, int, int),
    'gte': typecheck(bool, int, int),
    'if': lambda args : (len(args) == 3 and args[0] == bool
        and args[1] == args[2]) and args[1],
    'one': typecheck(int),
    'answer': typecheck(bool),
    'initiator': typecheck(user),
    'candidate': typecheck(user),
    'named': typecheck(user, int),
    'members': typecheck(set),
    'size-of': typecheck(int, set),
    'in': typecheck(bool, user, set),
    'vote-approval': typecheck(bool, int, set),
    }

def check(ast, context = {}):
    if type(ast) == Exp:
        if result := types[ast.op](tuple(map(check, ast.args))):
            return result
        raise TypeError('Type check failed')
    return type(ast)

def comp(ast, index = 0):
    if type(ast) in [int, str, bool]:
        return [ast]
    elif ast.op == 'and':
        return comp(Exp('if', (ast.args[0], ast.args[1], False)), index)
    elif ast.op == 'or':
        return comp(Exp('if', (ast.args[0], True, ast.args[1])), index)
    elif ast.op == 'if':
        a = comp(ast.args[0], index)
        b = comp(ast.args[1], index + len(a) + 2)
        c = comp(ast.args[2], index + len(a) + 2 + len(b) + 2)
        jmp = [index + len(a) + 2 + len(b) + 2 - 1, 'jf']
        jmp2 = [index + len(a) + 2 + len(b) + len(jmp) + len(c) - 1, 'jp']
        return a + jmp + b + jmp2 + c
    elif ast.op == 'vote-approval':
        post = ['vote-approval', 'answer']
    else:
        post = [ast.op]
    return reduce(
            lambda cur, nxt : cur + comp(nxt, index + len(cur)),
            ast.args,
            []
            ) + post

ProgramState = namedtuple('ProgramState', ['program', 'pc', 'stack'])

def run(state, context = None):
    (_, pc, stack) = state
    while pc < len(state.program):
        op = state.program[pc]
        if op == 'jp':
            pc = stack.pop()
        elif op == 'jf':
            dest = stack.pop()
            if not stack.pop():
                pc = dest
        elif op == 'not':
            stack.append(not stack.pop())
        elif op == 'add':
            stack.append(stack.pop() + stack.pop())
        elif op == 'sub':
            stack.append(-(stack.pop() - stack.pop()))
        elif op == 'mul':
            stack.append(stack.pop() * stack.pop())
        elif op == 'div':
            a = stack.pop()
            stack.append(stack.pop() / a)
        elif op == 'floor':
            stack.append(math.floor(stack.pop()))
        elif op == 'eq':
            stack.append(stack.pop() == stack.pop())
        elif op == 'neq':
            stack.append(stack.pop() != stack.pop())
        elif op == 'lt':
            stack.append(stack.pop() > stack.pop())
        elif op == 'gt':
            stack.append(stack.pop() < stack.pop())
        elif op == 'lte':
            stack.append(stack.pop() >= stack.pop())
        elif op == 'gte':
            stack.append(stack.pop() <= stack.pop())
        elif op == 'one':
            stack.append(1)
        elif op == 'answer':
            stack.append(context.answer)
        elif op == 'initiator':
            stack.append(context.initiator)
        elif op == 'candidate':
            stack.append(context.candidate)
        elif op == 'named':
            stack.append(context.named[stack.pop()])
        elif op == 'members':
            stack.append(context.members)
        elif op == 'size-of':
            stack.append(len(stack.pop()))
        elif op == 'in':
            _set = stack.pop()
            stack.append(stack.pop() in _set)
        elif op == 'vote-approval':
            return partial(VoteApproval,
                    eligible = stack.pop(),
                    threshold = stack.pop(),
                    state = ProgramState(state.program, pc + 1, stack),
                    context = context,
                    )
        else:
            stack.append(op)
        pc += 1
    if len(stack) != 1:
        raise RuntimeError('Program finished with invalid stack')
    return stack[0]

def eval(program):
    exp = parse_full(program)[0]
    check(exp)
    return run(ProgramState(comp(exp), 0, []))


# there is probably a better way to do this
# but i can't get __init_subclass__ to work otherwise
def serializable(name, parents, attrs):
    class Inner:
        _type = None
        table = {}
        def get_type(self):
            return self._type
        @classmethod
        def class_dict(cls, _dict):
            return cls(**_dict)
        @classmethod
        def from_dict(cls, _dict):
            return cls.table[_dict['type']].class_dict(_dict['data'])
        @classmethod
        def from_json(cls, js):
            return cls.from_dict(json.loads(js))
        def to_dict(self):
            # dc.asdict() converts child dc's to dicts too, unwanted
            # (that wouldn't include the type)
            return {'type': self._type, 'data': vars(self)}
        def to_json(self):
            return json.dumps(self.to_dict(),
                    default =
                        lambda val : val.to_dict() if dc.is_dataclass(val)
                        else list(val))
    def init(cls, _type = None):
        if _type is not None:
            cls._type = _type.value
            super(cls, cls).table[_type.value] = cls
    return type(name, (Inner,), attrs | {'__init_subclass__' : init})


@dc.dataclass
class VotableAction(metaclass = serializable):
    mask: str
    def add_context(self, context):
        pass
    def execute(self, bot):
        raise NotImplementedError()


@dc.dataclass
class Rules(metaclass = serializable):
    named: list[int] = dc.field(default_factory = list)
    @classmethod
    def from_message(cls, message):
        return cls()
    def for_action(self, atype):
        raise NotImplementedError()
    async def eval(self, bot, context, action):
        return await bot.step_program(
                ProgramState(comp(self.for_action(action.get_type())), 0, []),
                context, action)


@dc.dataclass
class ActionJoin(VotableAction, _type = ActionType.join):
    candidate: int
    def add_context(self, context):
        context.candidate = self.candidate
    def execute(self, bot, autoadd = False):
        nick = bot.fetchone('select nick from masks where maskid = ?',
                (self.mask,))
        if nick:
            if bot.is_member_of(self.mask, self.candidate):
                return # TODO errors
            bot.mkproxy(self.candidate, ProxyType.mask, cmdname = nick[0],
                    maskid = self.mask,
                    flags = ProxyFlags.autoadd if autoadd else ProxyFlags(0))


class ActionInvite(ActionJoin, _type = ActionType.invite):
    pass


@dc.dataclass
class ActionRemove(VotableAction, _type = ActionType.remove):
    candidate: int
    def add_context(self, context):
        context.candidate = self.candidate
    def execute(self, bot):
        bot.execute('delete from proxies '
                'where (userid, maskid, type) = (?, ?, ?)',
                (self.candidate, self.mask, ProxyType.mask))


@dc.dataclass
class ActionServer(VotableAction, _type = ActionType.server):
    server: int
    def execute(self, bot):
        if bot.fetchone('select 1 from masks where maskid = ?', (self.mask,)):
            if bot.get_guild(self.server) and not bot.is_mask_in(self.mask, self.server):
                bot.execute('insert into guildmasks values'
                        '(?, ?, NULL, NULL, NULL, ?, ?, NULL)',
                        (self.mask, self.server, ProxyType.mask,
                            int(time.time())))


@dc.dataclass
class ActionChange(VotableAction, _type = ActionType.change):
    cols = ('nick', 'avatar', 'color')
    which: str
    value: str
    message: int = 0    # for logging
    ext: str = ''       # if avatar is attachment to be saved
    server: int = 0     # reserved
    def valid(word):
        if ((ret := {'name': 'nick', 'colour': 'color'}.get(word, word))
                in ActionChange.cols):
            return ret
    def __post_init__(self):
        if self.which not in self.cols:
            raise ValueError(self.which)
    def save_avatar(self, bot, image):
        # hashlib and i/o release GIL
        digest = hashlib.sha1(image).hexdigest()
        name = '%s_%s.%s' % (self.mask, digest, self.ext)
        with open(bot.hosted_avatar_local_path(name), 'wb') as f:
            f.write(image)
        return name
    async def execute(self, bot):
        if self.which == 'avatar':
            if not (prev := bot.fetchone(
                    'select avatar from masks where maskid = ?',
                    (self.mask,))):
                return
            prev = prev[0]
            if self.ext:
                try:
                    async with bot.session.get(self.value) as r:
                        if r.status != 200:
                            return
                        image = await r.read()
                except:
                    return
                self.value = await bot.loop.run_in_executor(bot.threads,
                        self.save_avatar, bot, image)
                bot.log('%i: saved %s', self.message, self.value)
            if self.value != prev and bot.is_hosted_avatar(prev):
                os.remove(bot.hosted_avatar_local_path(prev))
        bot.execute({
                'nick': 'update masks set nick = ? where maskid = ?',
                'avatar': 'update masks set avatar = ? where maskid = ?',
                'color': 'update masks set color = ? where maskid = ?',
                }[self.which],
                (self.value, self.mask))


@dc.dataclass
class ActionRules(VotableAction, _type = ActionType.rules):
    newrules: Rules
    @classmethod
    def class_dict(cls, _dict):
        return super().class_dict(_dict | {
            'newrules': Rules.from_dict(_dict['newrules'])})
    def execute(self, bot):
        for user in self.newrules.named:
            if not bot.is_member_of(self.mask, user):
                return # TODO errors
        bot.execute('update masks set rules = ? where maskid = ?',
                (self.newrules.to_json(), self.mask))


@dc.dataclass
class RulesLegacy(Rules, _type = RuleType.legacy):
    role: int = 0
    guild: int = 0
    @classmethod
    def from_message(cls, message):
        raise UserError('You cannot set a mask to legacy rules.')
    async def eval(self, bot, context, action):
        """
        join    = manage_roles
        invite  = manage_roles
        remove  = manage_roles
        server  = never
        change  = always
        rules   = manage_roles
        """
        if not (guild := bot.get_guild(self.guild)):
            return
        member = guild.get_member(context.initiator)
        if not member or type(action) == ActionServer:
            return
        elif (type(action) == ActionChange
                or member.guild_permissions.manage_roles):
            await discord.utils.maybe_coroutine(action.execute, bot)
            return True
        raise UserError(
                'Because this is a legacy Mask, you need `Manage Roles` '
                'permissions to do that. '
                + (('(The <@&%i> role itself is no longer necessary.)'
                    % self.role)
                    if self.role != self.guild else ''))


@dc.dataclass
class RulesDictator(Rules, _type = RuleType.dictator):
    rule = parse_full(
            '(eq'
                '(initiator)'
                '(named 0)'
            ')'
            )[0]
    user: dc.InitVar[int] = None
    def __post_init__(self, user = None):
        if user:
            self.named = [user]
    @classmethod
    def from_message(cls, message):
        return cls(user = message.author.id)
    def for_action(self, atype):
        return self.rule


class RulesHandsOff(RulesDictator, _type = RuleType.handsoff):
    rule_voting = parse_full(
            '(vote-approval'
                '(add'
                    '(floor'
                        '(div'
                            '(sub'
                                '(size-of'
                                    '(members)'
                                ')'
                            '1)'
                        '2)'
                    ')'
                '1)'
                '(members)'
            ')'
            )[0]
    rule_immune = parse_full(
            '(neq'
                '(candidate)'
                '(named 0)'
            ')')[0]
    def for_action(self, atype):
        if atype == ActionType.rules:
            return self.rule
        return Exp('or', (self.rule,
            Exp('and', (self.rule_immune, self.rule_voting))
            if atype == ActionType.remove
            else self.rule_voting))


rule_solo = parse_full(
    '(and'
        '(eq'
            '(size-of'
                '(members)'
            ')'
        '1)'
        '(in'
            '(initiator)'
            '(members)'
        ')'
    ')'
    )[0]


class RulesMajority(Rules, _type = RuleType.majority):
    rule = parse_full(
            '(vote-approval'
                '(add'
                    '(floor'
                        '(div'
                            '(size-of'
                                '(members)'
                            ')'
                        '2)'
                    ')'
                '1)'
                '(members)'
            ')'
            )[0]
    def for_action(self, atype):
        return Exp('or', (rule_solo, self.rule))


class RulesUnanimous(Rules, _type = RuleType.unanimous):
    rule = parse_full(
            '(vote-approval'
                '(size-of'
                    '(members)'
                ')'
                '(members)'
            ')'
            )[0]
    rule_remove = parse_full(
            '(vote-approval'
                '(sub'
                    '(size-of'
                        '(members)'
                    ')'
                '1)'
                '(members)'
            ')'
            )[0]
    def for_action(self, atype):
        return Exp('or', (rule_solo,
                self.rule_remove if atype == ActionType.remove else self.rule))


@dc.dataclass
class ProgramContext:
    initiator: int
    message: int = None
    channel: int = None
    guild: int = None
    named: list[int] = None
    members: frozenset[int] = None
    candidate: int = None
    answer: bool = None
    yes: frozenset[int] = None
    no: frozenset[int] = None
    def from_message(msg):
        return ProgramContext(
                initiator = msg.author.id,
                message = msg.id,
                channel = msg.channel.id,
                guild = (msg.guild and msg.guild.id) or 0
                )
    def from_dict(_dict):
        return ProgramContext(**(_dict | ({
            'members': frozenset(_dict['members']),
            } if _dict['members'] else {}) | ({
                'yes': frozenset(_dict['yes']),
                'no': frozenset(_dict['no']),
                } if _dict['yes'] else {})
            ))
    def to_dict(self):
        return vars(self)


@dc.dataclass
class Vote(metaclass = serializable):
    # why is ProgramContext stored in the Vote, you might ask
    # (and by you i mean me, because i kept confusing myself about this)
    # well, the context is passed around in function calls
    # because it doesn't cleanly fit in VotableAction or ProgramState
    # but when it's time for a Vote, the context needs to be at rest
    context: ProgramContext
    eligible: frozenset[int] = frozenset()
    threshold: int = 0
    yes: set[int] = dc.field(default_factory = set)
    no: set[int] = dc.field(default_factory = set)
    def on_interaction(self, interaction, bot):
        if (userid := interaction.user.id) in self.eligible:
            button = interaction.data['custom_id']
            if button == 'abstain':
                if userid in self.yes:
                    self.yes.remove(userid)
                    desc = 'You removed your yes vote.'
                elif userid in self.no:
                    self.no.remove(userid)
                    desc = 'You removed your no vote.'
                else:
                    desc = 'You were already abstaining.'
            else:
                (dest, src) = ((self.yes, self.no) if button == 'yes'
                        else (self.no, self.yes))
                if userid in dest:
                    desc = 'You were already voting %s.' % button
                else:
                    src.discard(userid)
                    dest.add(userid)
                    desc = 'You chose %s.' % button
        else:
            desc = 'You are not eligible for this vote.'
        return self.after_interaction(interaction, desc, bot) # return coro
    async def after_interaction(self, interaction, desc, bot):
        await respond(interaction, desc)
        embed = discord.Embed(description = self.description())
        embed = (embed if interaction.message.embeds[0] != embed
                else discord.utils.MISSING)
        view = self.view(True) if self.inactive else discord.utils.MISSING
        if embed or view:
            await interaction.message.edit(embed = embed, view = view)
    # has vote received the maximum amount of interaction
    @property
    def complete(self):
        raise NotImplementedError()
    # is vote no longer accepting interactions due to being complete OR expired
    @property
    def inactive(self):
        return self.complete or (discord.utils.utcnow()
                - discord.utils.snowflake_time(self.context.message)
                # limited by avatar changes; cdn link expiry w/some padding
                > timedelta(days = 1, seconds = -10))
    async def on_done(self, bot):
        raise NotImplementedError()
    def description(self):
        raise NotImplementedError()
    def view(self, disabled = False):
        raise NotImplementedError()
    @classmethod
    def class_dict(cls, _dict):
        return super().class_dict(_dict | {
            'context': ProgramContext.from_dict(_dict['context']),
            'eligible': frozenset(_dict['eligible']),
            'yes': set(_dict['yes']),
            'no': set(_dict['no']),
            })


@dc.dataclass
class VoteConfirm(Vote):
    user: dc.InitVar[int] = None
    threshold: int = 1
    def __post_init__(self, user = None):
        if user:
            self.eligible = frozenset([user])
        if not self.eligible:
            raise ValueError('No user')
        if self.threshold != 1:
            raise ValueError('Threshold must be 1')
    def get_user(self):
        return next(iter(self.eligible)) # only one
    @property
    def complete(self):
        return bool(self.yes or self.no)
    async def on_done(self, bot):
        self.context.answer = bool(self.yes)
    def view(self, disabled = False):
        if disabled:
            return None
        view = discord.ui.View()
        view.add_item(discord.ui.Button(
            custom_id = 'yes',
            style = discord.ButtonStyle.green,
            label = 'Yes',
            disabled = disabled,
            ))
        view.add_item(discord.ui.Button(
            custom_id = 'no',
            style = discord.ButtonStyle.red,
            label = 'No',
            disabled = disabled,
            ))
        return view


@dc.dataclass
class VoteCreate(VoteConfirm, _type = VoteType.create):
    name: str = None
    async def on_done(self, bot):
        bot.execute('insert into masks values '
                '(?, ?, NULL, NULL, NULL, ?, 0, 0)',
                ((maskid := bot.gen_id()), self.name, time.time()))
        user = self.get_user()
        autoadd = bool(self.yes)
        ActionJoin(maskid, user).execute(bot, autoadd = autoadd)
        # this has to be after join or an is_member_of() check fails
        ActionRules(maskid, RulesDictator(user = user)).execute(bot)
        if autoadd:
            for guild in bot.get_user(user).mutual_guilds:
                await bot.try_auto_add(user, guild.id, maskid)
        elif self.context.guild:
            ActionServer(maskid, self.context.guild).execute(bot)
    def description(self):
        if self.complete:
            return 'Mask **%s** created!' % discord.utils.escape_markdown(
                    self.name)
        else:
            return ('Do you want to automatically try to add this mask to all '
                    'your current and future guilds? Other members can enable '
                    'this for themselves with `{p}p (name/id) autoadd on`.'
                    ).format(p = COMMAND_PREFIX)


@dc.dataclass
class VotePreinvite(VoteConfirm, _type = VoteType.preinvite):
    mask: str = None
    def __post_init__(self, user = None):
        if not self.mask:
            raise ValueError('Mask is required')
        super().__post_init__(user = user)
    async def on_done(self, bot):
        if self.yes:
            await bot.initiate_action(self.context,
                    ActionInvite(self.mask, self.get_user()))
    def description(self):
        if self.yes:
            desc = '<@%i> accepted the invitation to the mask.'
        elif self.no:
            desc = '<@%i> declined to join the mask.'
        else:
            desc = '<@%i>, do you want to join this mask?'
        return desc % self.get_user()


@dc.dataclass
class VoteProgram(Vote):
    action: VotableAction = None
    state: ProgramState = None
    def __post_init__(self):
        # state and action need a default because they follow other defaults
        # (but it's not actually default)
        if not (self.state and self.action):
            raise ValueError()
    async def on_done(self, bot):
        await bot.step_program(self.state, self.context, self.action)
    @classmethod
    def class_dict(cls, _dict):
        return super().class_dict(_dict | {
            'state': ProgramState(*_dict['state']),
            'action': VotableAction.from_dict(_dict['action'])
            })


class VoteApproval(VoteProgram, _type = VoteType.approval):
    @property
    def complete(self):
        return len(self.yes) >= self.threshold
    async def on_done(self, bot):
        # no other possibility except vote simply expiring
        # however, this might change so that expiry = False
        # therefore still pass it through context for forward compatibility
        self.context.answer = True
        await super().on_done(bot)
    def description(self):
        return '**%i**/**%i** votes needed' % (len(self.yes), self.threshold)
    def view(self, disabled = False):
        view = discord.ui.View()
        view.add_item(discord.ui.Button(
            custom_id = 'yes',
            style = discord.ButtonStyle.green,
            label = 'Yes',
            disabled = disabled,
            ))
        view.add_item(discord.ui.Button(
            custom_id = 'abstain',
            style = discord.ButtonStyle.grey,
            label = 'Abstain',
            disabled = disabled,
            ))
        return view


class VoteConsensus(VoteProgram, _type = VoteType.consensus):
    @property
    def complete(self):
        return len(self.yes) + len(self.no) >= self.threshold
    async def on_done(self, bot):
        self.context.yes = frozenset(self.yes)
        self.context.no = frozenset(self.no)
        await super().on_done(bot)
    def view(self, disabled = False):
        view = discord.ui.View()
        view.add_item(discord.ui.Button(
            custom_id = 'yes',
            style = discord.ButtonStyle.green,
            label = 'Yes',
            disabled = disabled,
            ))
        view.add_item(discord.ui.Button(
            custom_id = 'no',
            style = discord.ButtonStyle.red,
            label = 'No',
            disabled = disabled,
            ))
        view.add_item(discord.ui.Button(
            custom_id = 'abstain',
            style = discord.ButtonStyle.grey,
            label = 'Abstain',
            disabled = disabled,
            ))
        return view


@dc.dataclass
class VotePkswap(VoteConfirm, _type = VoteType.pkswap):
    name: str = None
    uuid: str = None
    receipt: str = None
    def execute(self, bot):
        if not self.is_redundant(bot):
            userid = self.get_user()
            sender = self.context.initiator
            proxid = bot.mkproxy(userid, ProxyType.pkswap, cmdname = self.name,
                    otherid = sender, maskid = self.uuid)
            if userid != sender:
                bot.mkproxy(sender, ProxyType.pkreceipt,
                        cmdname = self.receipt, otherid = userid,
                        maskid = proxid, state = ProxyState.inactive)
    async def on_done(self, bot):
        if self.yes:
            self.execute(bot)
    def description(self):
        if self.yes:
            desc = '<@%i> accepted <@%i>\'s %s.'
        elif self.no:
            desc = '<@%i> declined <@%i>\'s %s.'
        else:
            desc = '<@%i>, do you want <@%i>\'s %s?'
        return desc % (self.get_user(), self.context.initiator, self.name)
    def is_redundant(self, bot):
        # it would be really nice to just check the pkhid in the command
        # that way we could check if the proxy exists as the first step
        # unfortunately, pkhids are NOT guaranteed to be constant!
        # therefore, we're forced to use the pkuuid...
        # NB: a pk system may be attached to multiple accounts
        return bool(bot.fetchone(
            'select 1 from proxies '
            'where (userid, maskid, type, state) = (?, ?, ?, ?)',
            (self.get_user(), self.uuid, ProxyType.pkswap, ProxyState.active)))


async def respond(interaction, content):
    await interaction.response.send_message(
            embed = discord.Embed(description = content),
            ephemeral = True)


class GestaltVoting:
    def load(self):
        self.votes = {row['msgid']: Vote.from_json(row['state'])
                for row in self.fetchall('select * from votes')}


    def save(self):
        self.execute('delete from votes')
        self.cur.executemany('insert into votes values (?, ?)',
                ((msg, vote.to_json()) for msg, vote in self.votes.items()))


    def get_rules(self, maskid):
        if row := self.fetchone(
                'select rules from masks where maskid = ?',
                (maskid,)):
            return Rules.from_json(row[0])


    async def initiate_action(self, context, action):
        if not (rule := self.get_rules(action.mask)):
            return # TODO errors?
        context.named = rule.named
        context.members = frozenset(
                row[0] for row in
                self.fetchall(
                    'select userid from proxies where maskid = ?',
                    (action.mask,))
                )
        action.add_context(context)
        return await rule.eval(self, context, action)


    async def initiate_vote(self, vote):
        chanid = vote.context.channel
        # i don't know why this is a problem with dm channels
        channel = self.get_channel(chanid) or await self.fetch_channel(chanid)
        if not channel.guild:
            # docs say recipient may not always be available
            # yes this means two different chances to call fetch_(). i hate it
            recipient = channel.recipient or (await self.fetch_channel(
                chanid)).recipient
            if vote.eligible != frozenset({recipient.id}):
                return await self.send(channel,
                        'A vote was called for, but it must be run in a guild.')
        if msg := await self.send(channel, vote.description(),
                view = vote.view(),
                reference = channel.get_partial_message(vote.context.message)):
            if channel.guild:
                self.mkhistory(msg, vote.context.initiator)
            self.votes[msg.id] = vote


    def is_mask_in(self, maskid, guildid):
        return bool(self.fetchone(
                'select 1 from guildmasks where (guildid, maskid) = (?, ?)',
                (guildid, maskid)))


    def is_member_of(self, maskid, userid):
        return bool(self.fetchone(
                'select 1 from proxies where (userid, maskid) = (?, ?)',
                (userid, maskid)))


    async def try_auto_add(self, userid, guildid, maskid):
        if not self.is_mask_in(maskid, guildid):
            # there's no chance of anything async actually happening here
            # (the only async outcome is creating a vote, which can't happen)
            # but there's also no point in optimizing that away
            # shrug.
            await self.initiate_action(ProgramContext(initiator = userid),
                    ActionServer(maskid, guildid))


    # this doesn't get its own Action subclass because it's unconditional
    def nominate(self, maskid, nominator, nominee):
        if not self.is_member_of(maskid, nominee):
            return # TODO errors
        rules = self.get_rules(maskid)
        rules.named = [nominee if i == nominator else i for i in rules.named]
        ActionRules(maskid, rules).execute(self)


    async def step_program(self, program, context, action):
        result = run(program, context)
        if isinstance(result, partial):
            if context.channel: # None in case of auto-add (no channel)
                await self.initiate_vote(result(action = action))
        elif result == True:
            await discord.utils.maybe_coroutine(action.execute, self)
            return True


    # the docs discourage using this
    # but it's easier than using the callbacks across reboots
    async def on_interaction(self, interaction):
        vote = self.votes.get(msgid := interaction.message.id)
        if vote is None or vote.inactive:
            return await respond(interaction, 'This has concluded or expired.')
        # NOTE on_interaction is sync, but returns coro from after_interaction
        # this is to avoid race conditions
        coro = vote.on_interaction(interaction, self)
        if vote.complete:
            del self.votes[msgid]
            await vote.on_done(self)
        await coro


    async def on_bot_interaction(self, message):
        # trying to keep this as self-contained as possible
        class BotInteraction:
            def __init__(self, bot, trigger, msg):
                self.bot, self.trigger, self.message = bot, trigger, msg
                self.data = {'custom_id': trigger.content.lower()}
                self.response = self
                self.user = trigger.author
            async def send_message(self, embed, ephemeral):
                await self.bot.reply(self.trigger, embeds = [embed])

        button = message.content.lower()
        if not (votes := [
                msgid for (msgid, vote) in self.votes.items()
                if vote.context.channel == message.channel.id
                and message.author.id in vote.eligible
                and discord.utils.get(vote.view().children, custom_id = button)
                ]):
            return False

        try:
            vmessage = await message.channel.fetch_message(max(votes))
        except discord.errors.NotFound:
            return False

        await self.on_interaction(BotInteraction(self, message, vmessage))
        return True

