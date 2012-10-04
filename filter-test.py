#!/usr/bin/env python
#
# Test hook to launch an irker instance (if it doesn't already exist)
# just before shipping the notification. We start it in in another terminal
# so you can watch the debug messages. Probably only of interest only to
# developers
#
# To use it, set up irkerhook.py to file on each commit.
# Then set the filtercmd variable in your repo config as follows:
# 
# [irker]
# 	filtercmd = filter-test.py
#
# This is rather antisocial - imagine thousands of irkerds holding open
# connections to IRCDs.  It's better to go through an instance running
# at your forge or set up for shared use by your intranet administrator.

import os, sys, json, subprocess, time
metadata = json.loads(sys.argv[1])

ps = subprocess.Popen("ps -U %s uh" % os.getenv("LOGNAME"),
                      shell=True,
                      stdout=subprocess.PIPE)
data = ps.stdout.read()
irkerd_count = len([x for x in data.split("\n") if x.find("irkerd") != -1])

if irkerd_count:
    print "Using running irkerd..."
else:
    print "Launching new irkerd..."
    os.system("gnome-terminal --title 'irkerd' -e 'irkerd -d 2' &")

time.sleep(0.1)	# Avoid a race condition

print json.dumps(metadata)
# end
