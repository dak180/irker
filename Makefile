# Makefile for the irker relaying tool

VERS=$(shell sed -n 's/version = "\(.\+\)"/\1/p' irkerd)

docs: irkerd.html irkerd.8 irkerhook.html irkerhook.1

irkerd.8: irkerd.xml
	xmlto man irkerd.xml
irkerd.html: irkerd.xml
	xmlto html-nochunks irkerd.xml

irkerhook.1: irkerhook.xml
	xmlto man irkerhook.xml
irkerhook.html: irkerhook.xml
	xmlto html-nochunks irkerhook.xml

security.html: security.txt
	asciidoc -o security.html security.txt
hacking.html: hacking.txt
	asciidoc -o hacking.html hacking.txt

install: irkerd.8 irkerhook.1 uninstall
	install -m 755 -o 0 -g 0 -d $(DESTDIR)/usr/bin/
	install -m 755 -o 0 -g 0 irkerd $(DESTDIR)/usr/bin/irkerd
	install -m 755 -o 0 -g 0 -d $(DESTDIR)/usr/share/man/man8/
	install -m 755 -o 0 -g 0 irkerd.8 $(DESTDIR)/usr/share/man/man8/irkerd.8
	install -m 755 -o 0 -g 0 -d $(DESTDIR)/usr/share/man/man1/
	install -m 755 -o 0 -g 0 irkerhook.1 $(DESTDIR)/usr/share/man/man1/irkerhook.1

uninstall:
	rm -f $(DESTDIR)/usr/bin/irkerd
	rm -f $(DESTDIR)/usr/share/man/man8/irkerd.8
	rm -f $(DESTDIR)/usr/share/man/man1/irkerhook.1

clean:
	rm -f irkerd.8 irkerhook.1 irker-*.tar.gz *~
	rm -f SHIPPER.* *.html

PYLINTOPTS = --rcfile=/dev/null --reports=n --include-ids=y --disable="C0103,C0111,C0301,R0201,R0902,R0903,R0912,E1101,W0201,W0621,W0702,F0401"
pylint:
	@pylint --output-format=parseable $(PYLINTOPTS) irkerd
	@pylint --output-format=parseable $(PYLINTOPTS) irkerhook.py

loc:
	@echo "LOC:"; wc -l irkerd irkerhook.py
	@echo -n "LLOC: "; grep -vE '(^ *#|^ *$$)' irkerd irkerhook.py | wc -l

SOURCES = README COPYING NEWS install.txt security.txt hacking.txt \
	irkerd irkerhook.py filter-example.py filter-test.py \
	Makefile irkerd.xml irkerhook.xml
EXTRA_DIST = irker-logo.png org.catb.irkerd.plist

version:
	@echo $(VERS)

irker-$(VERS).tar.gz: $(SOURCES) irkerd.8 irkerhook.1
	tar --transform='s:^:irker-$(VERS)/:' --show-transformed-names -cvzf irker-$(VERS).tar.gz $(SOURCES) $(EXTRA_DIST)

dist: irker-$(VERS).tar.gz

release: irker-$(VERS).tar.gz irkerd.html irkerhook.html security.html hacking.html
	shipper -u -m -t; make clean
