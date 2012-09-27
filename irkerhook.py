#!/usr/bin/env python
# Copyright (c) 2012 Eric S. Raymond <esr@thyrsus.com>
# Distributed under BSD terms.
#
# This script contains git porcelain and porcelain byproducts.
# Requires Python 2.6, or 2.4 with the 2.6 json library installed.
#
# usage: irkerhook.py [-V] [-n]
#
# This script is meant to be run in a post-commit hook.  Try it with
# -n to see the notification dumped to stdout and verify that it looks
# sane. With -V it dumps its version and exits.
#
# Currently works for svn and git.  For svn you must call it as follows:
#
# irkerhook.py type=svn repository=REPO-PATH commit=REVISION channels=CHANNELS server=SERVER
#
# REPO-PATH must be the absolute path of the SVN repository (first
# argument of Subversion post-commit).  REVISION must be the Subversion numeric
# commit level (second argument of Subversion post-commit). CHANNELS must
# be a string containing either an IRC URL or comma-separated list of same.
# SERVER must be an irker host.
#
# For git, you can noormally call this script without arguments; it
# will deduce most of what it needs from git configuration variables
# (and the project value from where it is in the file system.
# Configuration variables are as follows.
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
#
# Any of these variables can be overridden with a command-line argument that
# is a key=value pair. For example "project=foobar" will force the project
# name to foobar, regardless of what the git configuration says.
#
# Other configuration changes you may want to make are to:
# urlprefix: the current version should work for viewcvs or gitweb
# installations, but will require modification for other systems.
# tinyfier: If your project maintains its own url-shrinking service

# The default location of the irker proxy, if the project configuration
# does not override it.
default_server = "localhost"
IRKER_PORT = 6659

# The service used to turn your gitwebbish URL into a tinyurl so it
# will take up less space on the IRC notification line.
tinyifier = "http://tinyurl.com/api-create.php?url="

#
# No user-serviceable parts below this line:
#

import os, sys, commands, socket, urllib, json

version = "1.0"

def do(command):
    return commands.getstatusoutput(command)[1]

def urlify(extractor, commit):
    prefix = urlprefix % extractor.__dict__
    # Try to tinyfy a reference to a web view for this commit.
    try:
        url = open(urllib.urlretrieve(tinyifier + prefix + commit)[0]).read()
    except:
        url = prefix + commit
    return url

class GitExtractor:
    "Metadata extraction for the git version control system."
    def __init__(self, project=None):
        # Get all global config variables
        self.project = project or do("git config --get irker.project")
        self.repo = do("git config --get irker.repo")
        self.server = do("git config --get irker.server")
        self.channels = do("git config --get irker.channels")
        self.tcp = do("git config --bool --get irker.tcp")
        # This one is git-specific
        self.revformat = do("git config --get irker.revformat")
        # The project variable defaults to the name of the repository toplevel.
        if not self.project:
            bare = do("git config --bool --get core.bare")
            if bare.lower() == "true":
                keyfile = "HEAD"
            else:
                keyfile = ".git/HEAD"
            here = os.getcwd()
            while True:
                if os.path.exists(os.path.join(here, keyfile)):
                    self.project = os.path.basename(here)
                    break
                elif here == '/':
                    sys.stderr.write("irkerhook.py: no git repo below root!\n")
                    sys.exit(1)
                here = os.path.dirname(here)
        # Revision level
        self.refname = do("git symbolic-ref HEAD 2>/dev/null")
        self.commit = do("git rev-parse HEAD")

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

class SvnExtractor:
    "Metadata extraction for the svn version control system."
    def __init__(self, arguments):
        self.commit = None
        # Some things we need to have before metadata queries will work
        for tok in arguments:
            if tok.startswith("repository="):
                self.repository = tok[11:]
            elif tok.startswith("commit="):
                self.commit = tok[7:]
        self.project = os.path.basename(self.repository)
        self.repo = None
        self.server = None
        self.channels = None
        self.tcp = True
        self.author = self.svnlook("author")
        self.files = self.svnlook("dirs-changed")
        self.logmsg = self.svnlook("log")
        self.rev = "r{0}".format(self.commit)
    def svnlook(self, info):
        return do("svnlook {0} {1} --revision {2}".format(info, self.repository, self.commit))

if __name__ == "__main__":
    import getopt

    try:
        (options, arguments) = getopt.getopt(sys.argv[1:], "nV")
    except getopt.GetoptError, msg:
        print "irkerhook.py: " + str(msg)
        raise SystemExit, 1

    vcs = "git"
    notify = True
    channels = ""
    commit = ""
    for (switch, val) in options:
        if switch == '-n':
            notify = False
        elif switch == '-V':
            print "irkerhook.py: version", version
            sys.exit(0)

    # Force the type if not git, also make globals settable
    for tok in arguments:
        if tok.startswith("type="):
            vcs = tok[5:]
        elif tok.startswith("tinyfier="):
            tinyfier = tok[9:]

    # Someday we'll have extractors for several version-control systems
    if vcs == "git":
        extractor = GitExtractor()
    else:
        extractor = SvnExtractor(arguments)

    # Changeset URL prefix for your repo: when the commit ID is appended
    # to this, it should point at a CGI that will display the commit
    # through gitweb or something similar. The defaults will probably
    # work if you have a typical gitweb/cgit setup.
    #
    #urlprefix = "http://%(host)s/cgi-bin/gitweb.cgi?p=%(repo)s;a=commit;h="
    if vcs == "svn":
        urlprefix = "http://%(host)s/viewcvs/%(repo)s?view=revision&revision="
    else:
        urlprefix = "http://%(host)s/cgi-bin/cgit.cgi/%(repo)s/commit/?id="
    # The template used to generate notifications.  You can make
    # visible changes to the IRC-bot notification lines by hacking this.
    #
    # ${project}: ${author} ${repo}:${branch} * ${rev} / ${files}: ${logmsg} ${url}
    if vcs == "svn":
        template = '%(project)s: %(author)s %(repo)s * %(rev)s / %(files)s: %(logmsg)s %(url)s'
    else:
        template = '%(project)s: %(author)s %(repo)s:%(branch)s * %(rev)s / %(files)s: %(logmsg)s %(url)s'

    # Make command-line overrides possible.
    # Each argument of the form <key>=<value> can override the
    # <key> member of the extractor class. 
    booleans = ["tcp"]
    for tok in arguments:
        for key in extractor.__dict__:
            if tok.startswith(key + "="):
                val = tok[len(key)+1:]
                if key in booleans:
                    if val.lower() == "true":
                        setattr(extractor, key, True)
                    elif val.lower() == "false":
                        setattr(extractor, key, False)
                else:
                    setattr(extractor, key, val)

    # By default, the channel list includes the freenode #commits list 
    if not extractor.channels:
        extractor.channels = "irc://chat.freenode.net/%s,irc://chat.freenode.net/#commits" % extractor.project
    # Other defaults get set here
    if not extractor.repo:
        extractor.repo = extractor.project.lower()
    extractor.host = socket.getfqdn()            
    extractor.url = urlify(extractor, extractor.commit)

    if not extractor.project:
        sys.stderr.write("irkerhook.py: no project name set!\n")
        sys.exit(1)

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
                    sock.connect((extractor.server or default_server, IRKER_PORT))
                    sock.sendall(message + "\n")
                finally:
                    sock.close()
            else:
                try:
                    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
                    sock.sendto(message + "\n", (extractor.server or default_server, IRKER_PORT))
                finally:
                    sock.close()
        except socket.error, e:
            sys.stderr.write("%s\n" % e)

#End
