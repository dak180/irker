#!/usr/bin/env python
# This is a trivial example of a metadata filter.
# All it does is change the name of the commit's author.
# It could do other things, including modifying the
# channels list
# 
import sys, json
metadata = json.loads(sys.argv[1])

metadata['author'] = "The Great and Powerful Oz"

print json.dumps(metadata)
# end
