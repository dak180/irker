# Makefile for the irker relaying tool

VERS=$(shell irkerd -V | sed 's/irkerd version //')

docs: irkerd.html irkerd.1

irkerd.1: irkerd.xml
	xmlto man irkerd.xml
irkerd.html: irkerd.xml
	xmlto html-nochunks irkerd.xml

install: irkerd.1 uninstall
	install -m 755 -o 0 -g 0 -d $(ROOT)/usr/bin/
	install -m 755 -o 0 -g 0 irkerd $(ROOT)/usr/bin/irkerd
	install -m 755 -o 0 -g 0 -d $(ROOT)/usr/share/man/man1/
	install -m 755 -o 0 -g 0 irkerd.1 $(ROOT)/usr/share/man/man1/irkerd.1

uninstall:
	rm -f ${ROOT}/usr/bin/irkerd ${ROOT}/usr/share/man/man1/irkerd.1

clean:
	rm -f irkerd.1 irker-*.tar.gz *~
	rm -f SHIPPER.* *.html

PYLINTOPTS = --rcfile=/dev/null --reports=n --include-ids=y --disable="C0103,C0111,C0301,R0201,R0902,R0903,E1101,W0201,W0621,W0702"
pylint:
	@pylint --output-format=parseable $(PYLINTOPTS) irkerd
	@pylint --output-format=parseable $(PYLINTOPTS) irkerhook.py


SOURCES = README COPYING NEWS BUGS install.txt security.txt \
	irkerd irkerhook.py Makefile irkerd.xml irker-logo.png

version:
	@echo $(VERS)

irker-$(VERS).tar.gz: $(SOURCES) irkerd.1
	tar --transform='s:^:irker-$(VERS)/:' --show-transformed-names -cvzf irker-$(VERS).tar.gz $(SOURCES)

dist: irker-$(VERS).tar.gz

release: irker-$(VERS).tar.gz irkerd.html
	shipper -u -m -t; make clean
