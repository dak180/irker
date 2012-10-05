#!/usr/bin/env python
#
# Test hook to launch an irker instance (if it doesn't already exist)
# just before shipping the notification. We start it in in another terminal
# so you can watch the debug messages. Intended to be used in the root
# directory of the irker repo. Probably only of interest only to irker
# developers
#
# To use this, set up irkerhook.py to fire on each commit.  Creating a
# .git/hooks/post-commit file containing the line "irkerhook.py"; be
# sure to make the opos-commit file executable.  Then set the
# filtercmd variable in your repo config as follows:
# 
# [irker]
# 	filtercmd = filter-test.py

import os, sys, json, subprocess, time
metadata = json.loads(sys.argv[1])

ps = subprocess.Popen("ps -U %s uh" % os.getenv("LOGNAME"),
                      shell=True,
                      stdout=subprocess.PIPE)
data = ps.stdout.read()
irkerd_count = len([x for x in data.split("\n") if x.find("irkerd") != -1])

if not irkerd_count:
    os.system("gnome-terminal --title 'irkerd' -e 'irkerd -d 2' &")

time.sleep(0.5)	# Avoid a race condition

print json.dumps(metadata)
# end
