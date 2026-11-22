import sqlite3
import json
import shutil

from pathlib import Path
from datetime import datetime

BASE_DIR = Path(__file__).resolve().parent
DB = BASE_DIR / "sentinel.db"
BACKUP_DIR = BASE_DIR / "backups"

BACKUP_DIR.mkdir(exist_ok=True)

if not DB.exists():
    raise FileNotFoundError("sentinel.db not found")

# ==========================================
# SOURCE DEFINITIONS
# ==========================================

SOURCES = [

    # --------------------------------------
    # META
    # --------------------------------------

    {
        "source_key": "meta_ad_library_ui",
        "source_name": "Meta Ad Library - Public Interface",
        "source_type": "ad_library",
        "platform": "Meta",
        "base_url": "https://www.facebook.com/ads/library/",
        "jurisdiction": "EU",
        "geographic_scope": "Global current ads; EU ads from the past year; separate political/social issue archives",
        "languages": [],
        "content_types": ["image", "video", "text", "advertisements"],
        "access_method": "Public browser interface; manual advertiser and keyword searches",
        "access_status": "manual_only",
        "auth_required": 0,
        "notes": "Use for initial source validation and manual discovery. Verify current availability and retention scope."
    },

    {
        "source_key": "meta_ad_library_api",
        "source_name": "Meta Ad Library API",
        "source_type": "advertising_api",
        "platform": "Meta",
        "base_url": "https://graph.facebook.com/",
        "jurisdiction": "EU",
        "geographic_scope": "EU-delivered ads within the documented one-year window; global political and issue ads have separate coverage",
        "languages": [],
        "content_types": [
            "image",
            "video",
            "text",
            "financial_products_and_services_ads",
            "advertiser_metadata"
        ],
        "access_method": "Graph API Ads Archive; developer app and access token; identity and location verification steps may apply",
        "access_status": "not_assessed",
        "auth_required": 1,
        "notes": "Meta documents FINANCIAL_PRODUCTS_AND_SERVICES_ADS as a filter. Search terms must be supplied in the source language. Do not assume global coverage of ordinary commercial ads."
    },

    {
        "source_key": "meta_branded_content",
        "source_name": "Meta Branded Content Search",
        "source_type": "paid_partnership_library",
        "platform": "Meta",
        "base_url": "https://www.facebook.com/ads/library/",
        "jurisdiction": "Meta platform coverage",
        "geographic_scope": "Verify current interface and API coverage",
        "languages": [],
        "content_types": [
            "paid_partnerships",
            "creator_posts",
            "stories",
            "reels",
            "videos"
        ],
        "access_method": "Meta branded content search interface; API access and permissions require separate review",
        "access_status": "manual_only",
        "auth_required": 0,
        "notes": "Separate influencer-brand partnerships from ordinary paid advertising."
    },

    # --------------------------------------
    # TIKTOK
    # --------------------------------------

    {
        "source_key": "tiktok_ccl_ui",
        "source_name": "TikTok Commercial Content Library",
        "source_type": "ad_library",
        "platform": "TikTok",
        "base_url": "https://library.tiktok.com/",
        "jurisdiction": "EEA, Switzerland, UK",
        "geographic_scope": "Commercial Content Library coverage; verify available country filters",
        "languages": [],
        "content_types": [
            "paid_ads",
            "other_commercial_content",
            "videos",
            "advertiser_metadata"
        ],
        "access_method": "Public browser interface",
        "access_status": "manual_only",
        "auth_required": 0,
        "notes": "Public library is available without a TikTok account. Do not assume all organic content is included."
    },

    {
        "source_key": "tiktok_commercial_content_api",
        "source_name": "TikTok Commercial Content API",
        "source_type": "advertising_api",
        "platform": "TikTok",
        "base_url": "https://developers.tiktok.com/products/commercial-content-api",
        "jurisdiction": "EU",
        "geographic_scope": "EU ad data in the currently documented initial API rollout",
        "languages": [],
        "content_types": [
            "paid_ads",
            "advertiser_metadata",
            "targeting_information",
            "other_commercial_content"
        ],
        "access_method": "TikTok for Developers account, application review and approved client key",
        "access_status": "not_assessed",
        "auth_required": 1,
        "notes": "Public and researcher API application. Approval required. Review current API documentation before implementation."
    },

    # --------------------------------------
    # GOOGLE
    # --------------------------------------

    {
        "source_key": "google_ads_transparency_ui",
        "source_name": "Google Ads Transparency Center",
        "source_type": "ad_library",
        "platform": "Google",
        "base_url": "https://adstransparency.google.com/",
        "jurisdiction": "Global interface; verify regional coverage",
        "geographic_scope": "Search by advertiser or website and filter by region and date",
        "languages": [],
        "content_types": [
            "search_ads",
            "display_ads",
            "youtube_ads",
            "advertiser_metadata",
            "video"
        ],
        "access_method": "Public browser interface",
        "access_status": "manual_only",
        "auth_required": 0,
        "notes": "Use for manual research and validation of advertiser identities and campaigns."
    },

    {
        "source_key": "google_ads_transparency_eea_api",
        "source_name": "Google Ads Transparency Center - EEA Data API",
        "source_type": "advertising_api",
        "platform": "Google",
        "base_url": "https://adstransparency.google.com/",
        "jurisdiction": "EEA",
        "geographic_scope": "Ads served in the European Economic Area",
        "languages": [],
        "content_types": [
            "advertisement_creatives",
            "advertiser",
            "payer",
            "campaign_period",
            "targeting_information",
            "reach_information"
        ],
        "access_method": "Official Ads Transparency Center API; access and usage subject to Google terms",
        "access_status": "not_assessed",
        "auth_required": 1,
        "notes": "Verify current API documentation, authentication requirements and permitted use before implementation."
    },

    # --------------------------------------
    # KNF REGULATORY SOURCES
    # --------------------------------------

    {
        "source_key": "knf_public_warnings",
        "source_name": "KNF Public Warnings",
        "source_type": "regulatory_warning_list",
        "platform": "KNF",
        "base_url": "https://www.knf.gov.pl/en/CONSUMERS/Information_for_the_financial_market_consumers/Public_warnings",
        "jurisdiction": "PL",
        "geographic_scope": "Entities connected with KNF public warnings and related proceedings",
        "languages": ["pl", "en"],
        "content_types": [
            "entity_names",
            "warning_records",
            "identifiers",
            "proceeding_information",
            "related_domains"
        ],
        "access_method": "Official public browser interface; manual review initially",
        "access_status": "manual_only",
        "auth_required": 0,
        "notes": "A listing is an investigative signal, not an automatic fraud verdict. Absence from the list does not establish legitimacy."
    },

    {
        "source_key": "knf_investment_firms",
        "source_name": "KNF Investment Firms Register",
        "source_type": "licensing_register",
        "platform": "KNF",
        "base_url": "https://www.knf.gov.pl/en/MARKET/Licensing_and_registration/Capital_sector/Register",
        "jurisdiction": "PL",
        "geographic_scope": "Polish and relevant foreign investment firms and tied agents",
        "languages": ["pl", "en"],
        "content_types": [
            "investment_firms",
            "brokerage_houses",
            "foreign_firms",
            "tied_agents",
            "licensing_information"
        ],
        "access_method": "Official public register and name search; manual verification initially",
        "access_status": "manual_only",
        "auth_required": 0,
        "notes": "Use for licensing and entity verification, not as a standalone risk verdict."
    },

    {
        "source_key": "knf_payment_register",
        "source_name": "KNF Payment Services Register - e-RUP",
        "source_type": "licensing_register",
        "platform": "KNF",
        "base_url": "https://e-rup.knf.gov.pl/",
        "jurisdiction": "PL",
        "geographic_scope": "Polish payment and electronic money institutions and related entities",
        "languages": ["pl"],
        "content_types": [
            "payment_institutions",
            "electronic_money_institutions",
            "agents",
            "branches",
            "registration_information"
        ],
        "access_method": "Official public register; manual verification initially",
        "access_status": "manual_only",
        "auth_required": 0,
        "notes": "Check current register structure and available export methods before adapter development."
    },

    {
        "source_key": "knf_investment_funds_api",
        "source_name": "KNF Investment Funds and Management Companies Data",
        "source_type": "regulatory_data_api",
        "platform": "KNF",
        "base_url": "https://wybieramfundusze-api.knf.gov.pl/",
        "jurisdiction": "PL",
        "geographic_scope": "Polish investment funds and fund management companies",
        "languages": ["pl"],
        "content_types": [
            "investment_funds",
            "fund_management_companies",
            "structured_entity_data"
        ],
        "access_method": "Structured data and API linked from the official KNF funds page",
        "access_status": "not_assessed",
        "auth_required": 0,
        "notes": "Confirm API schema, update frequency, terms and pagination before adapter development."
    },

    # --------------------------------------
    # CORPORATE REGISTRY
    # --------------------------------------

    {
        "source_key": "poland_krs_open_api",
        "source_name": "Polish National Court Register - KRS Open API",
        "source_type": "corporate_registry",
        "platform": "Polish Ministry of Justice",
        "base_url": "https://prs.ms.gov.pl/krs/openApi",
        "jurisdiction": "PL",
        "geographic_scope": "Entities registered in the Polish National Court Register",
        "languages": ["pl"],
        "content_types": [
            "legal_entity_data",
            "registration_details",
            "representation_information",
            "registry_changes"
        ],
        "access_method": "Official Open API; technical and reuse conditions require review",
        "access_status": "not_assessed",
        "auth_required": 0,
        "notes": "The official portal describes public API access. Personal data is subject to applicable anonymization and protection rules."
    },

    # --------------------------------------
    # DOMAIN INFRASTRUCTURE
    # --------------------------------------

    {
        "source_key": "icann_rdap",
        "source_name": "ICANN Registration Data Access Protocol",
        "source_type": "domain_registration",
        "platform": "ICANN / RDAP",
        "base_url": "https://lookup.icann.org/",
        "jurisdiction": "Global",
        "geographic_scope": "Supported generic top-level domain registration data",
        "languages": [],
        "content_types": [
            "domain_registration",
            "registrar",
            "registration_dates",
            "nameserver_information",
            "domain_status"
        ],
        "access_method": "RDAP protocol and public lookup interface",
        "access_status": "not_assessed",
        "auth_required": 0,
        "notes": "Registrant information may be redacted. Do not treat shared registrar or nameserver data as proof of common ownership."
    }
]


# ==========================================
# BACKUP
# ==========================================

timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
backup_path = BACKUP_DIR / f"sentinel_before_source_seed_{timestamp}.db"

source_connection = sqlite3.connect(DB)
backup_connection = sqlite3.connect(backup_path)

source_connection.backup(backup_connection)

backup_connection.close()
source_connection.close()

print("Database backup created:", backup_path)


# ==========================================
# INSERT SOURCES
# ==========================================

connection = sqlite3.connect(DB)
connection.execute("PRAGMA foreign_keys = ON")

cursor = connection.cursor()

inserted = 0
existing = 0

sql = """
INSERT OR IGNORE INTO collection_sources (

    source_key,
    source_name,
    source_type,
    platform,
    base_url,
    jurisdiction,
    geographic_scope,
    languages_json,
    content_types_json,
    access_method,
    access_status,
    auth_required,
    terms_reviewed,
    rate_limit_notes,
    permitted_use_notes,
    enabled,
    notes

)
VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
"""

for source in SOURCES:

    values = (
        source["source_key"],
        source["source_name"],
        source["source_type"],
        source["platform"],
        source["base_url"],
        source["jurisdiction"],
        source["geographic_scope"],
        json.dumps(source["languages"], ensure_ascii=False),
        json.dumps(source["content_types"], ensure_ascii=False),
        source["access_method"],
        source["access_status"],
        source["auth_required"],
        0,
        "Not assessed. No automated requests until reviewed.",
        "Review applicable platform terms, API policies and data protection requirements.",
        0,
        source["notes"]
    )

    cursor.execute(sql, values)

    if cursor.rowcount:
        inserted += 1
    else:
        existing += 1

connection.commit()


# ==========================================
# VERIFY
# ==========================================

rows = cursor.execute("""
    SELECT
        id,
        source_key,
        source_name,
        platform,
        access_status,
        enabled,
        terms_reviewed
    FROM collection_sources
    ORDER BY id
""").fetchall()

print("\n===== SENTINEL SOURCE REGISTRY =====")

for row in rows:

    print(f"\nID: {row[0]}")
    print(f"Key: {row[1]}")
    print(f"Name: {row[2]}")
    print(f"Platform: {row[3]}")
    print(f"Access status: {row[4]}")
    print(f"Enabled: {row[5]}")
    print(f"Terms reviewed: {row[6]}")

enabled_count = cursor.execute("""
    SELECT COUNT(*)
    FROM collection_sources
    WHERE enabled = 1
""").fetchone()[0]

print("\n===== SEED SUMMARY =====")
print("Source definitions supplied:", len(SOURCES))
print("New sources inserted:", inserted)
print("Existing sources skipped:", existing)
print("Total registry records:", len(rows))
print("Enabled sources:", enabled_count)

connection.close()

print("\nSource Registry seeded successfully.")
print("No collection jobs were started.")
print("No existing advertisements or evidence were changed.")
