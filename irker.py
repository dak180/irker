#!/usr/bin/env python
"""
irker - a simple IRC multiplexer daemon

Takes JSON objects of the form {'to':<irc-url>, 'privmsg':<text>}
and relays messages to IRC channels.

The <text> must be a string.  The value of the 'to' attribute can be a
string containing an IRC URL (e.g. 'irc://chat.freenet.net/botwar') or
a list of such strings; in the latter case the message is broadcast to
all listed channels.  Note that the channel portion of the URL will
*not* have a leading '#' unless the channel name itself does.

Message transmission is normally via UDP, optimizing for lowest
latency and network load by avoiding TCP connection setup time; the
cost is that delivery is not reliable in the face of packet loss.
The -t option changes this, telling the daemon to use TCP instead.

Other options: -p sets the listening port, -n sets the name suffix
for the nicks that irker uses.  The default suffix is derived from the
FQDN of the site on which irker is running; the intent is to avoid
nick collisions by instances running on different sites.

Requires Python 2.6 and the irc.client library: see

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

import sys, json, exceptions, getopt, urlparse, time, socket
import threading, Queue, SocketServer
import irc.client, logging

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
        host, _, port = parsed.netloc.partition(':')
        if not port:
            port = 6667
        self.servername = host
        self.channel = parsed.path.lstrip('/')
        self.port = int(port)
        # The consumer thread
        self.queue = Queue.Queue()
        self.thread = threading.Thread(target=self.dequeue)
        self.thread.daemon = True
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
                    self.irker.close(self.servername,
                                                 self.port)
                    self.server = None
                    break
            else:
                message = self.queue.get()
                self.server.join("#" + self.channel)
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
    def __init__(self, debuglevel=0, namesuffix=None):
        self.debuglevel = debuglevel
        self.namesuffix = namesuffix or socket.getfqdn().replace(".", "-")
        self.irc = irc.client.IRC()
        self.irc.add_global_handler("ping", lambda c, e: self._handle_ping(c,e))
        thread = threading.Thread(target=self.irc.process_forever)
        self.irc._thread = thread
        thread.daemon = True
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
            sys.stderr.write("irker[%d]: %s\n" % (self.debuglevel, errmsg))
    def nickname(self, n):
        "Return a name for the nth server connection."
        # The purpose of including the namme suffix (defaulting to the
        # host's FQDN) is to ensure that the nicks of bots managed by
        # instances running on different hosts can never collide.
        return (NAMESTYLE % n) + "-" + self.namesuffix
    def open(self, servername, port):
        "Allocate a new server instance."
        if not (servername, port) in self.countmap:
            self.countmap[(servername, port)] = (CONNECT_MAX+1, None)
        count = self.countmap[(servername, port)][0]
        if count > CONNECT_MAX:
            self.servercount += 1
            newserver = self.irc.server()
            newserver.connect(servername,
                              port,
                              self.nickname(self.servercount))
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
        for (name, server) in self.sessions.items():
            if name == connection.server:
                server.last_ping = time.time()
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
    host = HOST
    port = PORT
    namesuffix = None
    debuglevel = 0
    tcp = False
    (options, arguments) = getopt.getopt(sys.argv[1:], "d:p:n:t")
    for (opt, val) in options:
        if opt == '-d':		# Enable debug/progress messages
            debuglevel = int(val)
            if debuglevel > 1:
                logging.basicConfig(level=DEBUG)
        elif opt == '-p':	# Set the listening port
            port = int(val)
        elif opt == '-n':	# Set the name suffix for irker nicks
            namesuffix = val
        elif opt == '-t':	# Use TCP rather than UDP
            tcp = True
    irker = Irker(debuglevel=debuglevel, namesuffix=namesuffix)
    if tcp:
        server = SocketServer.TCPServer((host, port), IrkerTCPHandler)
    else:
        server = SocketServer.UDPServer((host, port), IrkerUDPHandler)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass

# end
