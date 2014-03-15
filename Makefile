# Makefile for the irker relaying daemon

VERS := $(shell sed -n 's/version = "\(.\+\)"/\1/p' irkerd)
SYSTEMDSYSTEMUNITDIR := $(shell pkg-config --variable=systemdsystemunitdir systemd)

# `prefix`, `mandir` & `DESTDIR` can and should be set on the command
# line to control installation locations
prefix ?= /usr
mandir ?= /share/man
target = $(DESTDIR)$(prefix)

docs: irkerd.html irkerd.8 irkerhook.html irkerhook.1

irkerd.8: irkerd.xml
	xmlto man irkerd.xml
irkerd.html: irkerd.xml
	xmlto html-nochunks irkerd.xml

irkerhook.1: irkerhook.xml
	xmlto man irkerhook.xml
irkerhook.html: irkerhook.xml
	xmlto html-nochunks irkerhook.xml

install.html: install.txt
	asciidoc -o install.html install.txt
security.html: security.txt
	asciidoc -o security.html security.txt
hacking.html: hacking.txt
	asciidoc -o hacking.html hacking.txt

install: irkerd.8 irkerhook.1 uninstall
	install -m 755 -o 0 -g 0 -d "$(target)/bin"
	install -m 755 -o 0 -g 0 irkerd "$(target)/bin/irkerd"
ifneq ($(strip $(SYSTEMDSYSTEMUNITDIR)),)
	install -m 755 -o 0 -g 0 -d "$(DESTDIR)$(SYSTEMDSYSTEMUNITDIR)"
	install -m 644 -o 0 -g 0 irkerd.service "$(DESTDIR)$(SYSTEMDSYSTEMUNITDIR)"
endif
	install -m 755 -o 0 -g 0 -d "$(target)$(mandir)/man8"
	install -m 755 -o 0 -g 0 irkerd.8 "$(target)$(mandir)/man8/irkerd.8"
	install -m 755 -o 0 -g 0 -d "$(target)$(mandir)/man1"
	install -m 755 -o 0 -g 0 irkerhook.1 "$(target)$(mandir)/man1/irkerhook.1"

uninstall:
	rm -f "$(target)/bin/irkerd"
ifneq ($(strip $(SYSTEMDSYSTEMUNITDIR)),)
	rm -f "$(DESTDIR)$(SYSTEMDSYSTEMUNITDIR)/irkerd.service"
endif
	rm -f "$(target)$(mandir)/man8/irkerd.8"
	rm -f "$(target)$(mandir)/man1/irkerhook.1"

clean:
	rm -f irkerd.8 irkerhook.1 irker-*.tar.gz *~ *.html

PYLINTOPTS = --rcfile=/dev/null --reports=n --include-ids=y --disable="C0103,C0111,C0301,C0302,R0201,R0902,R0903,R0912,R0913,R0914,R0915,E1101,W0142,W0201,W0212,W0621,W0702,W0703,F0401,E0611"
pylint:
	@pylint --output-format=parseable $(PYLINTOPTS) irkerd
	@pylint --output-format=parseable $(PYLINTOPTS) irkerhook.py

loc:
	@echo "LOC:"; wc -l irkerd irkerhook.py
	@echo -n "LLOC: "; grep -vE '(^ *#|^ *$$)' irkerd irkerhook.py | wc -l

DOCS = \
	README \
	COPYING \
	NEWS \
	install.txt \
	security.txt \
	hacking.txt \
	irkerhook.xml \
	irkerd.xml \

SOURCES = \
	$(DOCS) \
	irkerd \
	irkerhook.py \
	filter-example.py \
	filter-test.py \
	irk \
	Makefile

EXTRA_DIST = \
	org.catb.irkerd.plist \
	irkerd.service \
	irker-logo.png

version:
	@echo $(VERS)

irker-$(VERS).tar.gz: $(SOURCES) irkerd.8 irkerhook.1
	mkdir irker-$(VERS)
	cp -pR $(SOURCES) $(EXTRA_DIST) irker-$(VERS)/
	@COPYFILE_DISABLE=1 tar -cvzf irker-$(VERS).tar.gz irker-$(VERS)
	rm -fr irker-$(VERS)

dist: irker-$(VERS).tar.gz

release: irker-$(VERS).tar.gz irkerd.html irkerhook.html install.html security.html hacking.html
	shipper version=$(VERS) | sh -e -x
