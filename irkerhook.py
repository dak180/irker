#!/usr/bin/env python
# Copyright (c) 2012 Eric S. Raymond <esr@thyrsus.com>
# Distributed under BSD terms.
#
# This script contains git porcelain and porcelain byproducts.
# Requires Python 2.6, or 2.5 with the simplejson library installed.
#
# usage: irkerhook.py [-V] [-n] [--variable=value...] [commit_id...]
#
# This script is meant to be run in an update or post-commit hook.
# Try it with -n to see the notification dumped to stdout and verify
# that it looks sane. With -V this script dumps its version and exits.
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

version = "1.9"

import os, sys, commands, socket, urllib, subprocess
from pipes import quote as shellquote
try:
    import simplejson as json	# Faster, also makes us Python-2.5-compatible
except ImportError:
    import json

def do(command):
    return commands.getstatusoutput(command)[1]

class Commit:
    def __init__(self, extractor, commit):
        "Per-commit data."
        self.commit = commit
        self.branch = None
        self.rev = None
        self.mail = None
        self.author = None
        self.files = None
        self.logmsg = None
        self.url = None
        self.__dict__.update(extractor.__dict__)
    def __str__(self):
        "Produce a notification string from this commit."
        if self.urlprefix.lower() == "none":
            self.url = ""
        else:
            urlprefix = urlprefixmap.get(self.urlprefix, self.urlprefix) 
            webview = (urlprefix % self.__dict__) + self.commit
            try:
                if urllib.urlopen(webview).getcode() == 404:
                    raise IOError
                if self.tinyifier and self.tinyifier.lower() != "none":
                    try:
                        # Didn't get a retrieval error or 404 on the web
                        # view, so try to tinyify a reference to it.
                        self.url = open(urllib.urlretrieve(self.tinyifier + webview)[0]).read()
                    except IOError:
                        self.url = webview
                else:
                    self.url = webview
            except IOError:
                self.url = ""
        return self.template % self.__dict__

class GenericExtractor:
    "Generic class for encapsulating data from a VCS."
    booleans = ["tcp"]
    numerics = ["maxchannels"]
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
        self.filtercmd = None
        # Color highlighting is disabled by default.
        self.color = None
        self.bold = self.green = self.blue = self.yellow = ""
        self.brown = self.magenta = self.cyan = self.reset = ""
    def activate_color(self, style):
        "IRC color codes."
        if style == 'ANSI':
            self.bold = '\x1b[1m'
            self.green = '\x1b[1;32m'
            self.blue = '\x1b[1;34m'
            self.red =  '\x1b[1;31m'
            self.yellow = '\x1b[1;33m'
            self.brown = '\x1b[33m'
            self.magenta = '\x1b[35m'
            self.cyan = '\x1b[36m'
            self.reset = '\x1b[0m'
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
        for tok in self.arguments:
            for key in self.__dict__:
                if tok.startswith("--" + key + "="):
                    val = tok[len(key)+3:]
                    setattr(self, key, val)
        for (key, val) in self.__dict__.items():
            if key in GenericExtractor.booleans:
                if type(val) == type("") and val.lower() == "true":
                    setattr(self, key, True)
                elif type(val) == type("") and val.lower() == "false":
                    setattr(self, key, False)
            elif key in GenericExtractor.numerics:
                setattr(self, key, int(val))
        if not self.project:
            sys.stderr.write("irkerhook.py: no project name set!\n")
            raise SystemExit(1)
        if not self.repo:
            self.repo = self.project.lower()
        if not self.channels:
            self.channels = default_channels % self.__dict__
        if self.color and self.color.lower() != "none":
            self.activate_color(self.color)

def has(dirname, paths):
    "Test for existence of a list of paths."
    return all([os.path.exists(os.path.join(dirname, x)) for x in paths])

# VCS-dependent code begins here

class GitExtractor(GenericExtractor):
    "Metadata extraction for the git version control system."
    @staticmethod
    def is_repository(dirname):
        # Must detect both ordinary and bare repositories
        return has(dirname, [".git"]) or \
               has(dirname, ["HEAD", "refs", "objects"])
    def __init__(self, arguments):
        GenericExtractor.__init__(self, arguments)
        # Get all global config variables
        self.project = do("git config --get irker.project")
        self.repo = do("git config --get irker.repo")
        self.server = do("git config --get irker.server")
        self.channels = do("git config --get irker.channels")
        self.tcp = do("git config --bool --get irker.tcp")
        self.template = '%(bold)s%(project)s:%(reset)s %(green)s%(author)s%(reset)s %(repo)s:%(yellow)s%(branch)s%(reset)s * %(bold)s%(rev)s%(reset)s / %(bold)s%(files)s%(reset)s: %(logmsg)s %(brown)s%(url)s%(reset)s'
        self.tinyifier = do("git config --get irker.tinyifier") or default_tinyifier
        self.color = do("git config --get irker.color")
        self.urlprefix = do("git config --get irker.urlprefix") or "gitweb"
        self.filtercmd = do("git config --get irker.filtercmd")
        # These are git-specific
        self.refname = do("git symbolic-ref HEAD 2>/dev/null")
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
        # Get overrides
        self.do_overrides()
    def head(self):
        "Return a symbolic reference to the tip commit of the current branch."
        return "HEAD"
    def commit_factory(self, commit_id):
        "Make a Commit object holding data for a specified commit ID."
        commit = Commit(self, commit_id)
        commit.branch = os.path.basename(self.refname)
        # Compute a description for the revision
        if self.revformat == 'raw':
            commit.rev = commit.commit
        elif self.revformat == 'short':
            commit.rev = ''
        else: # self.revformat == 'describe'
            commit.rev = do("git describe %s 2>/dev/null" % shellquote(commit.commit))
        if not commit.rev:
            commit.rev = commit.commit[:12]
        # Extract the meta-information for the commit
        commit.files = do("git diff-tree -r --name-only " + shellquote(commit.commit))
        commit.files = " ".join(commit.files.strip().split("\n")[1:])
        # Design choice: for git we ship only the first line, which is
        # conventionally supposed to be a summary of the commit.  Under
        # other VCSes a different choice may be appropriate.
        metainfo = do("git log -1 '--pretty=format:%an <%ae>|%s' " + shellquote(commit.commit))
        (commit.author, commit.logmsg) = metainfo.split("|")
        commit.mail = commit.author.split()[-1].strip("<>")
        # This discards the part of the author's address after @.
        # Might be be nice to ship the full email address, if not
        # for spammers' address harvesters - getting this wrong
        # would make the freenode #commits channel into harvester heaven.
        commit.author = commit.mail.split("@")[0]
        return commit

class SvnExtractor(GenericExtractor):
    "Metadata extraction for the svn version control system."
    @staticmethod
    def is_repository(dirname):
        return has(dirname, ["format", "hooks", "locks"])
    def __init__(self, arguments):
        GenericExtractor.__init__(self, arguments)
        # Some things we need to have before metadata queries will work
        self.repository = '.'
        for tok in arguments:
            if tok.startswith("--repository="):
                self.repository = tok[13:]
        self.project = os.path.basename(self.repository)
        self.template = '%(bold)s%(project)s%(reset)s: %(green)s%(author)s%(reset)s %(repo)s * %(bold)s%(rev)s%(reset)s / %(bold)s%(files)s%(reset)s: %(logmsg)s %(brown)s%(url)s%(reset)s'
        self.urlprefix = "viewcvs"
        self.load_preferences(os.path.join(self.repository, "irker.conf"))
        self.do_overrides()
    def head(self):
        sys.stderr.write("irker: under svn, hook requires a commit argument.\n")
        raise SystemExit(1)
    def commit_factory(self, commit_id):
        self.id = commit_id
        commit = Commit(self, commit_id)
        commit.branch = ""
        commit.rev = "r%s" % self.id
        commit.author = self.svnlook("author")
        commit.files = self.svnlook("dirs-changed").strip().replace("\n", " ")
        commit.logmsg = self.svnlook("log")
        return commit
    def svnlook(self, info):
        return do("svnlook %s %s --revision %s" % (shellquote(info), shellquote(self.repository), shellquote(self.id)))

class HgExtractor(GenericExtractor):
    "Metadata extraction for the Mercurial version control system."
    @staticmethod
    def is_repository(directory):
        return has(directory, [".hg"])
    def __init__(self, arguments):
        # This fiddling with arguments is necessary since the Mercurial hook can
        # be run in two different ways: either directly via Python (in which
        # case hg should be pointed to the hg_hook function below) or as a
        # script (in which case the normal __main__ block at the end of this
        # file is exercised).  In the first case, we already get repository and
        # ui objects from Mercurial, in the second case, we have to create them
        # from the root path.
        self.repository = None
        if arguments and type(arguments[0]) == type(()):
            # Called from hg_hook function
            ui, self.repository = arguments[0]
            arguments = []  # Should not be processed further by do_overrides
        else:
            # Called from command line: create repo/ui objects
            from mercurial import hg, ui as uimod

            repopath = '.'
            for tok in arguments:
                if tok.startswith('--repository='):
                    repopath = tok[13:]
            ui = uimod.ui()
            ui.readconfig(os.path.join(repopath, '.hg', 'hgrc'), repopath)
            self.repository = hg.repository(ui, repopath)

        GenericExtractor.__init__(self, arguments)
        # Extract global values from the hg configuration file(s)
        self.project = ui.config('irker', 'project')
        self.repo = ui.config('irker', 'repo')
        self.server = ui.config('irker', 'server')
        self.channels = ui.config('irker', 'channels')
        self.tcp = str(ui.configbool('irker', 'tcp'))  # converted to bool again in do_overrides
        self.template = '%(bold)s%(project)s:%(reset)s %(green)s%(author)s%(reset)s %(repo)s:%(yellow)s%(branch)s%(reset)s * %(bold)s%(rev)s%(reset)s / %(bold)s%(files)s%(reset)s: %(logmsg)s %(brown)s%(url)s%(reset)s'
        self.tinyifier = ui.config('irker', 'tinyifier') or default_tinyifier
        self.color = ui.config('irker', 'color')
        self.urlprefix = (ui.config('irker', 'urlprefix') or
                          ui.config('web', 'baseurl') or '')
        if self.urlprefix:
            self.urlprefix = self.urlprefix.rstrip('/') + '/rev'
            # self.commit is appended to this by do_overrides
        if not self.project:
            self.project = os.path.basename(self.repository.root.rstrip('/'))
        self.do_overrides()
    def head(self):
        "Return a symbolic reference to the tip commit of the current branch."
        return "-1"
    def commit_factory(self, commit_id):
        "Make a Commit object holding data for a specified commit ID."
        from mercurial.node import short
        from mercurial.templatefilters import person
        node = self.repository.lookup(commit_id)
        commit = Commit(self, short(node))
        # Extract commit-specific values from a "context" object
        ctx = self.repository.changectx(node)
        commit.rev = '%d:%s' % (ctx.rev(), commit.commit)
        commit.branch = ctx.branch()
        commit.author = person(ctx.user())
        commit.logmsg = ctx.description()
        # Extract changed files from status against first parent
        st = self.repository.status(ctx.p1().node(), ctx.node())
        commit.files = ' '.join(st[0] + st[1] + st[2])
        return commit

def hg_hook(ui, repo, **kwds):
    # To be called from a Mercurial "commit" or "incoming" hook.  Example
    # configuration:
    # [hooks]
    # incoming.irker = python:/path/to/irkerhook.py:hg_hook
    extractor = HgExtractor([(ui, repo)])
    ship(extractor, kwds['node'], False)

# The files we use to identify a Subversion repo might occur as content
# in a git or hg repo, but the special subdirectories for those are more
# reliable indicators.  So test for Subversion last.
extractors = [GitExtractor, HgExtractor, SvnExtractor]

# VCS-dependent code ends here

def ship(extractor, commit, debug):
    "Ship a notification for the specified commit."
    metadata = extractor.commit_factory(commit)

    # This is where we apply filtering
    if extractor.filtercmd:
        cmd = '%s %s' % (shellquote(extractor.filtercmd),
                         shellquote(json.dumps(metadata.__dict__)))
        data = subprocess.Popen(cmd, shell=True, stdout=subprocess.PIPE).stdout.read()
        try:
            metadata.__dict__.update(json.loads(data))
        except ValueError:
            sys.stderr.write("irkerhook.py: could not decode JSON: %s\n" % data)
            raise SystemExit, 1

    # Message reduction.  The assumption here is that IRC can't handle
    # lines more than 510 characters long. If we exceed that length, we
    # try knocking out the file list, on the theory that for notification
    # purposes the commit text is more important.  If it's still too long
    # there's nothing much can be done other than ship it expecting the IRC
    # server to truncate.
    privmsg = str(metadata)
    if len(privmsg) > 510:
        metadata.files = ""
        privmsg = str(metadata)

    # Anti-spamming guard.  It's deliberate that we get maxchannels not from
    # the user-filtered metadata but from the extractor data - means repo
    # administrators can lock in that setting.
    channels = metadata.channels.split(",")
    if extractor.maxchannels != 0:
        channels = channels[:extractor.maxchannels]

    # Ready to ship.
    message = json.dumps({"to": channels, "privmsg": privmsg})
    if debug:
        print message
    elif channels:
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

if __name__ == "__main__":
    notify = True
    repository = os.getcwd()
    commits = []
    for arg in sys.argv[1:]:
        if arg == '-n':
            notify = False
        elif arg == '-V':
            print "irkerhook.py: version", version
            sys.exit(0)
        elif arg.startswith("--repository="):
            repository = arg[13:]
        elif not arg.startswith("--"):
            commits.append(arg)

    # Figure out which extractor we should be using
    for candidate in extractors:
        if candidate.is_repository(repository):
            cls = candidate
            break
    else:
        sys.stderr.write("irkerhook: cannot identify a repository type.\n")
        raise SystemExit(1)
    extractor = cls(sys.argv[1:])

    # And apply it.
    if not commits:
        commits = [extractor.head()]
    for commit in commits:
        ship(extractor, commit, not notify)

#End
