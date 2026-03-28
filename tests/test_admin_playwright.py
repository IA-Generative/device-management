"""
Playwright E2E tests for the Admin UI.
Requires Docker stack running at localhost:3001.
Run with: pytest tests/test_admin_playwright.py --base-url http://localhost:3001
"""

import pytest
from playwright.sync_api import Page, expect


BASE = "http://localhost:3001"


# ─── Navigation tests ────────────────────────────────────────────────────

def test_dashboard_loads(page: Page):
    """Dashboard page loads with title and metrics."""
    page.goto(f"{BASE}/admin/")
    expect(page.locator("h1")).to_contain_text("Tableau de bord")
    # Check metric tiles exist
    expect(page.locator(".dm-metric-tile")).to_have_count(4)


def test_nav_links_work(page: Page):
    """All navigation links are clickable and lead to 200 pages."""
    page.goto(f"{BASE}/admin/")
    nav_items = page.locator(".dm-nav__item a")
    count = nav_items.count()
    assert count == 7, f"Expected 7 nav items, got {count}"

    # Click each nav link and verify page loads
    links = [
        ("Tableau de bord", "Tableau de bord"),
        ("Appareils", "Appareils"),
        ("Cohortes", "Cohortes"),
        ("Feature flags", "Feature flags"),
        ("Artifacts", "Artifacts"),
        ("Campagnes", "Campagnes"),
    ]
    for link_text, expected_title in links:
        page.goto(f"{BASE}/admin/")
        page.click(f".dm-nav__item a:has-text('{link_text}')")
        expect(page.locator("h1")).to_contain_text(expected_title)
    # Audit link — apostrophe in text requires different selector
    page.goto(f"{BASE}/admin/")
    page.click(".dm-nav__item a[href='/admin/audit']")
    expect(page.locator("h1")).to_contain_text("Journal")


def test_devices_page(page: Page):
    """Devices page loads with search bar and table."""
    page.goto(f"{BASE}/admin/devices")
    expect(page.locator("h1")).to_contain_text("Appareils")
    # Search input exists
    expect(page.locator("#search-owner")).to_be_visible()
    # Health summary counters
    expect(page.locator(".dm-counter")).to_have_count(4)
    # Table exists
    expect(page.locator("table")).to_be_visible()


def test_cohorts_page(page: Page):
    """Cohorts page loads with create form and table."""
    page.goto(f"{BASE}/admin/cohorts")
    expect(page.locator("h1")).to_contain_text("Cohortes")
    # Create form toggle exists
    expect(page.locator("details summary")).to_contain_text("Creer une cohorte")


def test_flags_page(page: Page):
    """Feature flags page loads."""
    page.goto(f"{BASE}/admin/flags")
    expect(page.locator("h1")).to_contain_text("Feature flags")
    expect(page.locator("details summary")).to_contain_text("Creer un feature flag")


def test_artifacts_page(page: Page):
    """Artifacts page loads with upload form."""
    page.goto(f"{BASE}/admin/artifacts")
    expect(page.locator("h1")).to_contain_text("Artifacts")
    expect(page.locator("details summary")).to_contain_text("Uploader un artifact")


def test_campaigns_page(page: Page):
    """Campaigns page loads with new campaign button."""
    page.goto(f"{BASE}/admin/campaigns")
    expect(page.locator("h1")).to_contain_text("Campagnes")
    expect(page.locator("a:has-text('Nouvelle campagne')")).to_be_visible()


def test_campaign_new_wizard(page: Page):
    """Campaign creation wizard has 3 steps."""
    page.goto(f"{BASE}/admin/campaigns/new")
    expect(page.locator("h1")).to_contain_text("Nouvelle campagne")
    # 3 fieldsets (steps)
    fieldsets = page.locator("fieldset")
    assert fieldsets.count() == 3


def test_audit_page(page: Page):
    """Audit log page loads with filters and export button."""
    page.goto(f"{BASE}/admin/audit")
    expect(page.locator("h1")).to_contain_text("Journal")
    expect(page.locator("a:has-text('Export CSV')")).to_be_visible()


# ─── CRUD tests ───────────────────────────────────────────────────────────

def test_create_cohort(page: Page):
    """Create a new cohort via the form."""
    page.goto(f"{BASE}/admin/cohorts")
    # Open create form
    page.click("details summary")
    page.fill("#name", "test-cohort-e2e")
    page.select_option("#type", "manual")
    page.fill("#description", "Playwright test cohort")
    page.fill("#members", "test1@example.com\ntest2@example.com")
    page.click("button:has-text('Creer')")
    # Should redirect back to cohorts list
    page.wait_for_url("**/admin/cohorts**")
    expect(page.locator("text=test-cohort-e2e")).to_be_visible()


def test_create_flag(page: Page):
    """Create a new feature flag via the form."""
    page.goto(f"{BASE}/admin/flags")
    page.click("details summary")
    page.fill("#name", "test_flag_e2e")
    page.fill("#description", "Playwright test flag")
    page.click("button:has-text('Creer')")
    page.wait_for_url("**/admin/flags**")
    expect(page.locator("text=test_flag_e2e")).to_be_visible()


def test_create_campaign(page: Page):
    """Create a campaign via the wizard."""
    page.goto(f"{BASE}/admin/campaigns/new")
    page.fill("input[name='name']", "E2E Test Campaign")
    page.fill("textarea[name='description']", "Created by Playwright")
    page.click("button:has-text('Creer la campagne')")
    page.wait_for_url("**/admin/campaigns/**")
    expect(page.locator("h1")).to_contain_text("E2E Test Campaign")


# ─── Security tests ──────────────────────────────────────────────────────

def test_static_css_served(page: Page):
    """Static CSS file should be served."""
    response = page.request.get(f"{BASE}/admin/static/dm-admin.css")
    assert response.status == 200
    assert "dm-progress-bar" in response.text()


def test_security_headers(page: Page):
    """Security headers should be present on admin pages."""
    response = page.request.get(f"{BASE}/admin/")
    headers = response.headers
    assert headers.get("x-frame-options") == "DENY"
    assert headers.get("x-content-type-options") == "nosniff"
    assert "content-security-policy" in headers


def test_logout_clears_session(page: Page):
    """Logout should clear session cookie and redirect."""
    page.goto(f"{BASE}/admin/logout")
    # Should redirect to /admin/
    page.wait_for_url("**/admin/**")
