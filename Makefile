# Makefile for the irker relaying tool

VERS=$(shell irker.py -V | sed 's/irker version //')

docs: irker.html irker.1

irker.1: irker.xml
	xmlto man irker.xml
irker.html: irker.xml
	xmlto html-nochunks irker.xml

install: irker.1 uninstall
	install -m 755 -o 0 -g 0 -d $(ROOT)/usr/bin/
	install -m 755 -o 0 -g 0 irker.py $(ROOT)/usr/bin/irker.py
	install -m 755 -o 0 -g 0 -d $(ROOT)/usr/share/man/man1/
	install -m 755 -o 0 -g 0 irker.1 $(ROOT)/usr/share/man/man1/irker.1

uninstall:
	rm -f ${ROOT}/usr/bin/irker.py ${ROOT}/usr/share/man/man1/irker.1

clean:
	rm -f irker irker.1 irker-*.rpm irker-*.tar.gz *~
	rm -f SHIPPER.* *.html

PYLINTOPTS = --rcfile=/dev/null --reports=n --include-ids=y --disable="C0103,C0111,C0301,R0201,R0902,R0903,E1101"
pylint:
	@pylint --output-format=parseable $(PYLINTOPTS) irker.py


SOURCES = README COPYING NEWS irker.py Makefile irker.xml irker-logo.jpg

version:
	@echo $(VERS)

irker-$(VERS).tar.gz: $(SOURCES) irker.1
	@ls $(SOURCES) irker.1 | sed s:^:irker-$(VERS)/: >MANIFEST
	@(cd ..; ln -s irker irker-$(VERS))
	(cd ..; tar -czf irker/irker-$(VERS).tar.gz `cat irker/MANIFEST`)
	@(cd ..; rm irker-$(VERS))

dist: irker-$(VERS).tar.gz

release: irker-$(VERS).tar.gz irker.html
	shipper -u -m -t; make clean
