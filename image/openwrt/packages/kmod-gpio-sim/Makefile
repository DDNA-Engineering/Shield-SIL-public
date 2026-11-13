# Upstream Linux gpio-sim (GPIO simulator) for OpenWrt kernels that ship it
# disabled. Sources are vendored unmodified from linux-stable, see src/README.
include $(TOPDIR)/rules.mk
include $(INCLUDE_DIR)/kernel.mk

PKG_NAME:=kmod-gpio-sim
PKG_RELEASE:=1
PKG_LICENSE:=GPL-2.0-only

# Sources and patches per kernel series (src/<series>, patches/<series>).
KSERIES:=$(KERNEL_PATCHVER)
PATCH_DIR:=./patches/$(KSERIES)

include $(INCLUDE_DIR)/package.mk

define KernelPackage/gpio-sim
  SUBMENU:=Other modules
  TITLE:=GPIO simulator (upstream gpio-sim, out of tree)
  DEPENDS:=+kmod-fs-configfs
  FILES:=$(PKG_BUILD_DIR)/gpio-sim.ko
  AUTOLOAD:=$(call AutoProbe,gpio-sim)
endef

define KernelPackage/gpio-sim/description
  Upstream gpio-sim with irq_sim linked in. Lets the Shield SIL create named
  GPIO lines (gpiochip character device and sysfs) driven from the host.
endef

define Build/Prepare
	$(if $(wildcard ./src/$(KSERIES)),,$(error no vendored gpio-sim sources for kernel $(KSERIES)))
	mkdir -p $(PKG_BUILD_DIR)
	$(CP) ./src/Kbuild $(PKG_BUILD_DIR)/
	$(CP) $(wildcard ./src/$(KSERIES)/*.h) ./src/$(KSERIES)/irq_sim.c $(PKG_BUILD_DIR)/
	$(CP) ./src/$(KSERIES)/gpio-sim.c $(PKG_BUILD_DIR)/gpio-sim-main.c
	$(Build/Patch)
endef

define Build/Compile
	+$(KERNEL_MAKE) M="$(PKG_BUILD_DIR)" modules
endef

$(eval $(call KernelPackage,gpio-sim))
