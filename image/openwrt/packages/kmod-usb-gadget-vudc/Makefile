# Upstream Linux USB gadget core (libcomposite), Ethernet gadget function (ECM)
# and the USB/IP virtual device controller (usbip-vudc), for OpenWrt kernels
# that ship them disabled (libcomposite only where it is not built in).
# Sources are vendored unmodified from linux-stable, see src/README.md.
include $(TOPDIR)/rules.mk
include $(INCLUDE_DIR)/kernel.mk

PKG_NAME:=kmod-usb-gadget-vudc
PKG_RELEASE:=1
PKG_LICENSE:=GPL-2.0-only

KSERIES:=$(KERNEL_PATCHVER)
# Some kernels have libcomposite built in (OpenWrt 25.12 armsr): build and ship
# the module only where they do not.
COMPOSITE_BUILTIN:=$(shell grep -qx 'CONFIG_USB_LIBCOMPOSITE=y' $(LINUX_DIR)/.config 2>/dev/null && echo y)
COMPOSITE_MOD:=$(if $(COMPOSITE_BUILTIN),,libcomposite)

include $(INCLUDE_DIR)/package.mk

define KernelPackage/usb-gadget-vudc
  SUBMENU:=USB Support
  TITLE:=USB gadget stack + USB/IP virtual device controller (upstream, out of tree)
  DEPENDS:=+kmod-fs-configfs +kmod-usbip
  FILES:= \
	$(if $(COMPOSITE_BUILTIN),,$(PKG_BUILD_DIR)/libcomposite.ko) \
	$(PKG_BUILD_DIR)/u_ether.ko \
	$(PKG_BUILD_DIR)/usb_f_ecm.ko \
	$(PKG_BUILD_DIR)/usbip-vudc.ko
  AUTOLOAD:=$(call AutoProbe,$(COMPOSITE_MOD) u_ether usb_f_ecm)
endef

define KernelPackage/usb-gadget-vudc/description
  libcomposite (configfs gadgets), u_ether + usb_f_ecm (CDC Ethernet function)
  and usbip-vudc, so the Shield SIL can present Shield's USB-C network gadget
  (usb0) over USB/IP to a simulated laptop.
endef

define Build/Prepare
	$(if $(wildcard ./src/$(KSERIES)),,$(error no vendored gadget sources for kernel $(KSERIES)))
	mkdir -p $(PKG_BUILD_DIR)
	$(CP) ./src/Kbuild ./src/$(KSERIES)/* $(PKG_BUILD_DIR)/
endef

define Build/Compile
	+$(KERNEL_MAKE) M="$(PKG_BUILD_DIR)" modules
endef

$(eval $(call KernelPackage,usb-gadget-vudc))
