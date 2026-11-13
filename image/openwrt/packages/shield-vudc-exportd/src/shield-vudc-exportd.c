// SPDX-License-Identifier: Apache-2.0
/*
 * shield-vudc-exportd: the device side of USB/IP for the usbip-vudc gadget
 * controller, i.e. what `usbipd --device` does, reading sysfs directly instead
 * of through libudev (libudev-zero cannot enumerate UDCs).
 *
 * Protocol: Documentation/usb/usbip_protocol.rst. Only OP_REQ_IMPORT is served
 * (the SIL's host imports the gadget by bus ID). On import the connected
 * socket is handed to the kernel through <platform device>/usbip_sockfd, and
 * the reply carries the device's descriptor fields, as usbipd sends them.
 *
 *   shield-vudc-exportd [port]      (default 3240)
 */
#include <arpa/inet.h>
#include <errno.h>
#include <netinet/in.h>
#include <netinet/tcp.h>
#include <signal.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/socket.h>
#include <unistd.h>

#define USBIP_VERSION 0x0111
#define OP_REQ_IMPORT 0x8003
#define OP_REP_IMPORT 0x0003
#define ST_OK 0x00
#define ST_NA 0x01
#define ST_DEV_BUSY 0x02
#define ST_NODEV 0x04
#define SDEV_ST_AVAILABLE 1
#define SDEV_ST_USED 2
#define BUSID_SIZE 32
#define PATH_SIZE 256

struct __attribute__((packed)) usbip_usb_device {
	char path[PATH_SIZE];
	char busid[BUSID_SIZE];
	uint32_t busnum, devnum, speed;
	uint16_t idVendor, idProduct, bcdDevice;
	uint8_t bDeviceClass, bDeviceSubClass, bDeviceProtocol;
	uint8_t bConfigurationValue, bNumConfigurations, bNumInterfaces;
};

static void logmsg(const char *fmt, const char *arg)
{
	fprintf(stderr, "shield-vudc-exportd: ");
	fprintf(stderr, fmt, arg);
	fputc('\n', stderr);
}

static int read_full(int fd, void *buf, size_t len)
{
	size_t got = 0;
	while (got < len) {
		ssize_t n = recv(fd, (char *)buf + got, len - got, 0);
		if (n <= 0) {
			if (n < 0 && errno == EINTR)
				continue;
			return -1;
		}
		got += n;
	}
	return 0;
}

static int write_full(int fd, const void *buf, size_t len)
{
	size_t done = 0;
	while (done < len) {
		ssize_t n = send(fd, (const char *)buf + done, len - done, MSG_NOSIGNAL);
		if (n <= 0) {
			if (n < 0 && errno == EINTR)
				continue;
			return -1;
		}
		done += n;
	}
	return 0;
}

static int read_file(const char *path, void *buf, size_t len)
{
	FILE *f = fopen(path, "r");
	size_t n;
	if (!f)
		return -1;
	n = fread(buf, 1, len, f);
	fclose(f);
	return (int)n;
}

/* The bus ID must name a usbip-vudc platform device (no path tricks). */
static int valid_busid(const char *busid)
{
	return strncmp(busid, "usbip-vudc.", 11) == 0 && strspn(busid + 11, "0123456789") == strlen(busid + 11) &&
	       strlen(busid) > 11;
}

static uint32_t speed_of(const char *busid)
{
	static const char *names[] = { "UNKNOWN", "low-speed", "full-speed", "high-speed", "wireless", "super-speed" };
	char path[PATH_SIZE + 64], s[32] = "";
	size_t i;
	snprintf(path, sizeof(path), "/sys/class/udc/%s/current_speed", busid);
	if (read_file(path, s, sizeof(s) - 1) <= 0)
		return 0;
	s[strcspn(s, "\n")] = 0;
	for (i = 0; i < sizeof(names) / sizeof(names[0]); i++)
		if (!strcmp(s, names[i]))
			return i;
	return 0;
}

static void serve(int conn)
{
	struct { uint16_t version, code; uint32_t status; } __attribute__((packed)) op;
	char busid[BUSID_SIZE + 1] = "", path[PATH_SIZE + 64], st[16] = "";
	uint8_t desc[18];
	struct usbip_usb_device dev;
	uint32_t status = ST_OK;
	int one = 1;

	if (read_full(conn, &op, sizeof(op)) < 0 || ntohs(op.code) != OP_REQ_IMPORT)
		return;
	if (read_full(conn, busid, BUSID_SIZE) < 0)
		return;
	busid[BUSID_SIZE] = 0;

	memset(&dev, 0, sizeof(dev));
	snprintf(path, sizeof(path), "/sys/devices/platform/%s/dev_desc", busid);
	if (!valid_busid(busid) || read_file(path, desc, sizeof(desc)) != sizeof(desc)) {
		logmsg("requested device not found: %s", busid);
		status = ST_NODEV;
	} else {
		snprintf(path, sizeof(path), "/sys/devices/platform/%s/usbip_status", busid);
		if (read_file(path, st, sizeof(st) - 1) <= 0 || atoi(st) != SDEV_ST_AVAILABLE) {
			logmsg("device not available (in use or not bound): %s", busid);
			status = atoi(st) == SDEV_ST_USED ? ST_DEV_BUSY : ST_NA;
		}
	}
	if (status == ST_OK) {
		char fd[16];
		FILE *f;
		setsockopt(conn, IPPROTO_TCP, TCP_NODELAY, &one, sizeof(one));
		snprintf(path, sizeof(path), "/sys/devices/platform/%s/usbip_sockfd", busid);
		snprintf(fd, sizeof(fd), "%d\n", conn);
		f = fopen(path, "w");
		if (!f || fputs(fd, f) < 0 || fclose(f) != 0) {
			logmsg("cannot hand the connection to the kernel: %s", busid);
			status = ST_NA;
		} else {
			logmsg("connect: %s", busid);
		}
	}

	op.version = htons(USBIP_VERSION);
	op.code = htons(OP_REP_IMPORT);
	op.status = htonl(status);
	if (write_full(conn, &op, sizeof(op)) < 0 || status != ST_OK)
		return;

	snprintf(dev.path, sizeof(dev.path), "/sys/devices/platform/%s", busid);
	memcpy(dev.busid, busid, BUSID_SIZE - 1);
	dev.speed = htonl(speed_of(busid));
	/* struct usb_device_descriptor, little-endian fields */
	dev.bDeviceClass = desc[4];
	dev.bDeviceSubClass = desc[5];
	dev.bDeviceProtocol = desc[6];
	dev.idVendor = htons(desc[8] | desc[9] << 8);
	dev.idProduct = htons(desc[10] | desc[11] << 8);
	dev.bcdDevice = htons(desc[12] | desc[13] << 8);
	dev.bNumConfigurations = desc[17];
	write_full(conn, &dev, sizeof(dev));
}

int main(int argc, char **argv)
{
	int port = argc > 1 ? atoi(argv[1]) : 3240, one = 1, ls;
	struct sockaddr_in6 a;

	signal(SIGPIPE, SIG_IGN);
	ls = socket(AF_INET6, SOCK_STREAM, 0);
	if (ls < 0) {
		perror("socket");
		return 1;
	}
	setsockopt(ls, SOL_SOCKET, SO_REUSEADDR, &one, sizeof(one));
	memset(&a, 0, sizeof(a));
	a.sin6_family = AF_INET6;
	a.sin6_addr = in6addr_any; /* dual-stack: IPv4 too */
	a.sin6_port = htons(port);
	if (bind(ls, (struct sockaddr *)&a, sizeof(a)) < 0 || listen(ls, 4) < 0) {
		perror("bind/listen");
		return 1;
	}
	logmsg("listening on port %s", argc > 1 ? argv[1] : "3240");
	for (;;) {
		int conn = accept(ls, NULL, NULL);
		if (conn < 0) {
			if (errno == EINTR)
				continue;
			perror("accept");
			return 1;
		}
		serve(conn);
		/* After a successful import the kernel holds its own reference. */
		close(conn);
	}
}
