"""Amazon Clone holdout module.

This module provides the test functions called by the evaluator for
the Amazon Clone MVP benchmark scenarios.
"""

from holdout_amazon.playwright_test import (
    test_product_listing_loads,
    test_search_filters_products,
    test_detail_page_fields,
    test_cart_operations,
    test_checkout_invalid_email,
    test_checkout_valid_order,
    test_cart_persistence,
    test_mobile_viewport,
    test_accessibility_labels,
    test_no_pii_logged,
)

__all__ = [
    "test_product_listing_loads",
    "test_search_filters_products",
    "test_detail_page_fields",
    "test_cart_operations",
    "test_checkout_invalid_email",
    "test_checkout_valid_order",
    "test_cart_persistence",
    "test_mobile_viewport",
    "test_accessibility_labels",
    "test_no_pii_logged",
]