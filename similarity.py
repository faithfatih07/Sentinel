
from rapidfuzz import fuzz

# Simulated advertisement database
existing_ads = [
    {
        "id": 101,
        "company": "BrokerX",
        "text": "Start investing with BrokerX today!"
    },
    {
        "id": 102,
        "company": "BrokerX",
        "text": "Discover investment opportunities with BrokerX."
    },
    {
        "id": 103,
        "company": "CryptoX",
        "text": "Buy Bitcoin and start trading today!"
    },
    {
        "id": 104,
        "company": "BrokerY",
        "text": "Open your investment account today!"
    }
]

# New advertisement collected by our scraper
new_ad = {
    "id": 105,
    "company": "BrokerX",
    "text": "Start investing with BrokerX now!"
}

# STEP 1: Select candidates
candidates = [
    ad for ad in existing_ads
    if ad["company"] == new_ad["company"]
]

print(f"Total advertisements: {len(existing_ads)}")
print(f"Selected candidates: {len(candidates)}")

# STEP 2: Compare only selected candidates
print("\nSimilarity analysis:\n")

for ad in candidates:

    score = fuzz.ratio(
        new_ad["text"],
        ad["text"]
    )

    print(f"Advertisement {ad['id']}: {score:.2f}%")

    if score >= 75:
        print("Potentially similar advertisement!")

