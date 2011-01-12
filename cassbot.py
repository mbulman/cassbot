# cassbot

import re
import time
import shlex
from twisted.words.protocols import irc
from twisted.internet import reactor, defer, protocol
from twisted.web import client, error
from twisted.python import log
from twisted.plugin import getPlugins, IPlugin
from twisted.application import internet, service
from zope.interface import Interface, implements
import plugins


class IBotPlugin(Interface):
    def name():
        """
        Return the name of this plugin.
        """

    def interestingMethods():
        """
        Return a list of method names in which this plugin am interested.
        When the corresponding method is called on the bot's CassBotCore
        instance, then it will be called on this plugin as well (with an
        extra parameter, the CassBotCore instance, preceding the others.)

        This will be periodically re-called in order to refresh the plugin
        list.
        """

    def description():
        """
        Return a string describing what this plugin does, or None if there is
        no need for a description.
        """

    def implementedCommands():
        """
        Return a list of command names corresponding to the commands this
        plugin wants to handle. A command name is the first word in a message
        directed at the bot (by private message, or in a channel addressed
        specifically to the bot). Multiple plugins may implement a command
        (and each will run), but it is probably best not to take advantage of
        this, to avoid confusion.

        All the returned names should correspond to methods on this class
        named ('command_' + name). These methods will be called with the
        following parameters:

            (bot, user, channel, args)

        ..where bot is the CassBotCore instance to which this command came,
        user is the user who issued the command, channel is the channel by
        whence it came (this will be the same as user if via private message),
        and args is a list of the words which followed the command.
        """


class BaseBotPlugin(object):
    implements(IPlugin, IBotPlugin)

    @classmethod
    def name(cls):
        """
        Default implementation; just return the name of the class.
        """
        return cls.__name__

    @classmethod
    def description(cls):
        """
        Default implementation; just return the docstring of the class.
        """
        return cls.__doc__

    @classmethod
    def interestingMethods(cls):
        """
        Default implementation; express interest in all overrideable methods
        that match method names on this class.
        """

        for mname in CassBotCore.overrideable:
            try:
                x = getattr(cls, mname)
                if callable(x):
                    yield mname
            except AttributeError:
                pass

    @classmethod
    def implementedCommands(cls):
        """
        Default implementation; offer to handle all commands suggested by
        methods on this class named 'command_*'.
        """

        for name, value in cls.__dict__.iteritems():
            if name.startswith('command_') and callable(value):
                yield name[8:]


def noop(*a, **kw):
    pass

def removekey(dicty, key):
    try:
        del dicty[key]
    except KeyError:
        pass


class CassBotCore(irc.IRCClient):
    overrideable = (
        'created',
        'yourHost',
        'myInfo',
        'luserClient',
        'bounce',
        'isupport',
        'luserChannels',
        'luserOp',
        'luserMe',
        'privmsg',
        'joined',
        'left',
        'noticed',
        'modeChanged',
        'signedOn',
        'kickedFrom',
        'nickChanged',
        'userJoined',
        'userLeft',
        'userQuit',
        'userKicked',
        'action',
        'topicUpdated',
        'userRenamed',
        'receivedMOTD',
        'msg'
    )

    def __init__(self, nickname='cassbot'):
        # state that will be saved and reset on this object by the service
        self.nickname = nickname
        self.join_channels = ()
        self.cmd_prefix = None

        self.channels = set()
        self.chan_modemap = {}
        self.server_modemap = {}
        self.user_chan_modemap = {}
        self.topic_map = {}
        self.channel_memberships = {}
        self.is_signed_on = False
        self.init_time = time.time()

        for mname in self.overrideable:
            realmethod = getattr(self, mname, noop)
            wrappedmethod = self.make_watch_wrapper(mname, realmethod)
            setattr(self, mname, wrappedmethod)

    def make_watch_wrapper(self, mname, realmethod):
        @defer.inlineCallbacks
        def wrapper(*a, **kw):
            realresult = realmethod(*a, **kw)
            watchers = self.service.watcher_map.get(mname, ())
            for w in watchers:
                pluginmethod = getattr(w, mname, noop)
                try:
                    yield pluginmethod(self, *a, **kw)
                except Exception, e:
                    log.err(None, 'Exception in plugin %s for method %r'
                                  % (w.name(), mname))
            defer.returnValue(realresult)
        wrapper.func_name = 'wrapper_for_%s' % mname
        return wrapper

    def add_channel(self, channel):
        self.channels.add(channel)

    def leave_channel(self, channel):
        self.channels.discard(channel)
        removekey(self.topic_map, channel)
        removekey(self.chan_modemap, channel)
        removekey(self.channel_memberships, channel)

    def dispatch_command(self, user, channel, cmd, args):
        cmd = cmd.lower()
        mname = 'command_' + cmd
        handled = 0
        for p in self.service.command_map.get(cmd, ()):
            try:
                pluginmethod = getattr(p, mname)
            except AttributeError:
                continue
            handled += 1
            d = defer.maybeDeferred(pluginmethod, user, channel, cmd, args)
            d.addErrback(log.err, "Exception in plugin %s while in %s"
                                  % (p.name(), mname))
        if handled == 0:
            self.command_not_found(user, channel, cmd)

    def address_msg(self, user, channel, msg):
        if user != channel:
            if '!' in user:
                user = user.split('!', 1)[0]
            msg = '%s: %s' % (user, msg)
        return self.msg(channel, msg)

    def command_not_found(self, user, channel, cmd):
        self.address_msg(user, channel, "Sorry, I don't understand %r. :(" % cmd)

    ### methods called by the protocol

    def myInfo(self, servername, version, umodes, cmodes):
        self.servername = servername
        self.serverversion = version
        self.available_umodes = umodes
        self.available_cmodes = cmodes

    def yourHost(self, info):
        self.serverdaemon_info = info

    def luserMe(self, info):
        self.serverhost_info = info

    def privmsg(self, user, channel, message):
        cmdstr = None
        if user == channel:
            cmdstr = message
        if message.startswith('%s:' % (self.nickname,)):
            cmdstr = message[len(self.nickname)+1:]
        elif self.cmd_prefix is not None and message.startswith(self.cmd_prefix):
            cmdstr = message[len(self.cmd_prefix):]
        if cmdstr is not None:
            parts = shlex.split(cmdstr.strip())
            cmd = parts[0]
            args = parts[1:]
            self.dispatch_command(user, channel, cmd, args)

    def joined(self, channel):
        self.add_channel(channel)

    def left(self, channel):
        self.leave_channel(channel)

    def kickedFrom(self, channel, kicker, message):
        self.leave_channel(channel)

    def modeChanged(self, user, channel, beingset, modes, args):
        for m in modes:
            if user == channel:
                if beingset:
                    self.server_modemap.setdefault(user, {})[m] = args
                else:
                    try:
                        del self.server_modemap[user][m]
                    except KeyError:
                        pass
            else:
                if beingset:
                    self.chan_modemap.setdefault(channel, {})[m] = (user,) + args
                else:
                    try:
                        del self.chan_modemap[channel][m]
                    except KeyError:
                        pass

    def signedOn(self):
        self.factory.prot = self
        self.factory.resetDelay()
        for chan in self.join_channels:
            self.join(chan)
        self.is_signed_on = True
        self.sign_on_time = time.time()

    def userJoined(self, user, channel):
        self.channel_memberships.setdefault(channel, set()).add(user)

    def userLeft(self, user, channel):
        self.channel_memberships.setdefault(channel, set()).discard(user)

    userQuit = userKicked = userLeft

    def topicUpdated(self, user, channel, newTopic):
        self.topic_map[channel] = newTopic

    def userRenamed(self, oldname, newname):
        for cm in self.channel_memberships.values():
            if oldname in cm:
                cm.add(newname)
                cm.remove(oldname)

    def connectionLost(self, reason):
        self.is_signed_on = False
        try:
            del self.factory.prot
        except AttributeError:
            pass
        return irc.IRCClient.connectionLost(self, reason)

    def lineReceived(self, line):
        log.msg('line received: %r' % line)
        return irc.IRCClient.lineReceived(self, line)


class BotLogger(BaseBotPlugin):
    eterno_blacklist = ['evn']

    def __init__(self, blacklist=()):
        self.log_blacklist = self.eterno_blacklist + list(blacklist)

    def irclog(self, bot, *a, **kw):
        kw['mtype'] = 'irclog'
        return log.msg(*a, **kw)

    def signedOn(self, bot):
        self.irclog("Signed on as %s." % (self.nickname,))

    def joined(self, bot, channel):
        self.irclog("Joined %s." % (channel,))

    def left(self, bot, channel):
        self.irclog("Left %s." % (channel,))

    def noticed(self, bot, user, chan, msg):
        self.irclog("NOTICE -!- [%s] <%s> %s" % (chan, user, msg))

    def modeChanged(self, bot, user, chan, being_set, modes, args):
        self.irclog("MODE -!- %s %s modes %r in %r for %r" % (
            user,
            'set' if being_set else 'unset',
            modes,
            chan,
            args
        ))

    def kickedFrom(self, bot, chan, kicker, msg):
        self.irclog('KICKED -!- from %s by %s [%s]' % (chan, kicker, msg))

    def nickChanged(self, bot, nick):
        self.irclog('NICKCHANGE -!- my nick changed to %s' % (nick,))

    def userJoined(self, bot, user, chan):
        self.irclog('%s joined %s' % (user, chan))

    def userLeft(self, bot, user, chan):
        self.irclog('%s left %s' % (user, chan))

    def userQuit(self, bot, user, msg):
        self.irclog('%s quit [%s]' % (user, msg))

    def userKicked(self, bot, kickee, chan, kicker, msg):
        self.irclog('%s was kicked from %s by %s [%s]' % (kickee, chan, kicker, msg))

    def topicUpdated(self, bot, user, chan, newtopic):
        self.irclog('[%s] -!- topic changed by %s to %r' % (chan, user, newtopic))

    def userRenamed(self, bot, oldname, newname):
        self.irclog('RENAME %s is now known as %s' % (oldname, newname))

    def receivedMOTD(self, bot, motd):
        self.irclog('MOTD %s' % (motd,))

    def msg(self, dest, msg, length=None):
        self.irclog('[%s] <%s> %s' % (dest, self.nickname, msg))

    def action(self, bot, user, chan, data):
        user = user.split('!', 1)[0]
        if user not in self.log_blacklist:
            self.irclog('[%s] * %s %s' % (chan, user, data))

    def privmsg(self, user, channel, msg):
        user = user.split('!', 1)[0]
        if user not in self.log_blacklist:
            self.irclog('[%s] <%s> %s' % (channel, user, msg))


class CassandraLinkChecker(BaseBotPlugin):
    ticket_re = re.compile(r'(?:^|[]\s[(){}<>/:",-])(#{1,2})(\d+)\b')
    commit_re = re.compile(r'\br(\d+)\b')
    low_ticket_cutoff = 10

    def checktickets(self, msg):
        for match in self.ticket_re.finditer(msg):
            ticket = int(match.group(2))
            if ticket > self.low_ticket_cutoff or match.group(1) == '##':
                yield self.post_ticket(ticket)

    def post_ticket(self, ticket_num):
        return 'http://issues.apache.org/jira/browse/CASSANDRA-%d' % (ticket_num,)

    def checkrevs(self, msg):
        for match in self.commit_re.finditer(msg):
            commit = int(match.group(1))
            yield 'http://svn.apache.org/viewvc?view=rev&revision=%d' % (commit,)

    @defer.inlineCallbacks
    def privmsg(self, bot, user, channel, msg):
        responses = list(self.checktickets(msg)) \
                  + list(self.checkrevs(msg))
        for r in responses:
            yield bot.msg(channel, r)


class LogCommand(BaseBotPlugin):
    def command_logs(self, bot, user, channel, args):
        return bot.msg(channel, 'http://www.eflorenzano.com/cassbot/')


class BuildCommand(BaseBotPlugin):
    build_token = 'xxxxxxxxxxxx'
    build_url = 'http://hudson.zones.apache.org/hudson/job'

    @defer.inlineCallbacks
    def command_build(self, bot, user, channel, args):
        if not args[0]:
            yield bot.msg(channel, "usage: build <buildname>")
            return
        url = '%s/%s/polling?token=%s' % (self.build_url, args[0], self.build_token)
        msg = "request sent!"
        try:
            res = yield client.getPage(url)
        except error.Error, e:
            # Hudson returns a 404 even when this request succeeds :/
            if e.status == '404':
                pass
            else:
                msg = str(e)
        bot.address_msg(user, channel, msg)


class CassBotFactory(protocol.ReconnectingClientFactory):
    protocol = CassBotCore

    def buildProtocol(self, addr):
        p = protocol.ReconnectingClientFactory.buildProtocol(self, addr)
        self.service.initialize_proto_state(p)
        return p


class CassBotService(service.MultiService):
    plugin_scan_period = 240

    def __init__(self, host, port, nickname='cassbot', init_channels=(), reactor=None):
        service.MultiService.__init__(self)

        self.myhost = host
        self.myport = int(port)

        self.state = {
            'nickname': nickname,
            'channels': init_channels,
            'cmd_prefix': None
        }

        if reactor is None:
            from twisted.internet import reactor
        self.reactor = reactor

        self.watcher_map = {}
        self.command_map = {}

        self.plugin_scanner = internet.TimerService(self.plugin_scan_period, self.scan_plugins)
        self.plugin_scanner.setServiceParent(self)

        self.pfactory = CassBotFactory()
        self.client = internet.TCPClient(self.myhost, self.myport, self.pfactory, reactor=reactor)
        self.client.setServiceParent(self)

    def startService(self):
        self.pfactory.service = self
        return service.MultiService.startService(self)

    def stopService(self):
        self.pfactory.service = None
        return service.MultiService.stopService(self)

    def scan_plugins(self):
        self.watcher_map = {}
        self.command_map = {}
        for p in getPlugins(IBotPlugin, plugins):
            try:
                for methodname in p.interestingMethods():
                    self.watcher_map.setdefault(methodname, []).append(p)
            except Exception:
                log.err(None, 'Exception in plugin %s for interestingMethods request'
                              % (p.name(),))
            try:
                for cmdname in p.implementedCommands():
                    self.command_map.setdefault(cmdname, []).append(p)
            except Exception:
                log.err(None, 'Exception in plugin %s for implementedCommands request'
                              % (p.name(),))

    def initialize_proto_state(self, proto):
        proto.nickname = self.state['nickname']
        proto.join_channels = self.state.get('channels', ())
        proto.cmd_prefix = self.state.get('cmd_prefix', None)
        proto.service = self

    def __str__(self):
        return '<%s object [%s:%d]%s>' % (
            self.__class__.__name__,
            self.myhost,
            self.myport,
            ' (connected)' if hasattr(self.pfactory, 'prot') else ''
        )

    def getbot(self):
        return self.pfactory.prot


# vim: set et sw=4 ts=4 :