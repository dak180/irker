# Makefile for the irker relaying tool

VERS=$(shell irker -V | sed 's/irker version //')

docs: irker.html irker.1

irker.1: irker.xml
	xmlto man irker.xml
irker.html: irker.xml
	xmlto html-nochunks irker.xml

install: irker.1 uninstall
	install -m 755 -o 0 -g 0 -d $(ROOT)/usr/bin/
	install -m 755 -o 0 -g 0 irker $(ROOT)/usr/bin/irker.py
	install -m 755 -o 0 -g 0 -d $(ROOT)/usr/share/man/man1/
	install -m 755 -o 0 -g 0 irker.1 $(ROOT)/usr/share/man/man1/irker.1

uninstall:
	rm -f ${ROOT}/usr/bin/irker ${ROOT}/usr/share/man/man1/irker.1

clean:
	rm -f irker irker.1 irker-*.rpm irker-*.tar.gz *~
	rm -f SHIPPER.* *.html

PYLINTOPTS = --rcfile=/dev/null --reports=n --include-ids=y --disable="C0103,C0111,C0301,R0201,R0902,R0903,E1101,W0621,W0702"
pylint:
	@pylint --output-format=parseable $(PYLINTOPTS) irker
	@pylint --output-format=parseable $(PYLINTOPTS) irkerhook.py


SOURCES = README COPYING NEWS BUGS install.txt \
	irker irkerhook.py Makefile irker.xml irker-logo.png

version:
	@echo $(VERS)

irker-$(VERS).tar.gz: $(SOURCES) irker.1
	tar --transform='s:^:irker-$(VERS)/:' --show-transformed-names -cvzf irker-$(VERS).tar.gz $(SOURCES)

dist: irker-$(VERS).tar.gz

release: irker-$(VERS).tar.gz irker.html
	shipper -u -m -t; make clean
