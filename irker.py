#!/usr/bin/env python
"""
irker - a simple IRC multiplexer daemon

Takes JSON objects of the form {'channel':<channel-url>, 'privmsg':<text>}
and relays messages to IRC channels.

Run this as a daemon in order to maintain stateful connections to IRC
servers; this will allow it to respond to server pings and minimize
join/leave traffic.

Requires Python 2.6.

TO-DO: some servers have a limit of 20 channels per server connection.
TO-DO: Register the port?
TO-DO: Multiple irkers could try to use the same nick
"""
# These things might need tuning

HOST = "localhost"
PORT = 4747

TTL = (3 * 60 * 60)	# Connection time to live in seconds

# No user-serviceable parts below this line

import os, sys, json, irclib, exceptions, getopt, urlparse, time
import threading, Queue, SocketServer

class SessionException(exceptions.Exception):
    def __init__(self, message):
        exceptions.Exception.__init__(self)
        self.message = message

class Session():
    "IRC session and message queue processing."
    count = 0
    def __init__(self, irker, url):
        self.irker = irker
        self.url = url
        self.server = None
        # Server connection setup
        parsed = urlparse.urlparse(url)
        host, sep, port = parsed.netloc.partition(':')
        if not port:
            port = 6667
        self.servername = host
        self.channel = parsed.path.lstrip('/')
        self.port = int(port)
        Session.count += 1
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
                self.server = self.irker.irc.server()
                self.irker.debug(1, "TTL bump (connection) at %s" % time.asctime())
                self.last_active = time.time()
                self.server.connect(self.servername, self.port, self.name())
            elif self.queue.empty():
                if time.time() > self.last_active + TTL:
                    self.irker.debug(1, "timing out inactive connection at %s" % time.asctime())
                    self.server.part("#" + self.channel)
                    self.server = None
                    break
            else:
                message = self.queue.get()
                self.server.join("#" + self.channel)
                self.server.privmsg("#" + self.channel, message)
                self.last_active = time.time()
                self.irker.debug(1, "TTL bump (transmission) at %s" % time.asctime())
                self.queue.task_done()
    def name(self):
        "Generate a unique name for this session."
        return "irker%03d" % Session.count
    def await(self):
        "Block until processing of all queued messages is done."
        self.queue.join()

class Irker:
    "Persistent IRC multiplexer."
    def __init__(self, debuglevel=0):
        self.debuglevel = debuglevel
        self.irc = irclib.IRC(debuglevel=self.debuglevel-1)
        thread = threading.Thread(target=self.irc.process_forever)
        self.irc._thread = thread
        thread.daemon = True
        thread.start()
        self.sessions = {}
    def logerr(self, errmsg):
        "Log a processing error."
        sys.stderr.write("irker: " + errmsg + "\n")
    def debug(self, level, errmsg):
        "Debugging information."
        if self.debuglevel >= level:
            sys.stderr.write("irker[%d]: %s\n" % (self.debuglevel, errmsg))
    def handle(self, line):
        "Perform a JSON relay request."
        try:
            request = json.loads(line.strip())
            if "channel" not in request or "privmsg" not in request:
                self.logerr("ill-formed reqest")
            else:
                channel = request['channel']
                message = request['privmsg']
                if channel not in self.sessions:
                    self.sessions[channel] = Session(self, channel)
                self.sessions[channel].enqueue(message)
        except ValueError:
            self.logerr("can't recognize JSON on input.")
    def terminate(self):
        "Ship all pending messages before terminating."
        for session in self.sessions.values():
            session.await()

class MyTCPHandler(SocketServer.StreamRequestHandler):
    def handle(self):
        while True:
            irker.handle(self.rfile.readline().strip())

if __name__ == '__main__':
    host = HOST
    port = PORT
    debuglevel = 0
    (options, arguments) = getopt.getopt(sys.argv[1:], "d:p:")
    for (opt, val) in options:
        if opt == '-d':
            debuglevel = int(val)
        elif opt == '-p':
            port = int(val)
    irker = Irker(debuglevel=debuglevel)
    server = SocketServer.TCPServer((host, port), MyTCPHandler)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass

# end
