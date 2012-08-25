#!/usr/bin/env python
"""
irker - a simple IRC multiplexer daemon

Takes JSON objects of the form {'channel':<channel-url>, 'message':<text>}
and relays to IRC channels.

"""
import os, sys, json, irclib

