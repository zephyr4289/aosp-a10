# Product registry for the PL2 device tree — registers the QASSA product
# alongside the stock Lineage one. Applied by scripts/02.
PRODUCT_MAKEFILES := \
    $(LOCAL_DIR)/qassa_PL2.mk \
    $(LOCAL_DIR)/lineage_PL2.mk

COMMON_LUNCH_CHOICES := \
    qassa_PL2-userdebug \
    qassa_PL2-user \
    qassa_PL2-eng \
    lineage_PL2-userdebug
