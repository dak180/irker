#!/usr/bin/env python
# Copyright (c) 2012 Eric S. Raymond <esr@thyrsus.com>
# Distributed under BSD terms.
#
# This script contains porcelain and porcelain byproducts.
# Requires Python 2.6, or 2.4 with the 2.6 json library installed.
#
# usage: git-irkbot.py [-V] [-n]
#
# This script is meant to be run in a post-commit hook.  Try it with
# -n to see the notification dumped to stdout and verify that it looks
# sane. With -V it dumps its version and exits.
#
# git configuration variables affecting this script:
#
# irker.project = name of the project
# irker.channels = list of IRC URLs corresponding to channels
# irker.repo = name of the project repo for gitweb/cgit purposes
# irker.revformat = format in which the revision is shown
# irker.server = location of the irker server to use for relaying
# irker.tcp = use TCP/IP if true, otherwise UDP
#
# irker.channels defaults to a project channel on freenode, and #commits
# irker.project defaults to the directory name of the repository toplevel.
# irker.repo defaults to irker.project lowercased.
# irker.tcp defaults to False
#
# This means that in the normal case you need not do any configuration at all,
# but setting the project name will speed it up slightly.
#
# The revformat variable may have the following values
# raw -> full hex ID of commit
# short -> first 12 chars of hex ID
# describe = -> describe relative to last tag, falling back to short
# The default is 'describe'.

# The default location of the irker proxy, if the project configuration
# does not override it.
default_irker_host = "localhost"
IRKER_PORT = 6659

# Changeset URL prefix for your repo: when the commit ID is appended
# to this, it should point at a CGI that will display the commit
# through gitweb or something similar. The defaults will probably
# work if you have a typical gitweb/cgit setup.
#
#urlprefix = "http://%(host)s/cgi-bin/gitweb.cgi?p=%(repo)s;a=commit;h="
urlprefix = "http://%(host)s/cgi-bin/cgit.cgi/%(repo)s/commit/?id="

# The service used to turn your gitwebbish URL into a tinyurl so it
# will take up less space on the IRC notification line.
tinyifier = "http://tinyurl.com/api-create.php?url="

# The template used to generate notifications.  You can make
# visible changes to the IRC-bot notification lines by hacking this.
#
# ${project}: ${author} ${repo}:${branch} * ${rev} / ${files}: ${logmsg} ${url}
template = '%(project)s: %(author)s %(repo)s:%(branch)s * %(rev)s / %(files)s: %(logmsg)s %(url)s'

#
# No user-serviceable parts below this line:
#

import os, sys, commands, socket, urllib, json

version = "1.0"

def do(command):
    return commands.getstatusoutput(command)[1]

class GitExtractor:
    "Metadata extraction for the git version control system."
    def __init__(self, project=None):
        # Get all global config variables
        self.revformat = do("git config --get irker.revformat")
        self.project = project or do("git config --get irker.project")
        self.repo = do("git config --get irker.repo")
        self.server = do("git config --get irker.server")
        self.channels = do("git config --get irker.channels")
        self.tcp = do("git config --get irker.tcp")
        # The project variable defaults to the name of the repository toplevel. 
        if not self.project:
            here = os.getcwd()
            while True:
                if os.path.exists(os.path.join(here, ".git")):
                    self.project = os.path.basename(here)
                    break
                elif here == '/':
                    sys.stderr.write("git-irkbot.py: no .git below root!\n")
                    sys.exit(1)
                here = os.path.dirname(here)
        if not self.repo:
            self.repo = self.project.lower()
        self.host = socket.getfqdn()            
        # Revision level
        self.refname = do("git symbolic-ref HEAD 2>/dev/null")
        self.commit = do("git rev-parse HEAD")
        # Try to tinyfy a reference to a web view for this commit.
        try:
            self.url = open(urllib.urlretrieve(tinyifier + urlprefix + self.commit)[0]).read()
        except:
            self.url = urlprefix + self.commit

        self.branch = os.path.basename(self.refname)

        # Compute a description for the revision
        if self.revformat == 'raw':
            self.rev = self.commit
        elif self.revformat == 'short':
            self.rev = ''
        else: # self.revformat == 'describe'
            self.rev = do("git describe %s 2>/dev/null" % self.commit)
        if not self.rev:
            self.rev = self.commit[:12]

        # Extract the meta-information for the commit
        self.files = do("git diff-tree -r --name-only '"+ self.commit +"' | sed -e '1d' -e 's-.*-&-'")
        metainfo = do("git log -1 '--pretty=format:%an <%ae>%n%s' " + self.commit)
        (self.author, self.logmsg) = metainfo.split("\n")
        # This discards the part of the author's address after @.
        # Might be be nice to ship the full email address, if not
        # for spammers' address harvesters - getting this wrong
        # would make the freenode #commits channel into harvester heaven.
        self.author = self.author.replace("<", "").split("@")[0].split()[-1]

if __name__ == "__main__":
    import getopt

    try:
        (options, arguments) = getopt.getopt(sys.argv[1:], "np:V")
    except getopt.GetoptError, msg:
        print "git-irkbot.py: " + str(msg)
        raise SystemExit, 1

    notify = True
    project = None
    channels = ""
    for (switch, val) in options:
        if switch == '-p':
            project = val
        elif switch == '-n':
            notify = False
        elif switch == '-V':
            print "git-irkbot.py: version", version
            sys.exit(0)

    # Someday we'll have extractors for several version-control systems
    extractor = GitExtractor(project)

    # By default, the channel list includes the freenode #commits list 
    if not extractor.channels:
        extractor.channels = "irc://chat.freenode.net/%s,irc://chat.freenode.net/#commits" % extractor.project

    urlprefix = urlprefix % extractor.__dict__

    privmsg = template % extractor.__dict__
    channel_list = extractor.channels.split(",")
    structure = {"to":channel_list, "privmsg":privmsg}
    message = json.dumps(structure)
    if not notify:
        print message
    else:
        try:
            if extractor.tcp:
                try:
                    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
                    sock.connect((extractor.server or default_irker_host, IRKER_PORT))
                    sock.sendall(message + "\n")
                finally:
                    sock.close()
            else:
                try:
                    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
                    sock.sendto(message + "\n", (extractor.server or default_irker_host, IRKER_PORT))
                finally:
                    sock.close()
        except socket.error, e:
            sys.stderr.write("%s\n" % e)

#End
