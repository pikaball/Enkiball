CC ?= cc
CFLAGS ?= -O2 -Wall -Wextra -Werror -D_FORTIFY_SOURCE=2 -fstack-protector-strong
PREFIX ?= /usr/local

all: build/enkisandbox-client

build/enkisandbox-client: native/client.c
	mkdir -p build
	$(CC) $(CFLAGS) -o $@ $<

install-client: all
	install -Dm755 build/enkisandbox-client $(DESTDIR)$(PREFIX)/bin/enkisandbox-client

clean:
	rm -rf build

.PHONY: all install-client clean
