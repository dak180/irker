#!/usr/bin/env python
# Copyright (c) 2012 Eric S. Raymond <esr@thyrsus.com>
# Distributed under BSD terms.
#
# This script contains porcelain and porcelain byproducts.
# It should be compatible back to Python 2.1.5
#
# usage: git-irkbot.py [-V] [-n] [-p projectname]  [refname [commits...]]
#
# This script is meant to be run either in a post-commit hook or in an
# update hook. Try it with -n to see the notification mail dumped to
# stdout and verify that it looks sane. With -V it dumps its version
# and exits.
#
# In post-commit, run it without arguments. It will query for
# current HEAD and the latest commit ID to get the information it
# needs.
#
# In update, call it with a refname followed by a list of commits:
# You want to reverse the order git rev-list emits because it lists
# from most recent to oldest.
#
# /path/to/git-irkbot.py ${refname} $(git rev-list ${oldhead}..${newhead} | tac)
#
# Configuration variables affecting this script:
#
# irker.project = name of the project
# irker.channels = list of IRC URLs corresponding to channels
# irker.repo = name of the project repo for gitweb/cgit purposes
# irker.revformat = format in which the revision is shown
# irker.server = location of the irker server to use for relaying
#
# irker.channels defaults to a project channel on freenode, and #commits
# irker.project defaults to the directory name of the repository toplevel.
# irker.repo defaults to irker.project lowercased.
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
default_irker_port = 6659

# Changeset URL prefix for your repo: when the commit ID is appended
# to this, it should point at a CGI that will display the commit
# through gitweb or something similar. The defaults will probably
# work if you have a typical gitweb/cgit setup.
#
#urlprefix="http://%(host)s/cgi-bin/gitweb.cgi?p=%(repo)s;a=commit;h="
urlprefix="http://%(host)s/cgi-bin/cgit.cgi/%(repo)s/commit/?id="

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

# Identify the generator script.
# Should only change when the script itself gets a new home and maintainer.
generator = "http://www.catb.org/~esr/irker/git-irkbot.py"
version = "1.0"

def do(command):
    return commands.getstatusoutput(command)[1]

def extract(refname, merged):
    "Extract metadata to be reported to CIA."

    # Try to tinyfy a reference to a web view for this commit.
    try:
        url = open(urllib.urlretrieve(tinyifier + urlprefix + merged)[0]).read()
    except:
        url = urlprefix + merged

    branch = os.path.basename(refname)

    # Compute a description for the revision
    if revformat == 'raw':
        rev = merged
    elif revformat == 'short':
        rev = ''
    else: # revformat == 'describe'
        rev = do("git describe %s 2>/dev/null" % merged)
    if not rev:
        rev = merged[:12]

    # Extract the meta-information for the commit
    files=do("git diff-tree -r --name-only '"+ merged +"' | sed -e '1d' -e 's-.*-&-'")
    metainfo = do("git log -1 '--pretty=format:%an <%ae>%n%at%n%s' " + merged)
    (author, ts, logmsg) = metainfo.split("\n")

    # This discards the part of the author's address after @.
    # Might be be nice to ship the full email address, if not
    # for spammers' address harvesters - getting this wrong
    # would make the freenode #commits channel into harvester heaven.
    author = author.replace("<", "").split("@")[0].split()[-1]

    # This ignores the timezone.  Not clear what to do with it...
    ts = ts.strip().split()[0]

    context = locals()
    context.update(globals())

    return context

if __name__ == "__main__":
    import getopt

    # Get all config variables
    revformat = do("git config --get irker.revformat")
    project = do("git config --get irker.project")
    repo = do("git config --get irker.repo")
    server = do("git config --get irker.server")
    channels = do("git config --get irker.channels")

    host = socket.getfqdn()

    try:
        (options, arguments) = getopt.getopt(sys.argv[1:], "np:V")
    except getopt.GetoptError, msg:
        print "git-irkbot.py: " + str(msg)
        raise SystemExit, 1

    notify = True
    for (switch, val) in options:
        if switch == '-p':
            project = val
        elif switch == '-n':
            notify = False
        elif switch == '-V':
            print "git-irkbot.py: version", version
            sys.exit(0)

    # The project variable defaults to the name of the repository toplevel. 
    if not project:
        here = os.getcwd()
        while True:
            if os.path.exists(os.path.join(here, ".git")):
                project = os.path.basename(here)
                break
            elif here == '/':
                sys.stderr.write("git-irkbot.py: no .git below root!\n")
                sys.exit(1)
            here = os.path.dirname(here)

    # By default, the channel list includes the freenode #commits list 
    if not channels:
        channels = "irc://chat.freenode.net/%s,irc://chat.freenode.net/#commits" % project

    if not repo:
        repo = project.lower()

    urlprefix = urlprefix % globals()

    # The script wants a reference to head followed by the list of
    # commit ID to report about.
    if len(arguments) == 0:
        refname = do("git symbolic-ref HEAD 2>/dev/null")
        merges = [do("git rev-parse HEAD")]
    else:
        refname = arguments[0]
        merges = arguments[1:]

    for merged in merges:
        context = extract(refname, merged)
        privmsg = template % context
        channel_list = channels.split(",")
        structure = {"to":channel_list, "privmsg":privmsg}
        message = json.dumps(structure)
        if not notify:
            print message
        else:
            try:
                # FIXME: Actual delivery must go here. Use the server variable.
                pass
            except socket.error, e:
                sys.stderr.write("%s\n" % e)

#End
