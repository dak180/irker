#!/usr/bin/env python
"""
irker - a simple IRC multiplexer daemon

Takes JSON objects of the form {'channel':<channel-url>, 'message':<text>}
and relays to IRC channels.

"""
import os, sys, json, irclib, getopt

class Irker:
    "Persistent IRC multiplexer."
    def __init__(self):
        self.botpool = {}
    def logerr(self, errmsg):
        "Log a processing error."
        sys.stderr.write(errmsg)
    def run(self, ifp):
        "Accept JSON relay requests from specified stream."
        while True:
            inp = ifp.readline()
            if not inp:
                break
            try:
                request = json.loads(inp.strip())
            except ValueError:
                self.logerr("irker: can't recognize JSON on input.\n")
                break
            self.relay(request)
    def relay(self, request):
        print request

if __name__ == '__main__':
    irker = Irker()
    irker.run(sys.stdin)
