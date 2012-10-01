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
# sane. With -V this script dumps its version and exits.
#
# See the irkerhook manual page in the distribution for a detailed
# explanation of how to configure this hook.

# The default location of the irker proxy, if the project configuration
# does not override it.
default_server = "localhost"
IRKER_PORT = 6659

# The default service used to turn your web-view URL into a tinyurl so it
# will take up less space on the IRC notification line.
default_tinyifier = "http://tinyurl.com/api-create.php?url="

# Map magic urlprefix values to actual URL prefixes.
urlprefixmap = {
    "viewcvs": "http://%(host)s/viewcvs/%(repo)s?view=revision&revision=",
    "gitweb": "http://%(host)s/cgi-bin/gitweb.cgi?p=%(repo)s;a=commit;h=",
    "cgit": "http://%(host)s/cgi-bin/cgit.cgi/%(repo)s/commit/?id=",
    }

# By default, the channel list includes the freenode #commits list 
default_channels = "irc://chat.freenode.net/%(project)s,irc://chat.freenode.net/#commits"

#
# No user-serviceable parts below this line:
#

import os, sys, commands, socket, urllib, json

version = "1.2"

def shellquote(s):
    return "'" + s.replace("'","'\\''") + "'"

def do(command):
    return commands.getstatusoutput(command)[1]

class GenericExtractor:
    "Generic class for encapsulating data from a VCS."
    def __init__(self, arguments):
        self.arguments = arguments
        self.project = None
        self.repo = None
        # These aren't really repo data but they belong here anyway...
        self.tcp = True
        self.tinyifier = default_tinyifier
        self.server = None
        self.channels = None
        self.maxchannels = 0
        self.template = None
        self.urlprefix = None
        self.host = socket.getfqdn()
        # Per-commit data begins
        self.author = None
        self.files = None
        self.logmsg = None
        self.rev = None
        self.color = False
        # Color highlighting is disabled by default.
        self.bold = self.green = self.blue = ""
        self.yellow = self.brown = self.reset = ""
    def activate_color(self):
        "IRC color codes."
        self.bold = '\x02'
        self.green = '\x033'
        self.blue = '\x032'
        self.yellow = '\x037'
        self.brown = '\x035'
        self.reset = '\x0F'
    def load_preferences(self, conf):
        "Load preferences from a file in the repository root."
        if not os.path.exists(conf):
            return
        ln = 0
        for line in open(conf):
            ln += 1
            if line.startswith("#") or not line.strip():
                continue
            elif line.count('=') != 1:
                sys.stderr.write('"%s", line %d: missing = in config line\n' \
                                 % (conf, ln))
                continue
            fields = line.split('=')
            if len(fields) != 2:
                sys.stderr.write('"%s", line %d: too many fields in config line\n' \
                                 % (conf, ln))
                continue
            variable = fields[0].strip()
            value = fields[1].strip()
            if value.lower() == "true":
                value = True
            elif value.lower() == "false":
                value = False
            # User cannot set maxchannels - only a command-line arg can do that.
            if variable == "maxchannels":
                return
            setattr(self, variable, value)
    def do_overrides(self):
        "Make command-line overrides possible."
        booleans = ["tcp", "color"]
        numerics = ["maxchannels"]
        for tok in self.arguments:
            for key in self.__dict__:
                if tok.startswith(key + "="):
                    val = tok[len(key)+1:]
                    if key in booleans:
                        if val.lower() == "true":
                            setattr(self, key, True)
                        elif val.lower() == "false":
                            setattr(self, key, False)
                    elif key in numerics:
                        setattr(self, key, int(val))
                    else:
                        setattr(self, key, val)
        if not self.project:
            sys.stderr.write("irkerhook.py: no project name set!\n")
            raise SystemExit, 1
        if not self.repo:
            self.repo = self.project.lower()
        if not self.channels:
            self.channels = default_channels % self.__dict__
        if self.urlprefix.lower() == "none":
            self.url = ""
        else:
            self.urlprefix = urlprefixmap.get(self.urlprefix, self.urlprefix) 
            prefix = self.urlprefix % self.__dict__
            try:
                webview = prefix + self.commit
                txt = open(urllib.urlretrieve(webview)[0]).read()
                if "404" in txt or "not found" in txt:
                    raise IOError
                try:
                    # Didn't get a retrieval error or 404 on the web
                    # view, so try to tinyify a reference to it.
                    self.url = open(urllib.urlretrieve(self.tinyifier + webview)[0]).read()
                except IOError:
                    self.url = webview
            except IOError:
                self.url = ""
        if self.color:
            self.activate_color()

class GitExtractor(GenericExtractor):
    "Metadata extraction for the git version control system."
    def __init__(self, arguments):
        GenericExtractor.__init__(self, arguments)
        # Get all global config variables
        self.project = do("git config --get irker.project")
        self.repo = do("git config --get irker.repo")
        self.server = do("git config --get irker.server")
        self.channels = do("git config --get irker.channels")
        self.tcp = do("git config --bool --get irker.tcp")
        self.template = '%(bold)s%(project)s:%(reset)s %(green)s%(author)s%(reset)s %(repo)s:%(yellow)s%(branch)s%(reset)s * %(bold)s%(rev)s%(reset)s / %(bold)s%(files)s%(reset)s: %(logmsg)s %(brown)s%(url)s%(reset)s'
        self.color = do("git config --bool --get irker.color")
        self.urlprefix = do("git config --get irker.urlprefix") or "gitweb"
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
            self.rev = do("git describe %s 2>/dev/null" % shellquote(self.commit))
        if not self.rev:
            self.rev = self.commit[:12]
        # Extract the meta-information for the commit
        self.files = do("git diff-tree -r --name-only " + shellquote(self.commit))
        self.files = " ".join(self.files.strip().split("\n")[1:])
        metainfo = do("git log -1 '--pretty=format:%an <%ae>%n%s' " + shellquote(self.commit))
        (self.author, self.logmsg) = metainfo.split("\n")
        # This discards the part of the author's address after @.
        # Might be be nice to ship the full email address, if not
        # for spammers' address harvesters - getting this wrong
        # would make the freenode #commits channel into harvester heaven.
        self.author = self.author.replace("<", "").split("@")[0].split()[-1]
        # Get overrides
        self.do_overrides()

class SvnExtractor(GenericExtractor):
    "Metadata extraction for the svn version control system."
    def __init__(self, arguments):
        GenericExtractor.__init__(self, arguments)
        self.commit = None
        # Some things we need to have before metadata queries will work
        for tok in arguments:
            if tok.startswith("repository="):
                self.repository = tok[11:]
            elif tok.startswith("commit="):
                self.commit = tok[7:]
        if self.commit is None or self.repository is None:
            sys.stderr.write("irkerhook: svn requires 'repository' and 'commit' variables.")
            sys.exit(1)
        self.project = os.path.basename(self.repository)
        self.author = self.svnlook("author")
        self.files = self.svnlook("dirs-changed").strip().replace("\n", " ")
        self.logmsg = self.svnlook("log")
        self.rev = "r%s" % self.commit
        self.template = '%(bold)s%(project)s%(reset)s: %(green)s%(author)s%(reset)s %(repo)s * %(bold)s%(rev)s%(reset)s / %(bold)s%(files)s%(reset)s: %(logmsg)s %(brown)s%(url)s%(reset)s'
        self.urlprefix = "viewcvs"
        self.load_preferences(os.path.join(self.repository, "irker.conf"))
        self.do_overrides()
    def svnlook(self, info):
        return do("svnlook %s %s --revision %s" % (shellquote(info), shellquote(self.repository), shellquote(self.commit)))

if __name__ == "__main__":
    import getopt

    try:
        (options, arguments) = getopt.getopt(sys.argv[1:], "nV")
    except getopt.GetoptError, msg:
        print "irkerhook.py: " + str(msg)
        raise SystemExit, 1

    notify = True
    channels = ""
    commit = ""
    repository = ""
    for (switch, val) in options:
        if switch == '-n':
            notify = False
        elif switch == '-V':
            print "irkerhook.py: version", version
            sys.exit(0)

    # Gather info for repo type discrimination
    for tok in arguments:
        if tok.startswith("repository="):
            repository = tok[11:]

    # Determine the repository type. Default to git unless user has pointed
    # us at a repo with identifiable internals.
    vcs = "git"
    if os.path.exists(os.path.join(repository, "format")):
        vcs = "svn"

    # Someday we'll have extractors for several version-control systems
    if vcs == "svn":
        extractor = SvnExtractor(arguments)
    else:
        extractor = GitExtractor(arguments)

    # Message reduction.  The assumption here is that IRC can't handle
    # lines more than 510 characters long. If we exceed that length, we
    # try knocking out the file list, on the theory that for notification
    # purposes the commit text is more important.  If it's still too long
    # there's nothing much can be done other than ship it expecting the IRC
    # server to truncate.
    privmsg = extractor.template % extractor.__dict__
    if len(privmsg) > 510:
        extractor.files = ""
        privmsg = extractor.template % extractor.__dict__

    # Anti-spamming guard.
    channel_list = extractor.channels.split(",")
    if extractor.maxchannels != 0:
        channel_list = channel_list[:extractor.maxchannels]

    # Ready to ship.
    message = json.dumps({"to":channel_list, "privmsg":privmsg})
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
