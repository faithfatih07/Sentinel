
import sqlite3
from pathlib import Path

# Always use the database located beside this Python file
DB_PATH = Path(__file__).with_name("sentinel.db")

connection = sqlite3.connect(DB_PATH)
connection.execute("PRAGMA foreign_keys = ON")
cursor = connection.cursor()

# ----------------------------------------
# 1. PATTERN CATEGORIES
# ----------------------------------------

cursor.execute("""
CREATE TABLE IF NOT EXISTS pattern_categories (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    category_key TEXT UNIQUE NOT NULL,
    name TEXT NOT NULL,
    description TEXT
)
""")

# ----------------------------------------
# 2. CANONICAL PATTERNS
# ----------------------------------------

cursor.execute("""
CREATE TABLE IF NOT EXISTS patterns (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    canonical_name TEXT UNIQUE NOT NULL,
    description TEXT,
    is_active INTEGER DEFAULT 1,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
)
""")

# ----------------------------------------
# 3. PATTERN-CATEGORY RELATIONSHIPS
# One pattern can belong to multiple categories
# ----------------------------------------

cursor.execute("""
CREATE TABLE IF NOT EXISTS pattern_category_map (
    pattern_id INTEGER NOT NULL,
    category_id INTEGER NOT NULL,

    PRIMARY KEY (pattern_id, category_id),

    FOREIGN KEY (pattern_id)
        REFERENCES patterns(id),

    FOREIGN KEY (category_id)
        REFERENCES pattern_categories(id)
)
""")

# ----------------------------------------
# 4. LANGUAGE AND SEARCH VARIANTS
# ----------------------------------------

cursor.execute("""
CREATE TABLE IF NOT EXISTS pattern_variants (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    pattern_id INTEGER NOT NULL,
    language TEXT NOT NULL,
    variant_text TEXT NOT NULL,

    UNIQUE(language, variant_text),

    FOREIGN KEY (pattern_id)
        REFERENCES patterns(id)
)
""")

# ----------------------------------------
# 5. AI-GENERATED PATTERN PROPOSALS
# ----------------------------------------

cursor.execute("""
CREATE TABLE IF NOT EXISTS pattern_proposals (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    proposed_text TEXT NOT NULL,
    language TEXT NOT NULL,
    description TEXT,
    evidence_url TEXT,
    evidence_excerpt TEXT,
    confidence REAL,
    suggested_category_id INTEGER,
    proposed_by TEXT DEFAULT 'ai',

    status TEXT DEFAULT 'pending'
        CHECK(status IN ('pending', 'approved', 'rejected')),

    approved_pattern_id INTEGER,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    reviewed_at TIMESTAMP,

    FOREIGN KEY (suggested_category_id)
        REFERENCES pattern_categories(id),

    FOREIGN KEY (approved_pattern_id)
        REFERENCES patterns(id)
)
""")

# ----------------------------------------
# INITIALIZE FOUR CATEGORIES
# ----------------------------------------

categories = [
    (
        "service",
        "Service Patterns",
        "Types of financial services being offered."
    ),
    (
        "recommendation",
        "Recommendation Patterns",
        "Expressions suggesting financial instrument decisions."
    ),
    (
        "personalization",
        "Personalization Patterns",
        "Expressions indicating customer-specific services."
    ),
    (
        "commercial_intent",
        "Commercial Intent Patterns",
        "Expressions indicating commercial offers."
    )
]

cursor.executemany("""
    INSERT OR IGNORE INTO pattern_categories
    (category_key, name, description)
    VALUES (?, ?, ?)
""", categories)

# ----------------------------------------
# INITIAL PATTERN DATA
# ----------------------------------------

initial_patterns = [
    {
        "name": "investment_advisory",
        "description": "Investment advisory services.",
        "category": "service",
        "variants": [
            ("pl", "doradztwo inwestycyjne"),
            ("en", "investment advisory"),
            ("pl", "doradztwo finansowe")
        ]
    },
    {
        "name": "portfolio_management",
        "description": "Portfolio and asset management services.",
        "category": "service",
        "variants": [
            ("pl", "zarządzanie portfelem inwestycyjnym"),
            ("en", "portfolio management"),
            ("en", "wealth management")
        ]
    },
    {
        "name": "buy_sell_recommendations",
        "description": "Recommendations to buy or sell financial instruments.",
        "category": "recommendation",
        "variants": [
            ("pl", "rekomendacje kupna i sprzedaży"),
            ("en", "buy and sell recommendations"),
            ("pl", "sygnały kupna i sprzedaży")
        ]
    },
    {
        "name": "trading_signals",
        "description": "Trading signals and investment recommendations.",
        "category": "recommendation",
        "variants": [
            ("pl", "sygnały inwestycyjne"),
            ("en", "investment signals"),
            ("en", "trading signals")
        ]
    },
    {
        "name": "personalized_strategy",
        "description": "Investment strategies tailored to individual clients.",
        "category": "personalization",
        "variants": [
            ("pl", "indywidualna strategia inwestycyjna"),
            ("en", "personalized investment strategy"),
            ("pl", "strategia dopasowana do Twoich potrzeb")
        ]
    },
    {
        "name": "client_financial_situation",
        "description": "Services based on individual financial circumstances.",
        "category": "personalization",
        "variants": [
            ("pl", "na podstawie Twojej sytuacji finansowej"),
            ("en", "based on your financial situation"),
            ("en", "tailored to your financial needs")
        ]
    },
    {
        "name": "book_consultation",
        "description": "Invitations to book a commercial consultation.",
        "category": "commercial_intent",
        "variants": [
            ("pl", "umów konsultację"),
            ("en", "book a consultation"),
            ("pl", "zarezerwuj rozmowę")
        ]
    },
    {
        "name": "contact_advisor",
        "description": "Commercial invitations to contact an advisor.",
        "category": "commercial_intent",
        "variants": [
            ("pl", "skontaktuj się z doradcą"),
            ("en", "contact an investment advisor"),
            ("pl", "porozmawiaj z naszym doradcą")
        ]
    }
]

# ----------------------------------------
# SAVE PATTERNS, CATEGORIES AND VARIANTS
# ----------------------------------------

for pattern in initial_patterns:

    cursor.execute("""
        INSERT OR IGNORE INTO patterns
        (canonical_name, description)
        VALUES (?, ?)
    """, (
        pattern["name"],
        pattern["description"]
    ))

    cursor.execute("""
        SELECT id FROM patterns
        WHERE canonical_name = ?
    """, (pattern["name"],))

    pattern_id = cursor.fetchone()[0]

    cursor.execute("""
        SELECT id FROM pattern_categories
        WHERE category_key = ?
    """, (pattern["category"],))

    category_id = cursor.fetchone()[0]

    cursor.execute("""
        INSERT OR IGNORE INTO pattern_category_map
        (pattern_id, category_id)
        VALUES (?, ?)
    """, (pattern_id, category_id))

    for language, phrase in pattern["variants"]:

        cursor.execute("""
            INSERT OR IGNORE INTO pattern_variants
            (pattern_id, language, variant_text)
            VALUES (?, ?, ?)
        """, (pattern_id, language, phrase))

connection.commit()

# ----------------------------------------
# DISPLAY RESULTS
# ----------------------------------------

print("\n--- SENTINEL PATTERN LIBRARY ---")

cursor.execute("""
    SELECT
        c.name,
        COUNT(DISTINCT p.id)
    FROM pattern_categories c
    LEFT JOIN pattern_category_map pcm
        ON c.id = pcm.category_id
    LEFT JOIN patterns p
        ON p.id = pcm.pattern_id
    GROUP BY c.id
""")

for name, count in cursor.fetchall():
    print(f"{name}: {count} patterns")

cursor.execute("SELECT COUNT(*) FROM pattern_variants")
variant_count = cursor.fetchone()[0]

cursor.execute("""
    SELECT COUNT(*)
    FROM pattern_proposals
    WHERE status = 'pending'
""")

pending_count = cursor.fetchone()[0]

print(f"\nLanguage variants: {variant_count}")
print(f"Pending AI proposals: {pending_count}")

print("\nPattern Library initialized successfully!")

connection.close()

