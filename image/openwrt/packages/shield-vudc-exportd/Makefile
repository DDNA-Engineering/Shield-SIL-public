# shield-vudc-exportd: exports the USB/IP virtual device controller (usbip-vudc)
# to a USB/IP host, as `usbipd --device` does, without libudev.
#
# OpenWrt's usbip tools link libudev-zero, which only enumerates devices that
# have a device node (/sys/dev/char, /sys/dev/block). A UDC has none, so
# `usbipd --device` never finds usbip-vudc ("requested device not found").
# This daemon reads the same sysfs files directly. Ubuntu uses usbipd itself.
include $(TOPDIR)/rules.mk

PKG_NAME:=shield-vudc-exportd
PKG_VERSION:=1
PKG_RELEASE:=1
PKG_LICENSE:=Apache-2.0

include $(INCLUDE_DIR)/package.mk

define Package/shield-vudc-exportd
  SECTION:=net
  CATEGORY:=Network
  TITLE:=USB/IP export of the usbip-vudc gadget controller (Shield SIL)
  DEPENDS:=+kmod-usb-gadget-vudc
endef

define Build/Prepare
	mkdir -p $(PKG_BUILD_DIR)
	$(CP) ./src/* $(PKG_BUILD_DIR)/
endef

define Build/Compile
	$(TARGET_CC) $(TARGET_CPPFLAGS) $(TARGET_CFLAGS) -Wall -Wextra -Werror \
		$(PKG_BUILD_DIR)/shield-vudc-exportd.c $(TARGET_LDFLAGS) -o $(PKG_BUILD_DIR)/shield-vudc-exportd
endef

define Package/shield-vudc-exportd/install
	$(INSTALL_DIR) $(1)/usr/sbin
	$(INSTALL_BIN) $(PKG_BUILD_DIR)/shield-vudc-exportd $(1)/usr/sbin/shield-vudc-exportd
endef

$(eval $(call BuildPackage,shield-vudc-exportd))
