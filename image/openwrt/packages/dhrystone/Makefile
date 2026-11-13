# Dhrystone 2.1 (Reinhold P. Weicker), the CPU speed probe for the SIL's
# speed limit (docs/calibration.md). Fetched at build time from a pinned
# commit of a public archive of the original sources; nothing is vendored.
include $(TOPDIR)/rules.mk

PKG_NAME:=dhrystone
PKG_VERSION:=2.1
PKG_RELEASE:=1

PKG_SOURCE_PROTO:=git
PKG_SOURCE_URL:=https://github.com/Keith-S-Thompson/dhrystone.git
PKG_SOURCE_VERSION:=66bb9df1a5dea67f33437b856bf68ae52bd5c90f
PKG_MIRROR_HASH:=skip

include $(INCLUDE_DIR)/package.mk

define Package/dhrystone
  SECTION:=utils
  CATEGORY:=Utilities
  TITLE:=Dhrystone 2.1 CPU benchmark (SIL speed probe)
endef

define Package/dhrystone/description
  Dhrystone 2.1 built with -O3, the flags of the published Cortex-A53
  reference the Shield SIL compares against. Prints DMIPS for one core.
endef

# Same optimisation level as the reference measurement (GCC -O3).
TARGET_CFLAGS := $(filter-out -O%,$(TARGET_CFLAGS)) -O3

define Build/Compile
	sed -i 's|^extern  int     times ();|/* times() is declared by <sys/times.h> */|' $(PKG_BUILD_DIR)/v2.1/dhry_1.c
	$(TARGET_CC) $(TARGET_CPPFLAGS) $(TARGET_CFLAGS) -std=gnu89 -w -DHZ=100 \
		$(PKG_BUILD_DIR)/v2.1/dhry_1.c $(PKG_BUILD_DIR)/v2.1/dhry_2.c \
		$(TARGET_LDFLAGS) -o $(PKG_BUILD_DIR)/dhrystone
endef

define Package/dhrystone/install
	$(INSTALL_DIR) $(1)/usr/bin
	$(INSTALL_BIN) $(PKG_BUILD_DIR)/dhrystone $(1)/usr/bin/dhrystone
endef

$(eval $(call BuildPackage,dhrystone))
