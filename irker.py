#!/usr/bin/env python
"""
irker - a simple IRC multiplexer daemon

Listens for JSON objects of the form {'to':<irc-url>, 'privmsg':<text>}
and relays messages to IRC channels. Each request must be followed by
a newline.

The <text> must be a string.  The value of the 'to' attribute can be a
string containing an IRC URL (e.g. 'irc://chat.freenet.net/botwar') or
a list of such strings; in the latter case the message is broadcast to
all listed channels.  Note that the channel portion of the URL will
*not* have a leading '#' unless the channel name itself does.

Options: -p sets the listening port. The -V option prints the program
version and exits.

Requires Python 2.6 and the irc.client library at version >= 2.0.2: see

http://sourceforge.net/projects/python-irclib

TO-DO: set deaf usermode.
"""
# These things might need tuning

HOST = "localhost"
PORT = 4747			# Overridden by -p option

NAMESTYLE = "irker%03d"		# IRC nick template - must contain '%d'
XMIT_TTL = (3 * 60 * 60)	# Time to live, seconds from last transmit
PING_TTL = (15 * 60)		# Time to live, seconds from last PING
CONNECT_MAX = 18		# Max channels open per socket (freenet limit)

# No user-serviceable parts below this line

import sys, json, exceptions, getopt, urlparse, time
import threading, Queue, SocketServer
import irc.client, logging

version = "1.0"

# Sketch of implementation:
#
# One Irker object manages multiple IRC sessions.  It holds a map of
# Dispatcher objects, one per (server, port) combination, which are
# responsible for routing messages to one of any bumber of Connection
# objects that do the actual socket conversation.  The reason for
# the Dispatcher layer is that IRC daemons limit the number of channels
# a client (that is, from the daemon's point of view, a socket) can be
# joined to.
#
# Connections are timed out and removed when either they haven't seen a
# PING for a while (indicating that the server may be stalled or down)
# or there has been no message traffic to them for a while.
#
# There are multiple threads. One accepts incoming traffic from all servers.
# Each Connection also has a consumer thread and a thread-safe message queue.
# The program main appends messages to queues as JSON requests are received;
# the consumer threads try to ship them to servers.  When a socket write
# stalls, it only blocks an individual consumer thread; if it stalls long
# enough, the session will be timed out.
#
# Message delivery is thus not reliable in the face of network stalls, but
# this was considered acceptable because IRC (notoriously) has the same
# problem - there is little point in delivery to a relay that is down or
# unreliable.

class Connection(irc.client.ServerConnection):
    def __init__(self, irker, servername, port):
        self.irker = irker
        self.servername = servername
        self.port = port
        self.connection = None
        self.nick_trial = 1
        self.last_xmit = time.time()
        self.last_ping = time.time()
        self.channels_joined = []
        # The consumer thread
        self.queue = Queue.Queue()
        self.thread = threading.Thread(target=self.dequeue)
        self.thread.start()
    def nickname(self, n):
        "Return a name for the nth server connection."
        return (NAMESTYLE % n)
    def handle_ping(self):
        "Register the fact that the server has pinged this connection."
        self.last_ping = time.time()
    def handle_welcome(self):
        "The server says we're OK, with a non-conflicting nick."
        self.nick_accepted = True
        self.irker.debug(1, "nick %s accepted" % self.nickname(self.nick_trial))
    def handle_badnick(self):
        "The server says our nick has a conflict."
        self.irker.debug(1, "nick %s rejected" % self.nickname(self.nick_trial))
        self.nick_trial += 1
        self.nick(self.nickname(self.nick_trial))
    def enqueue(self, channel, message):
        "Enque a message for transmission."
        self.queue.put((channel, message))
    def dequeue(self):
        "Try to ship pending messages from the queue."
        while True:
            # We want to be kind to the IRC servers and not hold unused
            # sockets open forever, so they have a time-to-live.  The
            # loop is coded this particular way so that we can drop
            # the actual server connection when its time-to-live
            # expires, then reconnect and resume transmission if the
            # queue fills up again.
            if not self.connection:
                self.connection = self.irker.irc.server()
                self.connection.context = self
                self.nick_trial = 1
                self.channels_joined = []
                self.connection.connect(self.servername,
                                        self.port,
                                        nickname=self.nickname(self.nick_trial),
                                        username="irker",
                                        ircname="irker relaying client")
                self.nick_accepted = False
                self.irker.debug(1, "XMIT_TTL bump (%s connection) at %s" % (self.servername, time.asctime()))
                self.last_xmit = time.time()
            elif self.queue.empty():
                now = time.time()
                if now > self.last_xmit + XMIT_TTL \
                       or now > self.last_ping + PING_TTL:
                    self.irker.debug(1, "timing out inactive connection to %s at %s" % (self.servername, time.asctime()))
                    self.connection.context = None
                    self.connection.close()
                    self.connection = None
                    break
            elif self.nick_accepted:
                (channel, message) = self.queue.get()
                if channel not in self.channels_joined:
                    self.connection.join("#" + channel)
                    self.channels_joined.append(channel)
                self.connection.privmsg("#" + channel, message)
                self.last_xmit = time.time()
                self.irker.debug(1, "XMIT_TTL bump (%s transmission) at %s" % (self.servername, time.asctime()))
                self.queue.task_done()
    def timed_out(self):
        "Predicate: returns True if the connection has timed out."
        # Important invariant: enqueue() is only called synchronously in the
        # main thread.  Thus, once the consumer thread empties the queue and
        # declared timeout, this predicate is thread-stable.
        return self.connection == None

class Target():
    "Represent a transmission target."
    def __init__(self, url):
        parsed = urlparse.urlparse(url)
        irchost, _, ircport = parsed.netloc.partition(':')
        if not ircport:
            ircport = 6667
        self.servername = irchost
        self.channel = parsed.path.lstrip('/')
        self.port = int(ircport)
    def server(self):
        "Return a hashable tuple representing the destination server."
        return (self.servername, self.port)

class Dispatcher:
    "Dispatch messages for a particular server-port combination."
    # FIXME: Implement a nontivial policy that respects CONNECT_MAX.
    def __init__(self, irker, servername, port):
        self.irker = irker
        self.servername = servername
        self.port = port
        self.connection = Connection(self.irker, servername, port)
    def dispatch(self, channel, message):
        self.connection.enqueue(channel, message)

class Irker:
    "Persistent IRC multiplexer."
    def __init__(self, debuglevel=0):
        self.debuglevel = debuglevel
        self.irc = irc.client.IRC()
        self.irc.add_global_handler("ping", self._handle_ping)
        self.irc.add_global_handler("welcome", self._handle_welcome)
        self.irc.add_global_handler("erroneusnickname", self._handle_badnick)
        self.irc.add_global_handler("nicknameinuse", self._handle_badnick)
        self.irc.add_global_handler("nickcollision", self._handle_badnick)
        self.irc.add_global_handler("unavailresource", self._handle_badnick)
        #self.irc.add_global_handler("featurelist", self._handle_features)
        thread = threading.Thread(target=self.irc.process_forever)
        self.irc._thread = thread
        thread.start()
        self.ircds = {}
    def logerr(self, errmsg):
        "Log a processing error."
        sys.stderr.write("irker: " + errmsg + "\n")
    def debug(self, level, errmsg):
        "Debugging information."
        if self.debuglevel >= level:
            sys.stderr.write("irker: %s\n" % errmsg)
    def _handle_ping(self, connection, event):
        "PING arrived, bump the last-received time for the connection."
        if connection.context:
            connection.context.handle_ping()
    def _handle_welcome(self, connection, event):
        "Welcome arrived, nick accepted for this connection."
        if connection.context:
            connection.context.handle_welcome()
    def _handle_badnick(self, connection, event):
        "Nick not accepted for this connection."
        if connection.context:
            connection.context.handle_badnick()
    def handle(self, line):
        "Perform a JSON relay request."
        try:
            request = json.loads(line.strip())
            if "to" not in request or "privmsg" not in request:
                self.logerr("malformed reqest - 'to' or 'privmsg' missing: %s" % repr(request))
            else:
                channels = request['to']
                message = request['privmsg']
                if type(channels) not in (type([]), type(u"")) \
                       or type(message) != type(u""):
                    self.logerr("malformed request - unexpected types: %s" % repr(request))
                else:
                    if type(channels) == type(u""):
                        channels = [channels]
                    for url in channels:
                        if type(url) != type(u""):
                            self.logerr("malformed request - unexpected type: %s" % repr(request))
                        else:
                            target = Target(url)
                            if target.server() not in self.ircds:
                                self.ircds[target.server()] = Dispatcher(self, target.servername, target.port)
                            self.ircds[target.server()].dispatch(target.channel, message)
        except ValueError:
            self.logerr("can't recognize JSON on input: %s" % repr(line))

class IrkerTCPHandler(SocketServer.StreamRequestHandler):
    def handle(self):
        while True:
            irker.handle(self.rfile.readline().strip())

class IrkerUDPHandler(SocketServer.BaseRequestHandler):
    def handle(self):
        data = self.request[0].strip()
        #socket = self.request[1]
        irker.handle(data)

if __name__ == '__main__':
    srvhost = HOST
    srvport = PORT
    debuglvl = 0
    (options, arguments) = getopt.getopt(sys.argv[1:], "d:p:V")
    for (opt, val) in options:
        if opt == '-d':		# Enable debug/progress messages
            debuglvl = int(val)
            if debuglvl > 1:
                logging.basicConfig(level=logging.DEBUG)
        elif opt == '-p':	# Set the listening port
            port = int(val)
        elif opt == '-V':	# Emit version and exit
            sys.stdout.write("irker version %s\n" % version)
            sys.exit(0)
    irker = Irker(debuglevel=debuglvl)
    tcpserver = SocketServer.TCPServer((srvhost, srvport), IrkerTCPHandler)
    udpserver = SocketServer.UDPServer((srvhost, srvport), IrkerUDPHandler)
    threading.Thread(target=tcpserver.serve_forever).start()
    threading.Thread(target=udpserver.serve_forever).start()

# end
