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
# One Irker object manages multiple IRC sessions.  Each Session object
# corresponds to a destination IRC URL that the daemon has seen and handles
# traffic for one channel on one server.  There is never more than one
# Session per given (server, channel) pair.
#
# Multiple sessions to the same IRC server may share the same
# irc.client.ServerConnection object in order to cut down on open sockets,
# but because many servers enforce a limit on channels open per incoming
# socket, not *all* sessions on the same server necessarily do.
#
# Sessions are timed out and removed when either they haven't seen a
# PING for a while (indicating that the server may be stalled or down)
# or there has been no message traffic to them for a while.
#
# There are multiple threads. One accepts incoming traffic from all servers.
# Each Session also has a consumer thread and a thread-safe message queue.
# The program main appends messages to queues as JSON requests are received;
# the consumer threads try to ship them to servers.  When a socket write
# stalls, it only blocks an individual consumer thread; if it stalls long
# enough, the session will be timed out.
#
# Message delivery is thus not reliable in the face of network stalls, but
# this was considered acceptable because IRC (notoriously) has the same
# problem - there is little point in delivery to a relay that is down or
# unreliable.

class SessionException(exceptions.Exception):
    def __init__(self, message):
        exceptions.Exception.__init__(self)
        self.message = message

class Session():
    "IRC session and message queue processing."
    def __init__(self, irker, url):
        self.irker = irker
        self.url = url
        self.server = None
        self.last_xmit = time.time()
        self.last_ping = time.time()
        # Server connection setup
        parsed = urlparse.urlparse(url)
        irchost, _, ircport = parsed.netloc.partition(':')
        if not ircport:
            ircport = 6667
        self.servername = irchost
        self.channel = parsed.path.lstrip('/')
        self.port = int(ircport)
        # The consumer thread
        self.queue = Queue.Queue()
        self.thread = threading.Thread(target=self.dequeue)
        self.thread.start()
    def enqueue(self, message):
        "Enque a message for transmission."
        self.queue.put(message)
    def dequeue(self):
        "Try to ship pending messages from the queue."
        while True:
            # We want to be kind to the IRC servers and not hold unused
            # sockets open forever, so they have a time-to-live.  The
            # loop is coded this particular way so that we can drop
            # the actual server connection when its time-to-live
            # expires, then reconnect and resume transmission if the
            # queue fills up again.
            if not self.server:
                self.server = self.irker.open(self.servername,
                                                         self.port)
                self.irker.debug(1, "XMIT_TTL bump (connection) at %s" % time.asctime())
                self.last_xmit = time.time()
            elif self.queue.empty():
                now = time.time()
                if now > self.last_xmit + XMIT_TTL \
                       or now > self.last_ping + PING_TTL:
                    self.irker.debug(1, "timing out inactive connection at %s" % time.asctime())
                    self.irker.close(self.servername, self.port)
                    self.server = None
                    break
            elif self.server.nick_accepted:
                message = self.queue.get()
                if self.channel not in self.server.channels_joined:
                    self.server.join("#" + self.channel)
                    self.server.channels_joined.append(self.channel)
                self.server.privmsg("#" + self.channel, message)
                self.last_xmit = time.time()
                self.irker.debug(1, "XMIT_TTL bump (transmission) at %s" % time.asctime())
                self.queue.task_done()
    def terminate(self):
        "Terminate this session"
        self.server.quit("#" + self.channel)
        self.server.close()
    def await(self):
        "Block until processing of all queued messages is done."
        self.queue.join()

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
        self.sessions = {}
        self.countmap = {}
        self.servercount = 0
    def logerr(self, errmsg):
        "Log a processing error."
        sys.stderr.write("irker: " + errmsg + "\n")
    def debug(self, level, errmsg):
        "Debugging information."
        if self.debuglevel >= level:
            sys.stderr.write("irker: %s\n" % errmsg)
    def nickname(self, n):
        "Return a name for the nth server connection."
        return (NAMESTYLE % n)
    def open(self, servername, port):
        "Allocate a new server instance."
        if not (servername, port) in self.countmap:
            self.countmap[(servername, port)] = (CONNECT_MAX+1, None)
        count = self.countmap[(servername, port)][0]
        if count > CONNECT_MAX:
            self.servercount += 1
            newserver = self.irc.server()
            newserver.nick_trial = self.servercount
            newserver.channels_joined = []
            newserver.connect(servername,
                              port,
                              nickname=self.nickname(newserver.nick_trial),
                              username="irker",
                              ircname="irker relaying client")
            newserver.nick_accepted = False
            self.countmap[(servername, port)] = (1, newserver)
            self.debug(1, "new server connection %d opened for %s:%s" % \
                       (self.servercount, servername, port))
        else:
            self.debug(1, "reusing server connection for %s:%s" % \
                       (servername, port))
        return self.countmap[(servername, port)][1]
    def close(self, servername, port):
        "Release a server instance and all sessions using it."
        del self.countmap[(servername, port)]
        for val in self.sessions.values():
            if (val.servername, val.port) == (servername, port):
                self.sessions[val.url].terminate()
                del self.sessions[val.url]
    def _handle_ping(self, connection, event):
        "PING arrived, bump the last-received time for the connection."
        for (name, session) in self.sessions.items():
            if name == connection.server:
                session.last_ping = time.time()
    def _handle_welcome(self, connection, event):
        "Welcome arrived, nick accepted for this connection."
        connection.nick_accepted = True
        self.debug(1, "nick %s accepted" % self.nickname(connection.nick_trial))
    def _handle_badnick(self, connection, event):
        "Nick not accepted for this connection."
        self.debug(1, "nick %s rejected" % self.nickname(connection.nick_trial))
        connection.nick_trial += 1
        connection.nick(self.nickname(connection.nick_trial))
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
                            if url not in self.sessions:
                                self.sessions[url] = Session(self, url)
                            self.sessions[url].enqueue(message)
        except ValueError:
            self.logerr("can't recognize JSON on input: %s" % repr(line))
    def terminate(self):
        "Ship all pending messages before terminating."
        for session in self.sessions.values():
            session.await()

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
