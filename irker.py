#!/usr/bin/env python
"""
irker - a simple IRC multiplexer daemon

Takes JSON objects of the form {'channel':<channel-url>, 'message':<text>}
and relays to IRC channels.

"""
import os, sys, json, irclib, getopt

class Session:
    "IRC session and message queue processing."
    def __init__(self, channel):
        self.channel = channel
        self.queue = []
    def enqueue(self, message):
        "Enque a message for transmission."
        self.queue.append(message)

class Irker:
    "Persistent IRC multiplexer."
    def __init__(self):
        self.sessions = {}
    def logerr(self, errmsg):
        "Log a processing error."
        sys.stderr.write("irker: " + errmsg + "\n")
    def run(self, ifp):
        "Accept JSON relay requests from specified stream."
        while True:
            inp = ifp.readline()
            if not inp:
                break
            try:
                request = json.loads(inp.strip())
            except ValueError:
                self.logerr("can't recognize JSON on input.")
                break
            self.relay(request)
    def relay(self, request):
        if "channel" not in request or "message" not in request:
            self.logerr("ill-formed reqest")
        else:
            channel = request['channel']
            message = request['message']
            if channel not in self.sessions:
                self.sessions[channel] = Session(channel)
            self.sessions[channel].enqueue(message)

if __name__ == '__main__':
    irker = Irker()
    irker.run(sys.stdin)
