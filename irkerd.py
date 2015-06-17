#!/usr/bin/env python
"""
irkerd - a simple IRC multiplexer daemon

Listens for JSON objects of the form {'to':<irc-url>, 'privmsg':<text>}
and relays messages to IRC channels. Each request must be followed by
a newline.

The <text> must be a string.  The value of the 'to' attribute can be a
string containing an IRC URL (e.g. 'irc://chat.freenet.net/botwar') or
a list of such strings; in the latter case the message is broadcast to
all listed channels.  Note that the channel portion of the URL need
*not* have a leading '#' unless the channel name itself does.

Design and code by Eric S. Raymond <esr@thyrsus.com>. See the project
resource page at <http://www.catb.org/~esr/irker/>.

Requires Python 2.7, or:
* 2.6 with the argparse package installed.
"""

from __future__ import unicode_literals
from __future__ import with_statement

# These things might need tuning

HOST = "localhost"
PORT = 6659

XMIT_TTL = (3 * 60 * 60)	# Time to live, seconds from last transmit
PING_TTL = (15 * 60)		# Time to live, seconds from last PING
HANDSHAKE_TTL = 60		# Time to live, seconds from nick transmit
CHANNEL_TTL = (3 * 60 * 60)	# Time to live, seconds from last transmit
DISCONNECT_TTL = (24 * 60 * 60)	# Time to live, seconds from last connect
UNSEEN_TTL = 60			# Time to live, seconds since first request
CHANNEL_MAX = 18		# Max channels open per socket (default)
ANTI_FLOOD_DELAY = 1.0		# Anti-flood delay after transmissions, seconds
ANTI_BUZZ_DELAY = 0.09		# Anti-buzz delay after queue-empty check
CONNECTION_MAX = 200		# To avoid hitting a thread limit

# No user-serviceable parts below this line

version = "&&IRKVERSION&&"

import argparse
import logging
import logging.handlers
import json
import os
import os.path
try:  # Python 3
    import queue
except ImportError:  # Python 2
    import Queue as queue
import random
import re
import select
import signal
import socket
try:  # Python 3
    import socketserver
except ImportError:  # Python 2
    import SocketServer as socketserver
import ssl
import sys
import threading
import time
import traceback
try:  # Python 3
    import urllib.parse as urllib_parse
except ImportError:  # Python 2
    import urlparse as urllib_parse


LOG = logging.getLogger(__name__)
LOG.setLevel(logging.ERROR)
LOG_LEVELS = ['critical', 'error', 'warning', 'info', 'debug']

try:  # Python 2
    UNICODE_TYPE = unicode
except NameError:  # Python 3
    UNICODE_TYPE = str


# Sketch of implementation:
#
# One Irker object manages multiple IRC sessions.  It holds a map of
# Dispatcher objects, one per (server, port) combination, which are
# responsible for routing messages to one of any number of Connection
# objects that do the actual socket conversations.  The reason for the
# Dispatcher layer is that IRC daemons limit the number of channels a
# client (that is, from the daemon's point of view, a socket) can be
# joined to, so each session to a server needs a flock of Connection
# instances each with its own socket.
#
# Connections are timed out and removed when either they haven't seen a
# PING for a while (indicating that the server may be stalled or down)
# or there has been no message traffic to them for a while, or
# even if the queue is nonempty but efforts to connect have failed for
# a long time.
#
# There are multiple threads. One accepts incoming traffic from all
# servers.  Each Connection also has a consumer thread and a
# thread-safe message queue.  The program main appends messages to
# queues as JSON requests are received; the consumer threads try to
# ship them to servers.  When a socket write stalls, it only blocks an
# individual consumer thread; if it stalls long enough, the session
# will be timed out. This solves the biggest problem with a
# single-threaded implementation, which is that you can't count on a
# single stalled write not hanging all other traffic - you're at the
# mercy of the length of the buffers in the TCP/IP layer.
#
# Message delivery is thus not reliable in the face of network stalls,
# but this was considered acceptable because IRC (notoriously) has the
# same problem - there is little point in reliable delivery to a relay
# that is down or unreliable.
#
# This code uses only NICK, JOIN, PART, MODE, PRIVMSG, USER, and QUIT.
# It is strictly compliant to RFC1459, except for the interpretation and
# use of the DEAF and CHANLIMIT and (obsolete) MAXCHANNELS features.
#
# CHANLIMIT is as described in the Internet RFC draft
# draft-brocklesby-irc-isupport-03 at <http://www.mirc.com/isupport.html>.
# The ",isnick" feature is as described in
# <http://ftp.ics.uci.edu/pub/ietf/uri/draft-mirashi-url-irc-01.txt>.

# Historical note: the IRCClient and IRCServerConnection classes
# (~270LOC) replace the overweight, overcomplicated 3KLOC mass of
# irclib code that irker formerly used as a service library.  They
# still look similar to parts of irclib because I contributed to that
# code before giving up on it.

class IRCError(Exception):
    "An IRC exception"
    pass


class InvalidRequest(ValueError):
    "An invalid JSON request"
    pass


class IRCClient():
    "An IRC client session to one or more servers."
    def __init__(self):
        self.mutex = threading.RLock()
        self.server_connections = []
        self.event_handlers = {}
        self.add_event_handler("ping",
                               lambda c, e: c.ship("PONG %s" % e.target))

    def newserver(self):
        "Initialize a new server-connection object."
        conn = IRCServerConnection(self)
        with self.mutex:
            self.server_connections.append(conn)
        return conn

    def spin(self, timeout=0.2):
        "Spin processing data from connections forever."
        # Outer loop should specifically *not* be mutex-locked.
        # Otherwise no other thread would ever be able to change
        # the shared state of an IRC object running this function.
        while True:
            nextsleep = 0
            with self.mutex:
                connected = [x for x in self.server_connections
                             if x is not None and x.socket is not None]
                sockets = [x.socket for x in connected]
                if sockets:
                    connmap = dict([(c.socket.fileno(), c) for c in connected])
                    (insocks, _o, _e) = select.select(sockets, [], [], timeout)
                    for s in insocks:
                        try:
                            connmap[s.fileno()].consume()
                        except UnicodeDecodeError as e:
                            LOG.warn('{0}: invalid encoding ({1})'.format(
                                self, e))
                else:
                    nextsleep = timeout
            time.sleep(nextsleep)

    def add_event_handler(self, event, handler):
        "Set a handler to be called later."
        with self.mutex:
            event_handlers = self.event_handlers.setdefault(event, [])
            event_handlers.append(handler)

    def handle_event(self, connection, event):
        with self.mutex:
            h = self.event_handlers
            th = sorted(h.get("all_events", []) + h.get(event.type, []))
            for handler in th:
                handler(connection, event)

    def drop_connection(self, connection):
        with self.mutex:
            self.server_connections.remove(connection)


class LineBufferedStream():
    "Line-buffer a read stream."
    _crlf_re = re.compile(b'\r?\n')

    def __init__(self):
        self.buffer = b''

    def append(self, newbytes):
        self.buffer += newbytes

    def lines(self):
        "Iterate over lines in the buffer."
        lines = self._crlf_re.split(self.buffer)
        self.buffer = lines.pop()
        return iter(lines)

    def __iter__(self):
        return self.lines()

class IRCServerConnectionError(IRCError):
    pass

class IRCServerConnection():
    command_re = re.compile("^(:(?P<prefix>[^ ]+) +)?(?P<command>[^ ]+)( *(?P<argument> .+))?")
    # The full list of numeric-to-event mappings is in Perl's Net::IRC.
    # We only need to ensure that if some ancient server throws numerics
    # for the ones we actually want to catch, they're mapped.
    codemap = {
        "001": "welcome",
        "005": "featurelist",
        "432": "erroneusnickname",
        "433": "nicknameinuse",
        "436": "nickcollision",
        "437": "unavailresource",
    }

    def __init__(self, master):
        self.master = master
        self.socket = None

    def _wrap_socket(self, socket, target, certfile=None, cafile=None,
                     protocol=ssl.PROTOCOL_TLSv1):
        try:  # Python 3.2 and greater
            ssl_context = ssl.SSLContext(protocol)
        except AttributeError:  # Python < 3.2
            self.socket = ssl.wrap_socket(
                socket, certfile=certfile, cert_reqs=ssl.CERT_REQUIRED,
                ssl_version=protocol, ca_certs=cafile)
        else:
            ssl_context.verify_mode = ssl.CERT_REQUIRED
            if certfile:
                ssl_context.load_cert_chain(certfile)
            if cafile:
                ssl_context.load_verify_locations(cafile=cafile)
            else:
                ssl_context.set_default_verify_paths()
            kwargs = {}
            if ssl.HAS_SNI:
                kwargs['server_hostname'] = target.servername
            self.socket = ssl_context.wrap_socket(socket, **kwargs)
        return self.socket

    def _check_hostname(self, target):
        if hasattr(ssl, 'match_hostname'):  # Python >= 3.2
            cert = self.socket.getpeercert()
            try:
                ssl.match_hostname(cert, target.servername)
            except ssl.CertificateError as e:
                raise IRCServerConnectionError(
                    'Invalid SSL/TLS certificate: %s' % e)
        else:  # Python < 3.2
            LOG.warning(
                'cannot check SSL/TLS hostname with Python %s' % sys.version)

    def connect(self, target, nickname, username=None, realname=None,
                **kwargs):
        LOG.debug("connect(server=%r, port=%r, nickname=%r, ...)" % (
            target.servername, target.port, nickname))
        if self.socket is not None:
            self.disconnect("Changing servers")

        self.buffer = LineBufferedStream()
        self.event_handlers = {}
        self.real_server_name = ""
        self.target = target
        self.nickname = nickname
        try:
            self.socket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            if target.ssl:
                self.socket = self._wrap_socket(
                    socket=self.socket, target=target, **kwargs)
            self.socket.bind(('', 0))
            self.socket.connect((target.servername, target.port))
        except socket.error as err:
            raise IRCServerConnectionError("Couldn't connect to socket: %s" % err)

        if target.ssl:
            self._check_hostname(target=target)
        if target.password:
            self.ship("PASS " + target.password)
        self.nick(self.nickname)
        self.user(
            username=target.username or username or 'irker',
            realname=realname or 'irker relaying client')
        return self

    def close(self):
        # Without this thread lock, there is a window during which
        # select() can find a closed socket, leading to an EBADF error.
        with self.master.mutex:
            self.disconnect("Closing object")
            self.master.drop_connection(self)

    def consume(self):
        try:
            incoming = self.socket.recv(16384)
        except socket.error:
            # Server hung up on us.
            self.disconnect("Connection reset by peer")
            return
        if not incoming:
            # Dead air also indicates a connection reset.
            self.disconnect("Connection reset by peer")
            return

        self.buffer.append(incoming)

        for line in self.buffer:
            if not isinstance(line, UNICODE_TYPE):
                line = UNICODE_TYPE(line, 'utf-8')
                LOG.debug("FROM: %s" % line)

            if not line:
                continue

            prefix = None
            command = None
            arguments = None
            self.handle_event(Event("every_raw_message",
                                     self.real_server_name,
                                     None,
                                     [line]))

            m = IRCServerConnection.command_re.match(line)
            if m.group("prefix"):
                prefix = m.group("prefix")
                if not self.real_server_name:
                    self.real_server_name = prefix
            if m.group("command"):
                command = m.group("command").lower()
            if m.group("argument"):
                a = m.group("argument").split(" :", 1)
                arguments = a[0].split()
                if len(a) == 2:
                    arguments.append(a[1])

            command = IRCServerConnection.codemap.get(command, command)
            if command in ["privmsg", "notice"]:
                target = arguments.pop(0)
            else:
                target = None

                if command == "quit":
                    arguments = [arguments[0]]
                elif command == "ping":
                    target = arguments[0]
                else:
                    target = arguments[0]
                    arguments = arguments[1:]

            LOG.debug("command: %s, source: %s, target: %s, arguments: %s" % (
                command, prefix, target, arguments))
            self.handle_event(Event(command, prefix, target, arguments))

    def handle_event(self, event):
        self.master.handle_event(self, event)
        if event.type in self.event_handlers:
            for fn in self.event_handlers[event.type]:
                fn(self, event)

    def is_connected(self):
        return self.socket is not None

    def disconnect(self, message=""):
        if self.socket is None:
            return
        # Don't send a QUIT here - causes infinite loop!
        try:
            self.socket.shutdown(socket.SHUT_WR)
            self.socket.close()
        except socket.error:
            pass
        del self.socket
        self.socket = None
        self.handle_event(
            Event("disconnect", self.target.server, "", [message]))

    def join(self, channel, key=""):
        self.ship("JOIN %s%s" % (channel, (key and (" " + key))))

    def mode(self, target, command):
        self.ship("MODE %s %s" % (target, command))

    def nick(self, newnick):
        self.ship("NICK " + newnick)

    def part(self, channel, message=""):
        cmd_parts = ['PART', channel]
        if message:
            cmd_parts.append(message)
        self.ship(' '.join(cmd_parts))

    def privmsg(self, target, text):
        self.ship("PRIVMSG %s :%s" % (target, text))

    def quit(self, message=""):
        self.ship("QUIT" + (message and (" :" + message)))

    def user(self, username, realname):
        self.ship("USER %s 0 * :%s" % (username, realname))

    def ship(self, string):
        "Ship a command to the server, appending CR/LF"
        try:
            self.socket.send(string.encode('utf-8') + b'\r\n')
            LOG.debug("TO: %s" % string)
        except socket.error:
            self.disconnect("Connection reset by peer.")

class Event(object):
    def __init__(self, evtype, source, target, arguments=None):
        self.type = evtype
        self.source = source
        self.target = target
        if arguments is None:
            arguments = []
        self.arguments = arguments

def is_channel(string):
    return string and string[0] in "#&+!"

class Connection:
    def __init__(self, irker, target, nick_template, nick_needs_number=False,
                 password=None, **kwargs):
        self.irker = irker
        self.target = target
        self.nick_template = nick_template
        self.nick_needs_number = nick_needs_number
        self.password = password
        self.kwargs = kwargs
        self.nick_trial = None
        self.connection = None
        self.status = None
        self.last_xmit = time.time()
        self.last_ping = time.time()
        self.channels_joined = {}
        self.channel_limits = {}
        # The consumer thread
        self.queue = queue.Queue()
        self.thread = None
    def nickname(self, n=None):
        "Return a name for the nth server connection."
        if n is None:
            n = self.nick_trial
        if self.nick_needs_number:
            return self.nick_template % n
        else:
            return self.nick_template
    def handle_ping(self):
        "Register the fact that the server has pinged this connection."
        self.last_ping = time.time()
    def handle_welcome(self):
        "The server says we're OK, with a non-conflicting nick."
        self.status = "ready"
        LOG.info("nick %s accepted" % self.nickname())
        if self.password:
            self.connection.privmsg("nickserv", "identify %s" % self.password)
    def handle_badnick(self):
        "The server says our nick is ill-formed or has a conflict."
        LOG.info("nick %s rejected" % self.nickname())
        if self.nick_needs_number:
            # Randomness prevents a malicious user or bot from
            # anticipating the next trial name in order to block us
            # from completing the handshake.
            self.nick_trial += random.randint(1, 3)
            self.last_xmit = time.time()
            self.connection.nick(self.nickname())
        # Otherwise fall through, it might be possible to
        # recover manually.
    def handle_disconnect(self):
        "Server disconnected us for flooding or some other reason."
        self.connection = None
        if self.status != "expired":
            self.status = "disconnected"
    def handle_kick(self, outof):
        "We've been kicked."
        self.status = "handshaking"
        try:
            del self.channels_joined[outof]
        except KeyError:
            LOG.error("irkerd: kicked by %s from %s that's not joined" % (
                self.target, outof))
        qcopy = []
        while not self.queue.empty():
            (channel, message, key) = self.queue.get()
            if channel != outof:
                qcopy.append((channel, message, key))
        for (channel, message, key) in qcopy:
            self.queue.put((channel, message, key))
        self.status = "ready"
    def enqueue(self, channel, message, key, quit_after=False):
        "Enque a message for transmission."
        if self.thread is None or not self.thread.is_alive():
            self.status = "unseen"
            self.thread = threading.Thread(target=self.dequeue)
            self.thread.setDaemon(True)
            self.thread.start()
        self.queue.put((channel, message, key))
        if quit_after:
            self.queue.put((channel, None, key))
    def dequeue(self):
        "Try to ship pending messages from the queue."
        try:
            while True:
                # We want to be kind to the IRC servers and not hold unused
                # sockets open forever, so they have a time-to-live.  The
                # loop is coded this particular way so that we can drop
                # the actual server connection when its time-to-live
                # expires, then reconnect and resume transmission if the
                # queue fills up again.
                if self.queue.empty():
                    # Queue is empty, at some point we want to time out
                    # the connection rather than holding a socket open in
                    # the server forever.
                    now = time.time()
                    xmit_timeout = now > self.last_xmit + XMIT_TTL
                    ping_timeout = now > self.last_ping + PING_TTL
                    if self.status == "disconnected":
                        # If the queue is empty, we can drop this connection.
                        self.status = "expired"
                        break
                    elif xmit_timeout or ping_timeout:
                        LOG.info((
                            "timing out connection to %s at %s "
                            "(ping_timeout=%s, xmit_timeout=%s)") % (
                            self.target, time.asctime(), ping_timeout,
                            xmit_timeout))
                        with self.irker.irc.mutex:
                            self.connection.context = None
                            self.connection.quit("transmission timeout")
                            self.connection = None
                        self.status = "disconnected"
                    else:
                        # Prevent this thread from hogging the CPU by pausing
                        # for just a little bit after the queue-empty check.
                        # As long as this is less that the duration of a human
                        # reflex arc it is highly unlikely any human will ever
                        # notice.
                        time.sleep(ANTI_BUZZ_DELAY)
                elif self.status == "disconnected" \
                         and time.time() > self.last_xmit + DISCONNECT_TTL:
                    # Queue is nonempty, but the IRC server might be
                    # down. Letting failed connections retain queue
                    # space forever would be a memory leak.
                    self.status = "expired"
                    break
                elif not self.connection and self.status != "expired":
                    # Queue is nonempty but server isn't connected.
                    with self.irker.irc.mutex:
                        self.connection = self.irker.irc.newserver()
                        self.connection.context = self
                        # Try to avoid colliding with other instances
                        self.nick_trial = random.randint(1, 990)
                        self.channels_joined = {}
                        try:
                            # This will throw
                            # IRCServerConnectionError on failure
                            self.connection.connect(
                                target=self.target,
                                nickname=self.nickname(),
                                **self.kwargs)
                            self.status = "handshaking"
                            LOG.info("XMIT_TTL bump (%s connection) at %s" % (
                                self.target, time.asctime()))
                            self.last_xmit = time.time()
                            self.last_ping = time.time()
                        except IRCServerConnectionError as e:
                            LOG.error("irkerd: %s" % e)
                            self.status = "expired"
                            break
                elif self.status == "handshaking":
                    if time.time() > self.last_xmit + HANDSHAKE_TTL:
                        self.status = "expired"
                        break
                    else:
                        # Don't buzz on the empty-queue test while we're
                        # handshaking
                        time.sleep(ANTI_BUZZ_DELAY)
                elif self.status == "unseen" \
                         and time.time() > self.last_xmit + UNSEEN_TTL:
                    # Nasty people could attempt a denial-of-service
                    # attack by flooding us with requests with invalid
                    # servernames. We guard against this by rapidly
                    # expiring connections that have a nonempty queue but
                    # have never had a successful open.
                    self.status = "expired"
                    break
                elif self.status == "ready":
                    (channel, message, key) = self.queue.get()
                    if channel not in self.channels_joined:
                        self.connection.join(channel, key=key)
                        LOG.info("joining %s on %s." % (channel, self.target))
                    # None is magic - it's a request to quit the server
                    if message is None:
                        self.connection.quit()
                    # An empty message might be used as a keepalive or
                    # to join a channel for logging, so suppress the
                    # privmsg send unless there is actual traffic.
                    elif message:
                        for segment in message.split("\n"):
                            # Truncate the message if it's too long,
                            # but we're working with characters here,
                            # not bytes, so we could be off.
                            # 500 = 512 - CRLF - 'PRIVMSG ' - ' :'
                            maxlength = 500 - len(channel)
                            if len(segment) > maxlength:
                                segment = segment[:maxlength]
                            try:
                                self.connection.privmsg(channel, segment)
                            except ValueError as err:
                                LOG.warning((
                                    "irclib rejected a message to %s on %s "
                                    "because: %s") % (
                                    channel, self.target, UNICODE_TYPE(err)))
                                LOG.debug(traceback.format_exc())
                            time.sleep(ANTI_FLOOD_DELAY)
                    self.last_xmit = self.channels_joined[channel] = time.time()
                    LOG.info("XMIT_TTL bump (%s transmission) at %s" % (
                        self.target, time.asctime()))
                    self.queue.task_done()
                elif self.status == "expired":
                    LOG.error(
                        "irkerd: we're expired but still running! This is a bug.")
                    break
        except Exception as e:
            LOG.error("irkerd: exception %s in thread for %s" % (e, self.target))
            # Maybe this should have its own status?
            self.status = "expired"
            LOG.debug(traceback.format_exc())
        finally:
            try:
                # Make sure we don't leave any zombies behind
                self.connection.close()
            except:
                # Irclib has a habit of throwing fresh exceptions here. Ignore that
                pass
    def live(self):
        "Should this connection not be scavenged?"
        return self.status != "expired"
    def joined_to(self, channel):
        "Is this connection joined to the specified channel?"
        return channel in self.channels_joined
    def accepting(self, channel):
        "Can this connection accept a join of this channel?"
        if self.channel_limits:
            match_count = 0
            for already in self.channels_joined:
                # This obscure code is because the RFCs allow separate limits
                # by channel type (indicated by the first character of the name)
                # a feature that is almost never actually used.
                if already[0] == channel[0]:
                    match_count += 1
            return match_count < self.channel_limits.get(channel[0], CHANNEL_MAX)
        else:
            return len(self.channels_joined) < CHANNEL_MAX

class Target():
    "Represent a transmission target."
    def __init__(self, url):
        self.url = url
        parsed = urllib_parse.urlparse(url)
        self.ssl = parsed.scheme == 'ircs'
        if self.ssl:
            default_ircport = 6697
        else:
            default_ircport = 6667
        self.username = parsed.username
        self.password = parsed.password
        self.servername = parsed.hostname
        self.port = parsed.port or default_ircport
        # IRC channel names are case-insensitive.  If we don't smash
        # case here we may run into problems later. There was a bug
        # observed on irc.rizon.net where an irkerd user specified #Channel,
        # got kicked, and irkerd crashed because the server returned
        # "#channel" in the notification that our kick handler saw.
        self.channel = parsed.path.lstrip('/').lower()
        # This deals with a tweak in recent versions of urlparse.
        if parsed.fragment:
            self.channel += "#" + parsed.fragment
        isnick = self.channel.endswith(",isnick")
        if isnick:
            self.channel = self.channel[:-7]
        if self.channel and not isnick and self.channel[0] not in "#&+":
            self.channel = "#" + self.channel
        # support both channel?secret and channel?key=secret
        self.key = ""
        if parsed.query:
            self.key = re.sub("^key=", "", parsed.query)

    def __str__(self):
        "Represent this instance as a string"
        return self.servername or self.url or repr(self)

    def validate(self):
        "Raise InvalidRequest if the URL is missing a critical component"
        if not self.servername:
            raise InvalidRequest(
                'target URL missing a servername: %r' % self.url)
        if not self.channel:
            raise InvalidRequest(
                'target URL missing a channel: %r' % self.url)
    def server(self):
        "Return a hashable tuple representing the destination server."
        return (self.servername, self.port)

class Dispatcher:
    "Manage connections to a particular server-port combination."
    def __init__(self, irker, **kwargs):
        self.irker = irker
        self.kwargs = kwargs
        self.connections = []
    def dispatch(self, channel, message, key, quit_after=False):
        "Dispatch messages for our server-port combination."
        # First, check if there is room for another channel
        # on any of our existing connections.
        connections = [x for x in self.connections if x.live()]
        eligibles = [x for x in connections if x.joined_to(channel)] \
                    or [x for x in connections if x.accepting(channel)]
        if eligibles:
            eligibles[0].enqueue(channel, message, key, quit_after)
            return
        # All connections are full up. Look for one old enough to be
        # scavenged.
        ancients = []
        for connection in connections:
            for (chan, age) in connections.channels_joined.items():
                if age < time.time() - CHANNEL_TTL:
                    ancients.append((connection, chan, age))
        if ancients:
            ancients.sort(key=lambda x: x[2])
            (found_connection, drop_channel, _drop_age) = ancients[0]
            found_connection.part(drop_channel, "scavenged by irkerd")
            del found_connection.channels_joined[drop_channel]
            #time.sleep(ANTI_FLOOD_DELAY)
            found_connection.enqueue(channel, message, key, quit_after)
            return
        # All existing channels had recent activity
        newconn = Connection(self.irker, **self.kwargs)
        self.connections.append(newconn)
        newconn.enqueue(channel, message, key, quit_after)
    def live(self):
        "Does this server-port combination have any live connections?"
        self.connections = [x for x in self.connections if x.live()]
        return len(self.connections) > 0
    def pending(self):
        "Return all connections with pending traffic."
        return [x for x in self.connections if not x.queue.empty()]
    def last_xmit(self):
        "Return the time of the most recent transmission."
        return max(x.last_xmit for x in self.connections)

class Irker:
    "Persistent IRC multiplexer."
    def __init__(self, logfile=None, **kwargs):
        self.logfile = logfile
        self.kwargs = kwargs
        self.irc = IRCClient()
        self.irc.add_event_handler("ping", self._handle_ping)
        self.irc.add_event_handler("welcome", self._handle_welcome)
        self.irc.add_event_handler("erroneusnickname", self._handle_badnick)
        self.irc.add_event_handler("nicknameinuse", self._handle_badnick)
        self.irc.add_event_handler("nickcollision", self._handle_badnick)
        self.irc.add_event_handler("unavailresource", self._handle_badnick)
        self.irc.add_event_handler("featurelist", self._handle_features)
        self.irc.add_event_handler("disconnect", self._handle_disconnect)
        self.irc.add_event_handler("kick", self._handle_kick)
        self.irc.add_event_handler("every_raw_message", self._handle_every_raw_message)
        self.servers = {}
    def thread_launch(self):
        thread = threading.Thread(target=self.irc.spin)
        thread.setDaemon(True)
        self.irc._thread = thread
        thread.start()
    def _handle_ping(self, connection, _event):
        "PING arrived, bump the last-received time for the connection."
        if connection.context:
            connection.context.handle_ping()
    def _handle_welcome(self, connection, _event):
        "Welcome arrived, nick accepted for this connection."
        if connection.context:
            connection.context.handle_welcome()
    def _handle_badnick(self, connection, _event):
        "Nick not accepted for this connection."
        if connection.context:
            connection.context.handle_badnick()
    def _handle_features(self, connection, event):
        "Determine if and how we can set deaf mode."
        if connection.context:
            cxt = connection.context
            arguments = event.arguments
            for lump in arguments:
                if lump.startswith("DEAF="):
                    if not self.logfile:
                        connection.mode(cxt.nickname(), "+"+lump[5:])
                elif lump.startswith("MAXCHANNELS="):
                    m = int(lump[12:])
                    for pref in "#&+":
                        cxt.channel_limits[pref] = m
                    LOG.info("%s maxchannels is %d" % (connection.target, m))
                elif lump.startswith("CHANLIMIT=#:"):
                    limits = lump[10:].split(",")
                    try:
                        for token in limits:
                            (prefixes, limit) = token.split(":")
                            limit = int(limit)
                            for c in prefixes:
                                cxt.channel_limits[c] = limit
                        LOG.info("%s channel limit map is %s" % (
                            connection.target, cxt.channel_limits))
                    except ValueError:
                        LOG.error("irkerd: ill-formed CHANLIMIT property")
    def _handle_disconnect(self, connection, _event):
        "Server hung up the connection."
        LOG.info("server %s disconnected" % connection.target)
        connection.close()
        if connection.context:
            connection.context.handle_disconnect()
    def _handle_kick(self, connection, event):
        "Server hung up the connection."
        target = event.target
        LOG.info("irker has been kicked from %s on %s" % (
            target, connection.target))
        if connection.context:
            connection.context.handle_kick(target)
    def _handle_every_raw_message(self, _connection, event):
        "Log all messages when in watcher mode."
        if self.logfile:
            with open(self.logfile, "ab") as logfp:
                message = u"%03f|%s|%s\n" % \
                          (time.time(), event.source, event.arguments[0])
                logfp.write(message.encode('utf-8'))

    def pending(self):
        "Do we have any pending message traffic?"
        return [k for (k, v) in self.servers.items() if v.pending()]

    def _parse_request(self, line):
        "Request-parsing helper for the handle() method"
        request = json.loads(line.strip())
        if not isinstance(request, dict):
            raise InvalidRequest(
                "request is not a JSON dictionary: %r" % request)
        if "to" not in request or "privmsg" not in request:
            raise InvalidRequest(
                "malformed request - 'to' or 'privmsg' missing: %r" % request)
        channels = request['to']
        message = request['privmsg']
        if not isinstance(channels, (list, UNICODE_TYPE)):
            raise InvalidRequest(
                "malformed request - unexpected channel type: %r" % channels)
        if not isinstance(message, UNICODE_TYPE):
            raise InvalidRequest(
                "malformed request - unexpected message type: %r" % message)
        if not isinstance(channels, list):
            channels = [channels]
        targets = []
        for url in channels:
            try:
                if not isinstance(url, UNICODE_TYPE):
                    raise InvalidRequest(
                        "malformed request - URL has unexpected type: %r" %
                        url)
                target = Target(url)
                target.validate()
            except InvalidRequest as e:
                LOG.error("irkerd: " + UNICODE_TYPE(e))
            else:
                targets.append(target)
        return (targets, message)

    def handle(self, line, quit_after=False):
        "Perform a JSON relay request."
        try:
            targets, message = self._parse_request(line=line)
            for target in targets:
                if target.server() not in self.servers:
                    self.servers[target.server()] = Dispatcher(
                        self, target=target, **self.kwargs)
                self.servers[target.server()].dispatch(
                    target.channel, message, target.key, quit_after=quit_after)
                # GC dispatchers with no active connections
                servernames = self.servers.keys()
                for servername in servernames:
                    if not self.servers[servername].live():
                        del self.servers[servername]
                    # If we might be pushing a resource limit even
                    # after garbage collection, remove a session.  The
                    # goal here is to head off DoS attacks that aim at
                    # exhausting thread space or file descriptors.
                    # The cost is that attempts to DoS this service
                    # will cause lots of join/leave spam as we
                    # scavenge old channels after connecting to new
                    # ones. The particular method used for selecting a
                    # session to be terminated doesn't matter much; we
                    # choose the one longest idle on the assumption
                    # that message activity is likely to be clumpy.
                    if len(self.servers) >= CONNECTION_MAX:
                        oldest = min(
                            self.servers.keys(),
                            key=lambda name: self.servers[name].last_xmit())
                        del self.servers[oldest]
        except InvalidRequest as e:
            LOG.error("irkerd: " + UNICODE_TYPE(e))
        except ValueError:
            LOG.error("irkerd: " + "can't recognize JSON on input: %r" % line)
        except RuntimeError:
            LOG.error("irkerd: " + "wildly malformed JSON blew the parser stack.")

class IrkerTCPHandler(socketserver.StreamRequestHandler):
    def handle(self):
        while True:
            line = self.rfile.readline()
            if not line:
                break
            if not isinstance(line, UNICODE_TYPE):
                line = UNICODE_TYPE(line, 'utf-8')
            irker.handle(line=line.strip())

class IrkerUDPHandler(socketserver.BaseRequestHandler):
    def handle(self):
        line = self.request[0].strip()
        #socket = self.request[1]
        if not isinstance(line, UNICODE_TYPE):
            line = UNICODE_TYPE(line, 'utf-8')
        irker.handle(line=line.strip())

def in_background():
    "Is this process running in background?"
    try:
        return os.getpgrp() != os.tcgetpgrp(1)
    except OSError:
        return True

if __name__ == '__main__':
    parser = argparse.ArgumentParser(
        description=__doc__.strip().splitlines()[0])
    parser.add_argument(
        '-c', '--ca-file', metavar='PATH',
        help='file of trusted certificates for SSL/TLS')
    parser.add_argument(
        '-e', '--cert-file', metavar='PATH',
        help='pem file used to authenticate to the server')
    parser.add_argument(
        '-d', '--log-level', metavar='LEVEL', choices=LOG_LEVELS,
        help='how much to log to the log file (one of %(choices)s)')
    parser.add_argument(
        '-H', '--host', metavar='ADDRESS', default=HOST,
        help='IP address to listen on')
    parser.add_argument(
        '-l', '--log-file', metavar='PATH',
        help='file for saving captured message traffic')
    parser.add_argument(
        '-n', '--nick', metavar='NAME', default='irker%03d',
        help="nickname (optionally with a '%%.*d' server connection marker)")
    parser.add_argument(
        '-p', '--password', metavar='PASSWORD',
        help='NickServ password')
    parser.add_argument(
        '-i', '--immediate', metavar='IRC-URL',
        help=(
            'send a single message to IRC-URL and exit.  The message is the '
            'first positional argument.'))
    parser.add_argument(
        '-V', '--version', action='version',
        version='%(prog)s {0}'.format(version))
    parser.add_argument(
        'message', metavar='MESSAGE', nargs='?',
        help='message for --immediate mode')
    args = parser.parse_args()

    if not args.log_file and in_background():
        # The Linux, Mac, and FreeBSD values of the logging device.
        logdev = [x for x in ('/dev/log', '/var/run/syslog', '/var/run/log')
                  if os.path.exists(x) and not os.path.isdir(x)]
        if len(logdev) != 1:
            sys.stderr.write("can't initialize log device, bailing out!\n")
            raise SystemExit(1)
        # There's a case for falling back to address = ('localhost', 514)
        # But some systems (including OS X) disable this for security reasons.
        handler = logging.handlers.SysLogHandler(address=logdev[0],
                                                 facility='daemon')
    else:
        handler = logging.StreamHandler()

    LOG.addHandler(handler)
    if args.log_level:
        log_level = getattr(logging, args.log_level.upper())
        LOG.setLevel(log_level)

    irker = Irker(
        logfile=args.log_file,
        nick_template=args.nick,
        nick_needs_number=re.search('%.*d', args.nick),
        password=args.password,
        cafile=args.ca_file,
        certfile=args.cert_file,
        )
    LOG.info("irkerd version %s" % version)
    if args.immediate:
        if not args.message:
            # We want newline to become '\n' and tab to become '\t';
            # the JSON decoder will undo these transformations.
            # This will also encode backslash, backspace, formfeed,
            # and high-half characters, which might produce unexpected
            # results on output.
            args.message = sys.stdin.read().encode("string_escape")
        irker.irc.add_event_handler("quit", lambda _c, _e: sys.exit(0))
        irker.handle('{"to":"%s","privmsg":"%s"}' % (
            args.immediate, args.message), quit_after=True)
        irker.irc.spin()
    else:
        if args.message:
            LOG.error(
                'irkerd: message argument given (%r), but --immediate not set' % (
                args.message))
            raise SystemExit(1)
        irker.thread_launch()
        try:
            tcpserver = socketserver.TCPServer((args.host, PORT), IrkerTCPHandler)
            udpserver = socketserver.UDPServer((args.host, PORT), IrkerUDPHandler)
            for server in [tcpserver, udpserver]:
                server = threading.Thread(target=server.serve_forever)
                server.setDaemon(True)
                server.start()
            try:
                signal.pause()
            except KeyboardInterrupt:
                raise SystemExit(1)
        except socket.error as e:
            LOG.error("irkerd: server launch failed: %r\n" % e)

# end
